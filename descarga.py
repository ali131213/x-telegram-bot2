"""Isolated download process. Public posts only; no X login or paid API."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import yt_dlp

from core import BotProblem, extract_links

USER_AGENT = "XTelegramArchive/1.0 (small personal downloader)"


def validate_cdn(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not (host == "twimg.com" or host.endswith(".twimg.com"))
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise BotProblem("El enlace multimedia no pertenece al CDN de X.")
    return url


def original_photo(url: str) -> str:
    validate_cdn(url)
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))
    query["name"] = "orig"
    return urlunsplit(parsed._replace(query=urlencode(query)))


def fetch_file(client: httpx.Client, url: str, path: Path, maximum: int):
    current = validate_cdn(url)
    for _ in range(4):
        with client.stream("GET", current) as response:
            if response.is_redirect:
                current = validate_cdn(urljoin(current, response.headers["location"]))
                continue
            response.raise_for_status()
            if int(response.headers.get("content-length", "0")) > maximum:
                raise BotProblem(f"Este archivo supera el límite de descarga de {maximum // 1_000_000} MB.")
            size = 0
            with path.open("wb") as output:
                for data in response.iter_bytes(128 * 1024):
                    size += len(data)
                    if size > maximum:
                        raise BotProblem(f"Este archivo supera el límite de descarga de {maximum // 1_000_000} MB.")
                    output.write(data)
            if not size:
                raise BotProblem("X devolvió un archivo vacío.")
            return
    raise BotProblem("Demasiadas redirecciones al descargar desde X.")


def video_variants(item: dict) -> list[str]:
    variants = item.get("variants") or item.get("formats") or []
    mp4s = [
        v for v in variants
        if ".mp4" in str(v.get("url", "")) or v.get("content_type") == "video/mp4"
    ]
    mp4s.sort(key=lambda v: (int(v.get("bitrate") or 0), int(v.get("height") or 0)), reverse=True)
    result = [v["url"] for v in mp4s if v.get("url")]
    if item.get("url") and item["url"] not in result:
        result.append(item["url"])
    return result


def fx_download(url: str, folder: Path, maximum: int) -> dict:
    post_id = url.rsplit("/", 1)[-1]
    with httpx.Client(timeout=httpx.Timeout(45, connect=15), follow_redirects=False,
                      headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(f"https://api.fxtwitter.com/status/{post_id}")
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 200 or not isinstance(result.get("tweet"), dict):
            raise BotProblem("La publicación no está disponible públicamente.")
        tweet = result["tweet"]
        author = tweet.get("author") or {}
        media = tweet.get("media") or {}
        items = media.get("all") or (media.get("photos") or []) + (media.get("videos") or [])
        if not items:
            raise BotProblem("Esta publicación no contiene fotos ni videos descargables.")
        if len(items) > 20:
            raise BotProblem("La publicación contiene más de 20 archivos; no se descargó parcialmente.")
        post = {
            "url": url, "author": author.get("name"), "handle": author.get("screen_name"),
            "text": tweet.get("text") or "", "media": [], "provider": "FxTwitter",
        }
        failures = []
        for index, item in enumerate(items, 1):
            kind = item.get("type")
            if kind == "photo":
                photo_url = original_photo(item["url"])
                extension = dict(parse_qsl(urlsplit(photo_url).query)).get("format")
                extension = extension or Path(urlsplit(photo_url).path).suffix.lstrip(".") or "jpg"
                if extension not in ("jpg", "jpeg", "png", "webp"):
                    extension = "jpg"
                path = folder / f"foto_{index:02d}.{extension}"
                try:
                    fetch_file(client, photo_url, path, maximum)
                    post["media"].append({"path": str(path), "kind": "photo", "label": f"Foto {index}/{len(items)}"})
                except Exception:
                    failures.append(f"Foto {index}: no se pudo descargar el original.")
            elif kind in ("video", "gif", "animated_gif"):
                path = folder / f"video_{index:02d}.mp4"
                for variant in video_variants(item):
                    try:
                        fetch_file(client, variant, path, maximum)
                        # A playlist is handled by yt-dlp, not mislabelled as MP4.
                        with path.open("rb") as check:
                            is_playlist = check.read(7).startswith(b"#EXTM3U")
                        if is_playlist:
                            raise BotProblem("El video utiliza una lista HLS.")
                        post["media"].append({"path": str(path), "kind": "video", "label": f"Video {index}/{len(items)}"})
                        break
                    except Exception:
                        path.unlink(missing_ok=True)
                else:
                    failures.append(f"Video {index}: no se pudo descargar.")
            else:
                failures.append(f"Archivo {index}: formato no compatible.")
        if not post["media"]:
            raise BotProblem("No se pudo descargar el contenido de esta publicación.")
        post["warnings"] = failures
        return post


class QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def ytdlp_download(url: str, folder: Path, maximum: int) -> dict:
    """Backup for public videos when the public metadata service is unavailable."""
    issues = []
    for api in ("graphql", "syndication"):
        attempt = folder / api
        attempt.mkdir(exist_ok=True)
        downloaded = []

        def completed(data):
            if data.get("status") == "finished":
                downloaded.append(data["filename"])

        def progress(data):
            if max(data.get("downloaded_bytes") or 0, data.get("total_bytes") or 0) > maximum:
                raise BotProblem(f"El video supera el límite de descarga de {maximum // 1_000_000} MB.")

        options = {
            "format": "best[ext=mp4]/best",
            "outtmpl": str(attempt / "%(id)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "logger": QuietLogger(),
            "socket_timeout": 25, "retries": 2, "fragment_retries": 2,
            "max_filesize": maximum, "max_downloads": 20, "noplaylist": False,
            "extractor_args": {"twitter": {"api": [api]}},
            "progress_hooks": [progress], "postprocessor_hooks": [completed],
            "cachedir": False,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
            entries = (info.get("entries") or [info]) if info else []
            entries = [e for e in entries if e]
            files = [
                p for p in attempt.iterdir()
                if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
            ]
            if not files:
                raise BotProblem("No se obtuvo un video público.")
            first = entries[0] if entries else info
            return {
                "url": url,
                "author": info.get("uploader") or first.get("uploader"),
                "handle": info.get("uploader_id") or first.get("uploader_id"),
                "text": info.get("description") or first.get("description") or "",
                "media": [
                    {"path": str(p), "kind": "video", "label": f"Video {i}/{len(files)}"}
                    for i, p in enumerate(sorted(files), 1)
                ],
                "provider": f"yt-dlp/{api}",
                "warnings": ["Se descargaron los videos, pero no se pudo comprobar si había fotos adicionales."],
            }
        except Exception as exc:
            issues.append(type(exc).__name__)
            shutil.rmtree(attempt, ignore_errors=True)
    raise BotProblem(
        "X no permitió obtener este contenido público. Puede estar eliminado, ser privado, "
        "requerir iniciar sesión o existir un bloqueo temporal. Prueba otro enlace o inténtalo más tarde."
    )


def probe(path: Path) -> dict:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, timeout=40,
    )
    data = json.loads(completed.stdout)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not stream:
        raise BotProblem("El archivo obtenido no contiene una pista de video.")
    return {
        "width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0),
        "duration": float(data.get("format", {}).get("duration") or stream.get("duration") or 0),
        "codec": stream.get("codec_name"),
    }


def split_video(path: Path, maximum: int) -> list[Path]:
    """Split at keyframes without re-encoding. Check actual bytes of every part."""
    if path.stat().st_size <= maximum:
        return [path]
    duration = probe(path)["duration"]
    if duration <= 0:
        raise BotProblem("No se pudo medir la duración para dividir el video.")
    segment_time = max(0.25, duration * maximum / path.stat().st_size * 0.72)
    target = path.parent / (path.stem + "_partes")
    for _ in range(7):
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir()
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
             "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "segment",
             "-segment_time", f"{segment_time:.3f}", "-reset_timestamps", "1",
             "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
             str(target / "parte_%03d.mp4")],
            check=True, capture_output=True, timeout=240,
        )
        parts = sorted(target.glob("parte_*.mp4"))
        if parts and all(0 < p.stat().st_size <= maximum for p in parts):
            if len(parts) > 80:
                raise BotProblem("El video requiere más de 80 partes; es demasiado grande para este bot.")
            return parts
        segment_time /= 2
    raise BotProblem(
        "No se pudo dividir este video dentro del límite de Telegram sin recomprimirlo. "
        "El bot no envió un archivo incompleto."
    )


def download(url: str, folder: Path, maximum: int, upload_limit: int) -> dict:
    links = extract_links(url)
    if len(links) != 1:
        raise BotProblem("Envía un enlace válido a una publicación de X.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise BotProblem("Falta FFmpeg. En tu Mac ejecuta: brew install ffmpeg")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        post = fx_download(links[0], folder, maximum)
    except Exception:
        post = ytdlp_download(links[0], folder, maximum)
    prepared, warnings = [], list(post.get("warnings") or [])
    for media in post["media"]:
        path = Path(media["path"])
        try:
            if media["kind"] == "photo":
                if path.stat().st_size > upload_limit:
                    raise BotProblem(f"{media['label']}: la imagen original supera los 49 MB.")
                prepared.append(media)
                continue
            parts = split_video(path, upload_limit)
            for number, part in enumerate(parts, 1):
                attributes = probe(part)
                label = media["label"]
                if len(parts) > 1:
                    label += f" · Parte {number}/{len(parts)} · sin recomprimir"
                prepared.append({**media, "path": str(part), "label": label, **attributes})
        except BotProblem as exc:
            warnings.append(str(exc))
        except Exception:
            warnings.append(f"{media['label']}: no se pudo preparar el archivo para Telegram.")
    if not prepared:
        raise BotProblem("No se pudo preparar ningún archivo. " + " ".join(warnings))
    for i, media in enumerate(prepared, 1):
        # One simple sequence across all delivered attachments and split parts.
        media["label"] = f"({i}/{len(prepared)})"
    post["media"] = prepared
    post["warnings"] = warnings
    return post


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--maximum", type=int, default=600_000_000)
    parser.add_argument("--upload-limit", type=int, default=49_000_000)
    args = parser.parse_args()
    try:
        result = {"ok": True, "post": download(args.url, args.folder, args.maximum, args.upload_limit)}
    except BotProblem as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception:
        result = {"ok": False, "error": "No se pudo descargar el contenido. Prueba otro enlace público."}
    args.folder.mkdir(parents=True, exist_ok=True)
    (args.folder / "resultado.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
