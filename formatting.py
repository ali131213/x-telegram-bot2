"""Separate public delivery text from the archive's administrative metadata."""
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core import chunks, clip, units


def compact_label(label):
    if re.fullmatch(r"\([0-9]+/[0-9]+\)", label or ""):
        return label
    matches = re.findall(r"([0-9]+)/([0-9]+)", label or "")
    return f"({matches[-1][0]}/{matches[-1][1]})" if matches else "(1/1)"


def identity(requester, cfg, fixed_label=None, completed_at=None, code=None, category=None):
    lines = []
    if fixed_label:
        lines.append("Identificación: " + clip(fixed_label, 60))
    else:
        lines.append("🆕 SOLICITANTE NO ENLISTADO")
    profile = clip(requester.name or "Sin nombre visible", 75)
    profile += f" (@{clip(requester.username,40)})" if requester.username else " (sin @usuario)"
    zone = ZoneInfo(cfg.timezone)
    requested = requester.requested_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
    lines.extend([f"Solicitante: {profile}", f"ID: {requester.id}", f"Solicitud: {requested}"])
    if completed_at:
        lines.append("Descarga: " + completed_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("Zona: " + cfg.timezone)
    if category:
        lines.append("Tipo: " + category)
    if code:
        lines.append("Código: " + code)
    return "\n".join(lines)


def format_caption(post, label, cfg, requester=None, completed_at=None, requester_alias=None, code=None):
    number = compact_label(label)
    text = str(post.get("text") or "")
    if requester is None:
        # Only original post text plus the compact ordinal. No added author or URL.
        budget = 980 - units("\n\n" + number)
        body = clip(text, budget)
        rendered = (f"<b>{html.escape(body)}</b>\n\n" if body else "") + number
        return rendered, units(text) > budget
    name = clip(str(post.get("author") or "Autor no disponible"), 140)
    handle = clip(str(post.get("handle") or "").lstrip("@"), 50)
    author = name + (f" (@{handle})" if handle else "")
    suffix = (
        f"\n\n{number}\n{post['url']}\n\n"
        + identity(requester, cfg, requester_alias, completed_at, code, "Descarga de X")
    )
    budget = max(0, 980 - units(author + "\n\n" + suffix))
    body = clip(text, budget) if budget > 1 else ""
    rendered = f"<b>{html.escape(author)}</b>"
    if body:
        rendered += f"\n\n<b>{html.escape(body)}</b>"
    return rendered + html.escape(suffix), units(text) > budget


def text_parts(post):
    return [f"<b>{html.escape(piece)}</b>" for piece in chunks(str(post.get("text") or ""))]
