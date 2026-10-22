"""Optional interactive setup for a VM or local terminal. Render uses Environment."""
from __future__ import annotations

import asyncio
import getpass
import os

from dotenv import dotenv_values, set_key
from telegram import Bot
from telegram.error import TelegramError

from core import BASE


async def main():
    path = BASE / ".env"
    previous = dotenv_values(path)
    print("Configuración opcional por terminal. En Render usa Environment, sin ejecutar este asistente.")
    print("El token se oculta al escribir; no lo compartas.")
    token = getpass.getpass("BOT_TOKEN (Enter conserva el actual): ").strip() or previous.get("BOT_TOKEN", "")
    try:
        async with Bot(token=token) as bot:
            account = await bot.get_me()
    except (TelegramError, ValueError):
        print("No pude validar el token. Revisa lo que copiaste de BotFather y tu conexión.")
        return 1
    print(f"Token válido para @{account.username}.")
    owner = input(f"OWNER_ID personal [actual: {previous.get('OWNER_ID') or 'vacío'}]: ").strip()
    owner = owner or previous.get("OWNER_ID") or ""
    archive = input(f"ARCHIVE_CHAT_ID [actual: {previous.get('ARCHIVE_CHAT_ID') or 'vacío'}]: ").strip()
    archive = archive or previous.get("ARCHIVE_CHAT_ID") or ""
    if owner and (not owner.isdigit() or int(owner) <= 0):
        print("OWNER_ID debe ser tu ID personal positivo. Usa /id en el bot para obtenerlo.")
        return 1
    if archive and (not archive.lstrip("-").isdigit() or int(archive) >= 0):
        print("El canal debe tener un ID negativo. Usa /canales para obtenerlo.")
        return 1
    path.touch(exist_ok=True)
    for key, value in {
        "BOT_TOKEN": token, "OWNER_ID": owner, "ARCHIVE_CHAT_ID": archive,
        "TIMEZONE": "America/Puerto_Rico", "PHOTO_MODE": previous.get("PHOTO_MODE") or "original",
    }.items():
        set_key(str(path), key, value)
    if os.name == "posix":
        path.chmod(0o600)
    print("Configuración guardada. Para modo terminal: python app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
