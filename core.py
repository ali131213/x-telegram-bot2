"""Configuration, safe X links, Telegram captions and persistent archive outbox."""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
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
        return cfg


@dataclass
class Requester:
    id: int
    name: str
    username: str | None
    requested_at: datetime


def caption(post: dict, label: str, cfg: Settings, requester: Requester | None = None,
            completed_at: datetime | None = None) -> tuple[str, bool]:
    """Keep BOTH captions within Telegram's limit, including astral emoji."""
    name = clip(str(post.get("author") or "Autor no disponible"), 140)
    handle = clip(str(post.get("handle") or ""), 50).lstrip("@")
    author = name + (f" (@{handle})" if handle else "")
    source = post["url"]
    suffix = f"\n\n{label}\n{source}"
    if requester:
        zone = ZoneInfo(cfg.timezone)
        who = clip(requester.name, 75)
        who += f" (@{clip(requester.username, 40)})" if requester.username else " (sin @usuario)"
        requested = requester.requested_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
        finished = (completed_at or requester.requested_at).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
        suffix += (
            f"\n\nSolicitante: {who}\nID: {requester.id}"
            f"\nSolicitud: {requested}\nDescarga: {finished}\nZona: {cfg.timezone}"
        )
    original = str(post.get("text") or "")
    available = max(20, 980 - units(author + "\n\n" + suffix))
    body = clip(original or "(Publicación sin texto)", available)
    rendered = f"<b>{html.escape(author)}</b>\n\n<b>{html.escape(body)}</b>{html.escape(suffix)}"
    return rendered, units(original) > available


def full_text(post: dict) -> list[str]:
    pieces = chunks(str(post.get("text") or ""))
    return [
        f"<b>Texto completo ({i}/{len(pieces)})</b>\n\n<b>{html.escape(piece)}</b>"
        for i, piece in enumerate(pieces, 1)
    ]


class Store:
    """Only Telegram file IDs and captions are kept; downloaded bytes are temporary."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY, received_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, kind TEXT NOT NULL,
                file_id TEXT, body TEXT NOT NULL, extra TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0
            );
        """)

    def remember_channel(self, chat_id: int, title: str):
        self.db.execute("INSERT OR REPLACE INTO channels VALUES (?,?)", (chat_id, title))
        self.db.commit()

    def seen_update(self, update_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM updates WHERE id=?", (update_id,)).fetchone() is not None

    def mark_update(self, update_id: int):
        self.db.execute("INSERT OR IGNORE INTO updates VALUES (?,?)", (update_id, time.time()))
        self.db.execute("DELETE FROM updates WHERE received_at<?", (time.time() - 172800,))
        self.db.commit()

    def channels(self):
        return self.db.execute("SELECT * FROM channels ORDER BY title").fetchall()

    def enqueue(self, chat_id: int, kind: str, file_id: str | None, body: str, extra=None):
        self.db.execute(
            "INSERT INTO outbox(chat_id,kind,file_id,body,extra) VALUES (?,?,?,?,?)",
            (chat_id, kind, file_id, body, json.dumps(extra or {}))
        )
        self.db.commit()

    def next_job(self):
        # Preserve order within each channel, even when an earlier item is retrying.
        return self.db.execute("""
            SELECT * FROM outbox o WHERE next_at<=? AND NOT EXISTS(
                SELECT 1 FROM outbox p WHERE p.chat_id=o.chat_id AND p.id<o.id
            ) ORDER BY id LIMIT 1
        """, (time.time(),)).fetchone()

    def done(self, job_id: int):
        self.db.execute("DELETE FROM outbox WHERE id=?", (job_id,))
        self.db.commit()

    def retry(self, job_id: int, delay: float):
        self.db.execute(
            "UPDATE outbox SET attempts=attempts+1,next_at=? WHERE id=?",
            (time.time() + delay, job_id)
        )
        self.db.commit()

    def pending(self) -> int:
        return self.db.execute("SELECT count(*) FROM outbox").fetchone()[0]

    def close(self):
        self.db.close()
