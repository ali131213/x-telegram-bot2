"""Render Web Service entry point. Telegram wakes the service through HTTPS."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sys
from urllib.parse import urlsplit

import tornado.httpserver
import tornado.web
from telegram import Update

from app import build_application
from core import BotProblem, Settings

LOG = logging.getLogger("xbot")


def webhook_config(token: str) -> tuple[str, str]:
    public_url = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
    parsed = urlsplit(public_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BotProblem("Falta RENDER_EXTERNAL_URL o PUBLIC_URL con una dirección https válida.")
    secret = os.getenv("WEBHOOK_SECRET", "").strip() or hashlib.sha256(
        ("xbot-webhook:" + token).encode()
    ).hexdigest()
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", secret):
        raise BotProblem("WEBHOOK_SECRET debe tener de 24 a 256 letras, números, guiones o guiones bajos.")
    return public_url, secret


class Health(tornado.web.RequestHandler):
    def initialize(self, state):
        self.state = state

    def get(self):
        self.set_status(200 if self.state["ready"] else 503)
        self.write({"ok": self.state["ready"], "service": "x-telegram-bot"})

    def head(self):
        self.set_status(200 if self.state["ready"] else 503)
        self.finish()


class Webhook(tornado.web.RequestHandler):
    def initialize(self, bot_app, secret, state):
        self.bot_app, self.secret, self.state = bot_app, secret, state

    async def post(self):
        supplied = self.request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, self.secret):
            self.set_status(403)
            self.finish({"ok": False})
            return
        if not self.state["ready"]:
            self.set_status(503)
            self.finish({"ok": False})
            return
        try:
            data = json.loads(self.request.body)
            if not isinstance(data, dict) or type(data.get("update_id")) is not int:
                raise ValueError
            update = Update.de_json(data, self.bot_app.bot)
        except (ValueError, TypeError, KeyError):
            self.set_status(400)
            self.finish({"ok": False})
            return
        store = self.bot_app.bot_data["service"].store
        try:
            # Acknowledge only after saving the incoming update. On a DB error
            # Telegram receives 503 and can retry rather than losing the request.
            store.accept_update(update.update_id, data)
        except Exception as exc:
            LOG.error("No se confirmó recepción: %s", type(exc).__name__)
            self.set_status(503)
            self.finish({"ok": False})
            return
        self.bot_app.bot_data["service"].inbox_wakeup.set()
        self.write({"ok": True})


def make_web_app(bot_app, secret: str, state: dict):
    return tornado.web.Application([
        (r"/", Health, {"state": state}),
        (r"/health", Health, {"state": state}),
        (r"/webhook", Webhook, {"bot_app": bot_app, "secret": secret, "state": state}),
    ])


async def serve():
    cfg = Settings.load()
    # A single download at a time fits a small free instance better.
    cfg.max_download_bytes = 250_000_000
    cfg.request_timeout = 600
    public_url, secret = webhook_config(cfg.token)
    bot_app = build_application(cfg, polling=False)
    service = bot_app.bot_data["service"]
    state = {"ready": False}
    server = tornado.httpserver.HTTPServer(
        make_web_app(bot_app, secret, state), max_body_size=1_048_576,
        max_buffer_size=2_097_152, xheaders=True,
    )
    server.listen(int(os.getenv("PORT", "10000")), address="0.0.0.0")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopped.set)
    started = False
    try:
        async with bot_app:
            try:
                await service.startup(bot_app)
                await bot_app.start()
                started = True
                # The HTTP endpoint is listening and the bot is initialized before registration.
                state["ready"] = True
                await bot_app.bot.set_webhook(
                    url=public_url + "/webhook", secret_token=secret,
                    allowed_updates=["message", "channel_post", "my_chat_member", "callback_query"],
                    max_connections=1, drop_pending_updates=False,
                )
                LOG.info("Webhook activo. Health: /health. Acceso abierto a todos los chats privados.")
                await stopped.wait()
            finally:
                state["ready"] = False
                server.stop()
                if started:
                    await bot_app.stop()
                await service.shutdown(bot_app)
    finally:
        server.stop()
    # Leave the webhook registered: the next Telegram request must be able to wake Render.


def main() -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("telegram").setLevel(logging.CRITICAL)
    logging.getLogger("tornado.access").setLevel(logging.WARNING)
    try:
        asyncio.run(serve())
        return 0
    except BotProblem as exc:
        LOG.error("%s", exc)
    except Exception as exc:
        LOG.error("No se pudo iniciar el webhook: %s. Revisa el token, la URL y la conexión.", type(exc).__name__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
