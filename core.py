"""Configuration, safe X links, Telegram captions and persistent archive outbox."""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
URLS = re.compile(
    r"https?://[^\s<>]+|(?<![\w@./-])(?:(?:www|mobile|m)\.)?"
    r"(?:x\.com|twitter\.com)/[^\s<>]+", re.I
)
HOSTS = {p + h for h in ("x.com", "twitter.com") for p in ("", "www.", "mobile.", "m.")}
STATUS = re.compile(r"^/(?:[A-Za-z0-9_]+/status|i/web/status)/([0-9]{1,25})(?:/|$)")


class BotProblem(Exception):
    """An error safe to show to a user."""


def extract_links(text: str) -> list[str]:
    links = []
    for match in URLS.finditer(text or ""):
        candidate = match.group().rstrip(".,;:!?)]}\"'")
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = "https://" + candidate
        try:
            parsed = urlsplit(candidate)
            if parsed.hostname not in HOSTS or parsed.username or parsed.password:
                continue
            if parsed.port not in (None, 80, 443):
                continue
            status = STATUS.match(parsed.path)
            if status:
                canonical = f"https://x.com/i/status/{status[1]}"
                if canonical not in links:
                    links.append(canonical)
        except ValueError:
            continue
    return links


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clip(text: str, limit: int) -> str:
    if units(text) <= limit:
        return text
    return text.encode("utf-16-le")[: max(0, limit - 1) * 2].decode(
        "utf-16-le", errors="ignore"
    ) + "…"


def chunks(text: str, size: int = 3400) -> list[str]:
    result = []
    while text:
        piece = text.encode("utf-16-le")[:size * 2].decode("utf-16-le", errors="ignore")
        result.append(piece)
        text = text[len(piece):]
    return result


def alias_user_id(value: str | int) -> int:
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,19}", text) or not 0 < int(text) <= 2**63 - 1:
        raise BotProblem("Usa el ID personal positivo de Telegram, no un @usuario ni el ID del canal.")
    return int(text)


def normalize_alias(value: str) -> str:
    if not isinstance(value, str):
        raise BotProblem("Cada etiqueta debe ser un texto.")
    label = " ".join(value.split())
    if not label or units(label) > 60:
        raise BotProblem("La etiqueta debe tener entre 1 y 60 caracteres; los emojis pueden ocupar dos.")
    if any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise BotProblem("La etiqueta contiene caracteres de control no admitidos.")
    return label


def parse_aliases(value: str) -> dict[int, str]:
    try:
        raw = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise BotProblem('USER_ALIASES_JSON debe ser un objeto JSON válido, por ejemplo {"123456789":"Jan"}.') from exc
    if not isinstance(raw, dict):
        raise BotProblem("USER_ALIASES_JSON debe ser un objeto con IDs y etiquetas.")
    result = {}
    for key, label in raw.items():
        uid = alias_user_id(key)
        if uid in result:
            raise BotProblem(f"USER_ALIASES_JSON repite el ID {uid}.")
        result[uid] = normalize_alias(label)
    return result


@dataclass
class Settings:
    token: str
    owner_id: int | None = None
    archive_id: int | None = None
    timezone: str = "America/Puerto_Rico"
    photo_mode: str = "original"
    max_download_bytes: int = 600_000_000
    max_upload_bytes: int = 49_000_000
    request_timeout: int = 1200
    data_dir: Path = BASE / "datos"
    aliases: dict[int, str] = field(default_factory=dict)
    aliases_configured: bool = False
    initial_aliases: dict[int, str] = field(default_factory=dict)
    database_url: str = ""

    @classmethod
    def load(cls, path: Path = BASE / ".env") -> "Settings":
        # The local file takes priority over stale hosting environment variables.
        source = {**os.environ, **{k: v for k, v in dotenv_values(path).items() if v is not None}}
        token = source.get("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise BotProblem("Falta un BOT_TOKEN válido. Ejecuta: .venv/bin/python configurar.py")

        def number(key: str) -> int | None:
            value = source.get(key, "").strip()
            if not value:
                return None
            try:
                return int(value)
            except ValueError as exc:
                raise BotProblem(f"{key} debe ser un número entero, sin comillas adicionales.") from exc

        cfg = cls(token=token, owner_id=number("OWNER_ID"), archive_id=number("ARCHIVE_CHAT_ID"))
        cfg.timezone = source.get("TIMEZONE", cfg.timezone)
        try:
            ZoneInfo(cfg.timezone)
        except Exception as exc:
            raise BotProblem("TIMEZONE no es una zona válida; usa America/Puerto_Rico.") from exc
        cfg.photo_mode = source.get("PHOTO_MODE", "original").lower()
        if cfg.photo_mode not in ("original", "foto"):
            raise BotProblem("PHOTO_MODE debe ser original o foto.")
        if cfg.owner_id is not None and cfg.owner_id <= 0:
            raise BotProblem("OWNER_ID debe ser tu ID personal positivo, no el ID del canal.")
        if cfg.archive_id is not None and cfg.archive_id >= 0:
            raise BotProblem("ARCHIVE_CHAT_ID debe ser el ID negativo de un canal.")
        aliases_json = source.get("USER_ALIASES_JSON", "").strip()
        cfg.aliases_configured = bool(aliases_json)
        if cfg.aliases_configured:
            cfg.aliases = parse_aliases(aliases_json)
        cfg.database_url = source.get("DATABASE_URL", "").strip()
        if cfg.database_url and not cfg.database_url.startswith(("postgres://", "postgresql://")):
            raise BotProblem("DATABASE_URL debe ser una conexión PostgreSQL válida.")
        initial = BASE / "usuarios_iniciales.json"
        if initial.is_file():
            cfg.initial_aliases = parse_aliases(initial.read_text(encoding="utf-8"))
        return cfg


@dataclass
class Requester:
    id: int
    name: str
    username: str | None
    requested_at: datetime


def caption(post: dict, label: str, cfg: Settings, requester: Requester | None = None,
            completed_at: datetime | None = None, requester_alias: str | None = None,
            code: str | None = None) -> tuple[str, bool]:
    from formatting import format_caption
    return format_caption(post, label, cfg, requester, completed_at, requester_alias, code)


def full_text(post: dict) -> list[str]:
    from formatting import text_parts
    return text_parts(post)


from storage import Store
