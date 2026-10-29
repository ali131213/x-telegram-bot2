"""v1.3 workflows over the proven download/upload implementation."""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from core import BotProblem, HOSTS, Requester, URLS, alias_user_id, caption, chunks, extract_links, full_text
from formatting import identity

LOG = logging.getLogger("xbot")
TARGETS = {"usuario": "user", "galeria": "archive", "galería": "archive", "ambos": "both"}
ATTACHMENTS = (
    "photo", "video", "animation", "document", "audio", "voice", "video_note",
    "sticker", "contact", "location", "venue", "poll", "dice", "paid_media",
)


def seconds(error):
    value = error.retry_after
    return float(value.total_seconds() if hasattr(value, "total_seconds") else value) + 1


def has_x_intent(text):
    for match in URLS.finditer(text or ""):
        candidate = match.group().rstrip(".,;:!?)]}\"'")
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = "https://" + candidate
        try:
            if urlsplit(candidate).hostname in HOSTS:
                return True
        except ValueError:
            pass
    return False


def pack_requester(person):
    return {
        "id": person.id, "name": person.name, "username": person.username,
        "requested_at": person.requested_at.isoformat(),
    }


def unpack_requester(data):
    return Requester(
        data["id"], data["name"], data.get("username"), datetime.fromisoformat(data["requested_at"]),
    )


async def wait_event(event, delay=None):
    if delay is None:
        await event.wait()
    else:
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.1, delay))
        except asyncio.TimeoutError:
            pass


class WorkflowMixin:
    def __init__(self, cfg):
        super().__init__(cfg)
        self.locks = defaultdict(asyncio.Lock)
        self.inbox_wakeup = asyncio.Event()
        self.handling_inbox = False
        self.dispatch_failed = False

    def saved_job(self, record):
        payload = json.loads(record["payload"])
        return SimpleNamespace(
            chat_id=payload["chat_id"], status_message_id=payload["status_message_id"],
            url=record["source"], requester=unpack_requester(payload["requester"]), code=record["code"],
        )

    async def startup(self, app):
        # A partially delivered request is never blindly resent after a restart.
        for record in self.store.interrupted_records():
            if self.store.refs(record["code"], "user"):
                self.store.set_status(record["code"], "interrupted")
            else:
                self.store.set_status(record["code"], "queued")
        for record in self.store.queued_records():
            job = self.saved_job(record)
            self.users[job.requester.id] += 1
            self.queue.put_nowait(job)
        for record in self.store.rows("SELECT * FROM records WHERE status='new' AND category='hub'"):
            self.stage_hub(record)
        await super().startup(app)
        self.tasks.append(asyncio.create_task(self.inbox_loop(app), name="recepcion"))
        self.inbox_wakeup.set()
        self.archive_wakeup.set()

    async def error(self, update, context):
        if self.handling_inbox:
            self.dispatch_failed = True
        await super().error(update, context)

    async def inbox_loop(self, app):
        while True:
            self.inbox_wakeup.clear()
            try:
                row = self.store.inbox_next()
                if row is None:
                    await wait_event(self.inbox_wakeup, self.store.inbox_delay())
                    continue
                self.handling_inbox = True
                self.dispatch_failed = False
                update = Update.de_json(json.loads(row["payload"]), app.bot)
                await app.process_update(update)
                if self.dispatch_failed:
                    self.store.inbox_retry(row["update_id"], row["attempts"])
                else:
                    self.store.inbox_done(row["update_id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("Recepción pendiente: %s", type(exc).__name__)
                await asyncio.sleep(10)
            finally:
                self.handling_inbox = False

    async def message(self, update, context):
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return
        person = Requester(
            user.id, user.full_name, user.username,
            message.date or datetime.now(timezone.utc),
        )
        self.store.observe_person(person.id, person.name, person.username)
        source_id = message.message_id
        chat_id = message.chat_id
        text = message.text or message.caption or ""
        attachment = any(getattr(message, key, None) for key in ATTACHMENTS)
        for entity in (*(message.entities or ()), *(message.caption_entities or ())):
            if entity.url:
                text += "\n" + entity.url
        links = extract_links(text) if not attachment else []
        if not attachment and has_x_intent(text):
            if len(links) != 1:
                await message.reply_text("Envía un enlace válido de una publicación de X/Twitter, uno por mensaje.")
                return
            request_key = f"download:{chat_id}:{source_id}"
            existing = self.store.one("SELECT * FROM records WHERE request_key=?", (request_key,))
            if existing and existing["status"] != "new":
                return
            if self.users[person.id] >= 2:
                await message.reply_text("Ya tienes 2 solicitudes pendientes. Espera a que termine una.")
                return
            if self.queue.qsize() >= 20:
                await message.reply_text("La cola está llena. Inténtalo nuevamente cuando termine una descarga.")
                return
            payload = {"chat_id": chat_id, "requester": pack_requester(person)}
            record = existing or self.store.create_record(
                request_key, person.id, links[0], "download", payload, "new",
            )
            status = await message.reply_text("Enlace recibido. Preparando descarga…")
            payload["status_message_id"] = status.message_id
            self.store.update_payload(record["code"], payload)
            self.store.set_status(record["code"], "queued")
            record["payload"] = json.dumps(payload)
            self.users[person.id] += 1
            self.queue.put_nowait(self.saved_job(record))
            return
        # Everything else is a silent contribution, including forwarded media,
        # ordinary links and files with an X URL in their caption.
        category = next((key for key in ATTACHMENTS if getattr(message, key, None)), "texto")
        payload = {
            "from_chat_id": chat_id, "message_id": source_id,
            "requester": pack_requester(person), "type": category,
        }
        record = self.store.create_record(
            f"hub:{chat_id}:{source_id}", person.id, category, "hub", payload, "new",
        )
        self.stage_hub(record)

    def stage_hub(self, record):
        if not self.cfg.archive_id:
            return
        data = json.loads(record["payload"])
        person = unpack_requester(data["requester"])
        header = identity(
            person, self.cfg, self.store.alias_for(person.id),
            code=record["code"], category="Aportación: " + data["type"],
        )
        self.store.stage_jobs(record["code"], [
            {"chat_id": self.cfg.archive_id, "kind": "text", "body": html.escape(header)},
            {"chat_id": self.cfg.archive_id, "kind": "copy", "extra": {
                "_copy": {"from_chat_id": data["from_chat_id"], "message_id": data["message_id"]},
            }},
        ])
        self.archive_wakeup.set()

    def record_message(self, code, side, chat_id, message):
        date = getattr(message, "date", None)
        timestamp = date.timestamp() if isinstance(date, datetime) else time.time()
        self.store.add_ref(code, side, chat_id, message.message_id, timestamp)

    def archive(self, kind, file_id, body, extra=None, code=None):
        if self.cfg.archive_id:
            metadata = dict(extra or {})
            if code:
                metadata["_record"] = code
            self.store.enqueue(self.cfg.archive_id, kind, file_id, body, metadata)
            self.archive_wakeup.set()

    async def deliver(self, app, job, post):
        finished = datetime.now(timezone.utc)
        code = getattr(job, "code", "")
        failed = list(post.get("warnings") or [])
        public_long = archive_long = False
        sent_count = 0
        total = len(post["media"])
        for index, media in enumerate(post["media"], 1):
            label = f"({index}/{total})"
            body, clipped = caption(post, label, self.cfg)
            public_long |= clipped
            try:
                sent, kind, file_id, extra = await self.upload(app, job.chat_id, media, body)
                if code:
                    self.record_message(code, "user", job.chat_id, sent)
                archive_body, clipped_archive = caption(
                    post, label, self.cfg, job.requester, finished,
                    requester_alias=self.store.alias_for(job.requester.id), code=code or None,
                )
                archive_long |= clipped_archive
                self.archive(kind, file_id, archive_body, extra, code=code)
                sent_count += 1
            except Forbidden:
                raise
            except TelegramError:
                failed.append(f"{label}: Telegram no confirmó el envío.")
            await asyncio.sleep(1.1)
        if not sent_count:
            raise BotProblem("No se pudo entregar ningún archivo. " + " ".join(failed))
        if public_long or archive_long:
            for text in full_text(post):
                if public_long:
                    try:
                        sent = await app.bot.send_message(job.chat_id, text, parse_mode=ParseMode.HTML)
                        if code:
                            self.record_message(code, "user", job.chat_id, sent)
                    except TelegramError:
                        failed.append("No se pudo entregar una parte del texto.")
                    await asyncio.sleep(1.1)
                if archive_long:
                    self.archive("text", None, text, code=code)
        if failed:
            self.archive("text", None, html.escape("Avisos:\n" + "\n".join(failed)), code=code)
        result = f"✅ Entregados: {sent_count} archivo(s)."
        if failed:
            result += "\n\n⚠️ " + "\n".join(failed)
        await self.edit_status(app, job, result[:3900])
        if code:
            self.store.add_ref(code, "user", job.chat_id, job.status_message_id)

    async def download_loop(self, app):
        while True:
            job = await self.queue.get()
            self.active = True
            self.store.set_status(job.code, "processing")
            try:
                await self.edit_status(app, job, "Descargando y preparando los archivos…")
                with TemporaryDirectory(prefix="xbot_") as temporary:
                    post = await self.run_downloader(job, Path(temporary))
                    await self.edit_status(app, job, "Descarga lista. Enviando a Telegram…")
                    await self.deliver(app, job, post)
                self.store.set_status(job.code, "done")
            except BotProblem as exc:
                self.store.set_status(job.code, "failed")
                await self.edit_status(app, job, "No se completó la descarga.\n\n" + str(exc))
            except asyncio.TimeoutError:
                self.store.set_status(job.code, "failed")
                await self.edit_status(app, job, "La descarga tardó demasiado. Puedes reenviar el enlace.")
            except Forbidden:
                self.store.set_status(job.code, "failed")
                LOG.warning("El solicitante no admite mensajes del bot.")
            except asyncio.CancelledError:
                # The persisted processing state decides safe recovery at the next startup.
                raise
            except Exception as exc:
                self.store.set_status(job.code, "failed")
                LOG.error("Descarga fallida: %s", type(exc).__name__)
                await self.edit_status(app, job, "No se pudo procesar el enlace. Inténtalo otra vez.")
            finally:
                self.users[job.requester.id] -= 1
                if not self.users[job.requester.id]:
                    del self.users[job.requester.id]
                self.active = False
                self.queue.task_done()

    async def send_archive_job(self, app, job):
        metadata = json.loads(job["extra"])
        code = metadata.pop("_record", None)
        copying = metadata.pop("_copy", None)
        metadata = {key: value for key, value in metadata.items() if not key.startswith("_")}
        kind = job["kind"]
        if kind == "copy":
            args = dict(
                chat_id=job["chat_id"], from_chat_id=copying["from_chat_id"],
                message_id=copying["message_id"], disable_notification=True,
            )
            if code:
                references = self.store.refs(code, "archive")
                if references:
                    args["reply_parameters"] = ReplyParameters(
                        message_id=references[0]["message_id"], allow_sending_without_reply=True,
                    )
            copied = await app.bot.copy_message(**args)
            if code:
                self.store.add_ref(code, "archive", job["chat_id"], copied.message_id)
            return
        base = {
            "chat_id": job["chat_id"], "caption": job["body"], "parse_mode": ParseMode.HTML,
            "disable_notification": True,
        }
        if kind == "text":
            sent = await app.bot.send_message(
                job["chat_id"], job["body"], parse_mode=ParseMode.HTML, disable_notification=True,
            )
        elif kind == "video":
            sent = await app.bot.send_video(
                video=job["file_id"], supports_streaming=True, **metadata, **base,
            )
        elif kind == "photo":
            sent = await app.bot.send_photo(photo=job["file_id"], **base)
        else:
            sent = await app.bot.send_document(document=job["file_id"], **base)
        if code:
            self.record_message(code, "archive", job["chat_id"], sent)

    async def archive_loop(self, app):
        while True:
            self.archive_wakeup.clear()
            try:
                job = self.store.next_job()
                if job is None:
                    await wait_event(self.archive_wakeup, self.store.archive_delay())
                    continue
                code = json.loads(job["extra"]).get("_record", "")
                async with self.locks[code]:
                    record = self.store.record(code) if code else None
                    if record and record["retired_archive"]:
                        self.store.done(job["id"])
                        continue
                    try:
                        await self.send_archive_job(app, job)
                    except RetryAfter as exc:
                        self.store.retry(job["id"], seconds(exc))
                    except (BadRequest, Forbidden) as exc:
                        # Unsupported/protected/deleted sources and invalid permissions need admin review.
                        details = json.loads(job["extra"])
                        details["_last_error"] = str(exc)[:400]
                        self.store.write(
                            "UPDATE outbox SET attempts=12,extra=? WHERE id=?",
                            (json.dumps(details, ensure_ascii=False), job["id"]),
                        )
                        LOG.warning("Archivo requiere revisión (%s): %s", code, type(exc).__name__)
                    except TelegramError:
                        self.store.retry(job["id"], min(3600, 30 * 2**min(job["attempts"], 7)))
                    else:
                        self.store.done(job["id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("Archivo pendiente: %s", type(exc).__name__)
                await asyncio.sleep(10)
            await asyncio.sleep(3.1)

    async def admin_records(self, update, context):
        if not self.is_owner(update):
            await update.effective_message.reply_text("Comando no disponible.")
            return
        try:
            uid = alias_user_id(context.args[0]) if context.args else None
        except BotProblem as exc:
            await update.effective_message.reply_text(str(exc))
            return
        lines = ["Registros recientes:"]
        for row in self.store.records(uid):
            alias = self.store.alias_for(row["user_id"]) or "NO ENLISTADO"
            lines.append(f"{row['code']} · {alias} · {row['user_id']} · {row['category']} · {row['status']}")
        for text in chunks("\n".join(lines)):
            await update.effective_message.reply_text(text)

    async def admin_unlisted(self, update, context):
        if not self.is_owner(update):
            await update.effective_message.reply_text("Comando no disponible.")
            return
        lines = ["Solicitantes no enlistados:"]
        for row in self.store.unlisted_people():
            lines.append(f"{row['user_id']} · {row['name']} · @{row['username']}" if row["username"]
                         else f"{row['user_id']} · {row['name']} · sin @usuario")
        for text in chunks("\n".join(lines)):
            await update.effective_message.reply_text(text)

    async def admin_pending(self, update, context):
        if not self.is_owner(update):
            await update.effective_message.reply_text("Comando no disponible.")
            return
        lines = [f"Copias pendientes: {self.store.pending()}", "Fallos que requieren revisión:"]
        for job in self.store.archive_failures():
            details = json.loads(job["extra"])
            code = details.get("_record", "anterior")
            reason = details.get("_last_error", "Fallo temporal repetido")[:120]
            lines.append(f"{code} · cola {job['id']} · tipo {job['kind']}\n{reason}")
        waiting = self.store.rows("SELECT code FROM records WHERE category='hub' AND status='new' LIMIT 20")
        if waiting:
            lines.append("Aportaciones esperando configuración de canal: " + ", ".join(row["code"] for row in waiting))
        for text in chunks("\n".join(lines)):
            await update.effective_message.reply_text(text)

    async def admin_retry(self, update, context):
        if not self.is_owner(update):
            await update.effective_message.reply_text("Comando no disponible.")
            return
        for record in self.store.rows("SELECT * FROM records WHERE status='new' AND category='hub'"):
            self.stage_hub(record)
        self.store.retry_archives()
        self.archive_wakeup.set()
        await update.effective_message.reply_text("Se reintentará la cola de archivo.")

    async def withdraw(self, update, context):
        if not self.is_owner(update):
            await update.effective_message.reply_text("Comando no disponible.")
            return
        if not context.args:
            await update.effective_message.reply_text(
                "Usa /retirar CODIGO [usuario|galeria|ambos]. Obtén el código en la galería o con /registros."
            )
            return
        code = context.args[0].upper()
        if not self.store.record(code):
            await update.effective_message.reply_text("No existe un registro con ese código.")
            return
        if len(context.args) > 1:
            target = TARGETS.get(context.args[1].lower())
            if not target:
                await update.effective_message.reply_text("Elige usuario, galeria o ambos.")
                return
            await self.withdraw_preview(update.effective_message, code, target)
            return
        await update.effective_message.reply_text(
            f"¿Qué quieres retirar de {code}?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Del usuario", callback_data=f"rt_select:{code}:user"),
                 InlineKeyboardButton("De la galería", callback_data=f"rt_select:{code}:archive")],
                [InlineKeyboardButton("De ambos", callback_data=f"rt_select:{code}:both")],
            ]),
        )

    async def withdraw_preview(self, message, code, target):
        record = self.store.record(code)
        if not record:
            await message.reply_text("Registro no encontrado.")
            return
        if record["status"] in ("new", "queued", "processing") and record["category"] == "download":
            await message.reply_text("La descarga todavía está en curso. Espera a que termine antes de retirarla.")
            return
        refs = self.store.refs(code, target)
        eligible = sum(time.time() - ref["sent_at"] < 172800 for ref in refs)
        token = self.store.confirmation(code, target)
        await message.reply_text(
            f"Retiro {code} · destino: {target}\n"
            f"Mensajes registrados: {len(refs)}; dentro de 48 horas: {eligible}.\n"
            "La eliminación es definitiva. No elimina archivos guardados ni reenvíos a otros chats. "
            "Si incluyes la galería, se cancelarán sus copias todavía pendientes.\n\n¿Confirmas?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Confirmar retiro", callback_data=f"rt_go:{token}"),
                InlineKeyboardButton("Cancelar", callback_data=f"rt_cancel:{token}"),
            ]]),
        )

    async def withdrawal_callback(self, update, context):
        query = update.callback_query
        chat = update.effective_chat
        if not self.is_owner(update) or not chat or chat.type != ChatType.PRIVATE or chat.id != self.cfg.owner_id:
            await query.answer("Comando no disponible.", show_alert=True)
            return
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) == 3 and parts[0] == "rt_select" and parts[2] in ("user", "archive", "both"):
            await self.withdraw_preview(query.message, parts[1], parts[2])
            return
        if len(parts) != 2 or parts[0] not in ("rt_go", "rt_cancel"):
            return
        confirmation = self.store.consume_confirmation(parts[1])
        if confirmation is None:
            await query.edit_message_text("Confirmación vencida o ya utilizada. Vuelve a usar /retirar.")
            return
        if parts[0] == "rt_cancel":
            await query.edit_message_text("Retiro cancelado.")
            return
        code, target = confirmation["code"], confirmation["target"]
        removed = missing = expired = failed = 0
        async with self.locks[code]:
            record = self.store.record(code)
            if not record or record["status"] in ("queued", "processing"):
                await query.edit_message_text("El registro está en proceso; no se realizó el retiro.")
                return
            self.store.retire(code, target)
            self.archive_wakeup.set()
            for ref in self.store.refs(code, target):
                if time.time() - ref["sent_at"] >= 172800:
                    expired += 1
                    continue
                try:
                    await context.bot.delete_message(chat_id=ref["chat_id"], message_id=ref["message_id"])
                except RetryAfter as exc:
                    await asyncio.sleep(seconds(exc))
                    try:
                        await context.bot.delete_message(chat_id=ref["chat_id"], message_id=ref["message_id"])
                    except TelegramError:
                        failed += 1
                        continue
                    self.store.mark_deleted(ref)
                    removed += 1
                except BadRequest as exc:
                    if "message to delete not found" in str(exc).lower():
                        self.store.mark_deleted(ref)
                        missing += 1
                    else:
                        failed += 1
                except TelegramError:
                    failed += 1
                else:
                    self.store.mark_deleted(ref)
                    removed += 1
                await asyncio.sleep(1.1)
        await query.edit_message_text(
            f"Retiro {code}\nEliminados: {removed}\nYa ausentes: {missing}\n"
            f"Fuera de plazo: {expired}\nFallidos: {failed}\n"
            "Los fallidos conservan su registro para poder revisarlos o reintentar."
        )
