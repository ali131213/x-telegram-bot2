"""Telegram bot: open access in private chats, download queue, private archive."""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import set_key
from telegram import BotCommand, BotCommandScopeChat, MessageOriginChannel, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, InvalidToken, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters

from core import BASE, BotProblem, Requester, Settings, Store, caption, extract_links, full_text

LOG = logging.getLogger("xbot")


@dataclass
class Job:
    chat_id: int
    status_message_id: int
    url: str
    requester: Requester


def retry_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    return float(value.total_seconds() if hasattr(value, "total_seconds") else value) + 1


class Service:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.store = Store(cfg.data_dir / "archivo.sqlite3")
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=20)
        self.users: Counter[int] = Counter()
        self.tasks: list[asyncio.Task] = []
        self.archive_wakeup = asyncio.Event()
        self.active = False

    async def startup(self, app: Application):
        public_commands = [
            BotCommand("start", "Descargar de X/Twitter"),
            BotCommand("ayuda", "Cómo usar el bot"),
            BotCommand("estado", "Comprobar que el bot está activo"),
        ]
        await app.bot.set_my_commands(public_commands)
        if self.cfg.owner_id:
            try:
                await app.bot.set_my_commands(
                    public_commands + [
                        BotCommand("id", "Mostrar tu ID de Telegram"),
                        BotCommand("version", "Comprobar la versión instalada"),
                        BotCommand("canales", "Ver canales detectados"),
                        BotCommand("archivo", "Vincular el canal de archivo"),
                    ],
                    scope=BotCommandScopeChat(chat_id=self.cfg.owner_id),
                )
            except TelegramError as exc:
                LOG.warning("No se pudo configurar el menú del dueño: %s", type(exc).__name__)
        self.tasks = [
            asyncio.create_task(self.download_loop(app), name="descargas"),
            asyncio.create_task(self.archive_loop(app), name="archivo"),
        ]
        identity = app.bot.username
        LOG.info("Bot activo: @%s. Acceso abierto a todos los chats privados.", identity)
        if not self.cfg.owner_id:
            LOG.info("Obtén tu ID con /id y vuelve a ejecutar configurar.py para establecer OWNER_ID.")
        if not self.cfg.archive_id:
            LOG.info("Canal aún sin vincular. Las pruebas se envían solamente al solicitante.")

    async def shutdown(self, app: Application):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()

    def is_owner(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id == self.cfg.owner_id)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "Envíame un enlace de X/Twitter y te devolveré sus videos y fotos.\n\n"
            "Un enlace por mensaje."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "Envía un enlace público de X/Twitter, uno por mensaje.\n\n"
            "Recibirás los videos, GIF y fotos de esa publicación. "
            "Los videos grandes pueden llegar divididos en partes.\n\n"
            "Si un enlace falla, verifica que la publicación siga disponible."
        )

    async def identify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            f"Tu ID de Telegram: {update.effective_user.id}"
        )

    async def version(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "Descargador de X/Twitter · v1.1"
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = "✅ Bot activo."
        if self.is_owner(update):
            text += (
                f"\nDescarga en curso: {'sí' if self.active else 'no'}"
                f"\nSolicitudes esperando: {self.queue.qsize()}"
                f"\nCanal de archivo: {self.cfg.archive_id or 'sin configurar'}"
                f"\nCopias pendientes de archivo: {self.store.pending()}"
            )
        await update.effective_message.reply_text(text)

    async def channel_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_owner(update):
            await update.effective_message.reply_text("Comando no disponible.")
            return
        channels = self.store.channels()
        if not channels:
            await update.effective_message.reply_text(
                "Todavía no detecté canales. Agrégame como administrador del canal privado, "
                "con permiso para publicar, y escribe un mensaje nuevo dentro del canal mientras "
                "el bot está encendido. Después vuelve a usar /canales."
            )
            return
        text = "Canales detectados:\n\n" + "\n\n".join(
            f"{row['title'][:200]}\nID: {row['id']}\n/archivo {row['id']}" for row in channels
        )
        await update.effective_message.reply_text(text[:3900])

    async def bind_archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_owner(update):
            await update.effective_message.reply_text("Comando no disponible.")
            return
        if len(context.args) != 1:
            await update.effective_message.reply_text("Usa /canales y luego /archivo -100NUMERO_DEL_CANAL")
            return
        try:
            chat_id = int(context.args[0])
            if chat_id >= 0:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text("Usa el ID negativo del canal, que normalmente comienza por -100.")
            return
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type != ChatType.CHANNEL:
                raise BotProblem("El archivo debe ser un canal, no un grupo ni un chat personal.")
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if member.status != "administrator" or not member.can_post_messages:
                raise BotProblem("Agrega el bot como administrador del canal con permiso para publicar mensajes.")
            await context.bot.send_message(
                chat_id, "✅ Canal de archivo vinculado. Aquí se guardarán las próximas descargas."
            )
            set_key(str(BASE / ".env"), "ARCHIVE_CHAT_ID", str(chat_id))
            self.cfg.archive_id = chat_id
            self.store.remember_channel(chat_id, chat.title or str(chat_id))
            self.archive_wakeup.set()
        except BotProblem as exc:
            await update.effective_message.reply_text(str(exc))
            return
        except TelegramError:
            await update.effective_message.reply_text(
                "No pude publicar en ese canal. Revisa el ID y el permiso del bot para publicar."
            )
            return
        except OSError:
            await update.effective_message.reply_text(
                "El canal responde, pero no pude guardar la configuración en .env. Revisa los permisos de la carpeta."
            )
            return
        await update.effective_message.reply_text(
            f"✅ Archivo vinculado: {chat.title}\n"
            "Las próximas descargas tendrán su copia aquí.\n\n"
            f"En Render, guarda también ARCHIVE_CHAT_ID={chat_id} en Environment y "
            "aplica el cambio. Eso conserva la vinculación después de que el servicio se reinicie."
        )

    async def observe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat and chat.type == ChatType.CHANNEL:
            self.store.remember_channel(chat.id, chat.title or str(chat.id))

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.effective_message
        origin = message.forward_origin
        if self.is_owner(update) and isinstance(origin, MessageOriginChannel):
            self.store.remember_channel(origin.chat.id, origin.chat.title or str(origin.chat.id))
            await message.reply_text(
                f"Canal detectado: {origin.chat.title}\n"
                f"Para vincularlo usa:\n/archivo {origin.chat.id}"
            )
            return
        text = message.text or message.caption or ""
        # Include hyperlinks whose visible label is different from the URL.
        for entity in (*(message.entities or ()), *(message.caption_entities or ())):
            if entity.url:
                text += "\n" + entity.url
        links = extract_links(text)
        if len(links) != 1:
            await message.reply_text(
                "Envía un solo enlace de una publicación de X/Twitter por mensaje. "
                "Ejemplo: https://x.com/cuenta/status/123456789"
            )
            return
        user = update.effective_user
        if self.users[user.id] >= 2:
            await message.reply_text("Ya tienes 2 solicitudes pendientes. Espera a que termine una.")
            return
        if self.queue.full():
            await message.reply_text("La cola está llena. Inténtalo nuevamente cuando termine alguna descarga.")
            return
        position = self.queue.qsize() + (1 if self.active else 0)
        status_message = await message.reply_text(
            "Enlace recibido. " + (f"Hay {position} solicitud(es) por delante." if position else "Preparando descarga…")
        )
        requester = Requester(
            id=user.id, name=user.full_name, username=user.username,
            requested_at=message.date or datetime.now(timezone.utc),
        )
        job = Job(message.chat_id, status_message.message_id, links[0], requester)
        self.users[user.id] += 1
        self.queue.put_nowait(job)

    async def edit_status(self, app: Application, job: Job, text: str):
        with contextlib.suppress(TelegramError):
            await app.bot.edit_message_text(
                chat_id=job.chat_id, message_id=job.status_message_id, text=text,
            )

    async def run_downloader(self, job: Job, folder: Path) -> dict:
        args = [
            sys.executable, str(BASE / "descarga.py"), "--url", job.url,
            "--folder", str(folder), "--maximum", str(self.cfg.max_download_bytes),
            "--upload-limit", str(self.cfg.max_upload_bytes),
        ]
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=self.cfg.request_timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
            await process.wait()
            raise
        manifest = folder / "resultado.json"
        if not manifest.is_file():
            raise BotProblem("El proceso de descarga terminó sin un archivo válido.")
        result = json.loads(manifest.read_text(encoding="utf-8"))
        if not result.get("ok"):
            raise BotProblem(result.get("error") or "No se pudo descargar el contenido.")
        return result["post"]

    async def upload(self, app: Application, chat_id: int, media: dict, body: str):
        path = Path(media["path"])
        base = dict(
            chat_id=chat_id, caption=body, parse_mode=ParseMode.HTML,
            read_timeout=120, write_timeout=300, connect_timeout=30, pool_timeout=30,
        )
        for attempt in range(3):
            try:
                with path.open("rb") as file:
                    if media["kind"] == "photo" and self.cfg.photo_mode == "foto":
                        try:
                            sent = await app.bot.send_photo(photo=file, **base)
                            return sent, "photo", sent.photo[-1].file_id, {}
                        except BadRequest:
                            # Large images and uncommon dimensions can be sent as originals.
                            file.seek(0)
                    if media["kind"] == "video" and media.get("codec") == "h264":
                        extra = {
                            key: int(media[key]) for key in ("width", "height", "duration")
                            if media.get(key)
                        }
                        try:
                            sent = await app.bot.send_video(video=file, supports_streaming=True, **extra, **base)
                            return sent, "video", sent.video.file_id, extra
                        except BadRequest:
                            file.seek(0)
                    sent = await app.bot.send_document(
                        document=file, filename=path.name, disable_content_type_detection=True, **base,
                    )
                    return sent, "document", sent.document.file_id, {}
            except RetryAfter as exc:
                if attempt == 2:
                    raise
                await asyncio.sleep(retry_seconds(exc))
        raise BotProblem("Telegram no aceptó el archivo.")

    def archive(self, kind: str, file_id: str | None, body: str, extra=None):
        if self.cfg.archive_id:
            self.store.enqueue(self.cfg.archive_id, kind, file_id, body, extra)
            self.archive_wakeup.set()

    async def deliver(self, app: Application, job: Job, post: dict):
        finished = datetime.now(timezone.utc)
        long_text = False
        sent_count = 0
        failed = list(post.get("warnings") or [])
        for media in post["media"]:
            body, clipped_user = caption(post, media["label"], self.cfg)
            archive_body, clipped_archive = caption(
                post, media["label"], self.cfg, job.requester, finished
            )
            long_text |= clipped_user or clipped_archive
            try:
                _, kind, file_id, extra = await self.upload(app, job.chat_id, media, body)
                self.archive(kind, file_id, archive_body, extra)
                sent_count += 1
            except Forbidden:
                raise
            except TelegramError:
                failed.append(f"{media['label']}: Telegram no confirmó el envío.")
            # Space uploads to a single recipient; RetryAfter is also handled explicitly.
            await asyncio.sleep(1.1)
        if not sent_count:
            raise BotProblem("No se pudo entregar ningún archivo. " + " ".join(failed))
        if long_text:
            for text in full_text(post):
                try:
                    await app.bot.send_message(job.chat_id, text, parse_mode=ParseMode.HTML)
                except TelegramError:
                    failed.append("No se pudo entregar una parte del texto completo.")
                self.archive("text", None, text)
                await asyncio.sleep(1.1)
        if failed:
            warning = "Avisos de esta descarga:\n" + "\n".join(failed)
            self.archive("text", None, html.escape(warning))
        result = f"✅ Entregados: {sent_count} archivo(s)."
        if failed:
            result += "\n\n⚠️ " + "\n".join(failed)
        await self.edit_status(app, job, result[:3900])

    async def download_loop(self, app: Application):
        while True:
            job = await self.queue.get()
            self.active = True
            try:
                await self.edit_status(app, job, "Descargando y preparando los archivos…")
                with tempfile.TemporaryDirectory(prefix="xbot_") as temporary:
                    post = await self.run_downloader(job, Path(temporary))
                    await self.edit_status(app, job, "Descarga lista. Enviando a Telegram…")
                    await self.deliver(app, job, post)
            except BotProblem as exc:
                await self.edit_status(app, job, "No se completó la descarga.\n\n" + str(exc))
            except asyncio.TimeoutError:
                minutes = self.cfg.request_timeout // 60
                await self.edit_status(
                    app, job, f"La descarga tardó más de {minutes} minutos y se detuvo. Puedes reenviar el enlace."
                )
            except Forbidden:
                LOG.warning("El solicitante bloqueó el bot o cerró el chat antes de recibir los archivos.")
            except asyncio.CancelledError:
                await self.edit_status(app, job, "El bot se está reiniciando. Vuelve a enviar este enlace cuando esté activo.")
                raise
            except Exception as exc:
                LOG.error("Error de descarga: %s", type(exc).__name__)
                await self.edit_status(app, job, "Ocurrió un error al procesar el enlace. Prueba otro enlace público.")
            finally:
                self.users[job.requester.id] -= 1
                if not self.users[job.requester.id]:
                    del self.users[job.requester.id]
                self.active = False
                self.queue.task_done()

    async def send_archive_job(self, app: Application, job):
        extra = json.loads(job["extra"])
        base = {
            "chat_id": job["chat_id"], "caption": job["body"],
            "parse_mode": ParseMode.HTML, "disable_notification": True,
        }
        kind = job["kind"]
        if kind == "text":
            await app.bot.send_message(
                job["chat_id"], job["body"], parse_mode=ParseMode.HTML, disable_notification=True
            )
        elif kind == "video":
            await app.bot.send_video(video=job["file_id"], supports_streaming=True, **extra, **base)
        elif kind == "photo":
            await app.bot.send_photo(photo=job["file_id"], **base)
        else:
            await app.bot.send_document(document=job["file_id"], **base)

    async def archive_loop(self, app: Application):
        while True:
            self.archive_wakeup.clear()
            job = self.store.next_job()
            if job is None:
                try:
                    await asyncio.wait_for(self.archive_wakeup.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self.send_archive_job(app, job)
            except RetryAfter as exc:
                self.store.retry(job["id"], retry_seconds(exc))
            except TelegramError as exc:
                delay = min(3600, 30 * 2 ** min(job["attempts"], 7))
                self.store.retry(job["id"], delay)
                LOG.warning("Copia de archivo pendiente; se reintentará. Motivo: %s", type(exc).__name__)
            except Exception as exc:
                self.store.retry(job["id"], 300)
                LOG.error("Error de archivo: %s", type(exc).__name__)
            else:
                self.store.done(job["id"])
            await asyncio.sleep(3.1)

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        if isinstance(context.error, Conflict):
            LOG.error("Conflicto: hay otra instancia usando este token. Detén Render y las otras terminales.")
        else:
            LOG.error("Error de Telegram: %s", type(context.error).__name__)


def build_application(cfg: Settings, *, polling: bool = True) -> Application:
    service = Service(cfg)
    builder = (
        Application.builder().token(cfg.token).connect_timeout(30).read_timeout(120)
        .write_timeout(300).pool_timeout(30).connection_pool_size(8)
        .get_updates_connect_timeout(30).get_updates_read_timeout(45)
        .post_init(service.startup).post_stop(service.shutdown)
    )
    if not polling:
        builder = builder.updater(None)
    app = builder.build()
    app.bot_data["service"] = service
    private = filters.ChatType.PRIVATE
    for command, callback in (
        ("start", service.start), ("ayuda", service.help), ("help", service.help),
        ("id", service.identify), ("estado", service.status),
        ("health", service.status), ("version", service.version),
        ("canales", service.channel_list), ("archivo", service.bind_archive),
    ):
        app.add_handler(CommandHandler(command, callback, filters=private))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, service.message))
    app.add_handler(TypeHandler(Update, service.observe), group=-1)
    app.add_error_handler(service.error)
    return app


def main() -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    # HTTP logs can contain Telegram bot tokens. Keep request URLs out of logs.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("telegram").setLevel(logging.CRITICAL)
    try:
        cfg = Settings.load()
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise BotProblem("Falta FFmpeg. Instálalo en tu Mac con: brew install ffmpeg")
        app = build_application(cfg)
        app.run_polling(
            allowed_updates=["message", "channel_post", "my_chat_member"],
            drop_pending_updates=False, timeout=30, bootstrap_retries=3,
        )
        return 0
    except BotProblem as exc:
        LOG.error("%s", exc)
    except InvalidToken:
        LOG.error("Telegram rechazó el token. Vuelve a ejecutar configurar.py.")
    except Exception as exc:
        LOG.error("El bot no pudo arrancar: %s. Revisa tu conexión y la configuración.", type(exc).__name__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
