import asyncio
import base64
import binascii
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from faster_whisper import WhisperModel

from config import settings

logger = logging.getLogger(__name__)

TMP_DIR = Path("/tmp")

_model: WhisperModel | None = None


class DownloadError(Exception):
    """yt-dlp could not download the video (private, geo-blocked, bad URL...)."""


@dataclass
class Transcript:
    title: str
    duration: int  # seconds, 0 if unknown
    segments: list[tuple[int, str]]  # (start second, text)


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        logger.info("Loading faster-whisper model 'small' (int8)...")
        _model = WhisperModel("small", device="cpu", compute_type="int8")
        logger.info("Whisper model loaded")
    return _model


def _cookie_file() -> str | None:
    raw = settings.ytdlp_cookies.strip()
    if not raw:
        return None
    # Netscape cookies.txt is tab-separated; no tabs means the value is
    # base64-encoded (safer to paste into an env var without mangling).
    if "\t" not in raw:
        try:
            raw = base64.b64decode(raw).decode()
        except (binascii.Error, UnicodeDecodeError):
            logger.warning("YTDLP_COOKIES is neither cookies.txt content nor valid base64, ignoring")
            return None
    path = TMP_DIR / "ytdlp_cookies.txt"
    if not path.exists():
        path.write_text(raw)
    return str(path)


def _download(url: str, file_id: str) -> tuple[Path, dict]:
    outtmpl = str(TMP_DIR / f"{file_id}.%(ext)s")
    opts = {
        # pure audio if available, else anything containing an audio track,
        # else whatever there is — the postprocessor extracts audio anyway
        "format": "bestaudio/bestaudio*/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 500 * 1024 * 1024,
        # always hand whisper a clean audio file; fails loudly here (instead
        # of deep inside whisper's decoder) when the video has no sound
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
    }
    cookies = _cookie_file()
    if cookies:
        opts["cookiefile"] = cookies
    elif "instagram.com" in url:
        logger.warning("Instagram URL without YTDLP_COOKIES set — download will likely be rate-limited")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    # YoutubeDLError covers both download and postprocessing (ffmpeg) failures
    except yt_dlp.utils.YoutubeDLError as e:
        raise DownloadError(str(e)) from e

    files = list(TMP_DIR.glob(f"{file_id}.*"))
    if not files:
        raise DownloadError("Download finished but no media file was produced")
    # prefer the extracted audio if the original somehow survived alongside it
    files.sort(key=lambda f: f.suffix != ".m4a")
    return files[0], info or {}


def _transcribe_file(path: Path) -> list[tuple[int, str]]:
    model = _get_model()
    logger.info("Transcribing %s...", path.name)
    started = time.monotonic()
    # vad_filter skips silence, which speeds things up and reduces hallucinations
    segments, _info = model.transcribe(str(path), language="ru", vad_filter=True)
    result = []
    for segment in segments:  # generator: transcription happens while iterating
        text = segment.text.strip()
        if text:
            result.append((int(segment.start), text))
    logger.info("Transcribed %s in %.1fs", path.name, time.monotonic() - started)
    return result


async def transcribe_url(url: str) -> Transcript:
    """Download the video and return a timestamped transcript. Raises
    DownloadError on download failures. Temp files are always removed."""
    loop = asyncio.get_event_loop()
    file_id = uuid.uuid4().hex
    try:
        path, info = await loop.run_in_executor(None, _download, url, file_id)
        logger.info("Downloaded %s -> %s", url, path.name)
        segments = await loop.run_in_executor(None, _transcribe_file, path)
        return Transcript(
            title=info.get("title") or "Видео",
            duration=int(info.get("duration") or 0),
            segments=segments,
        )
    finally:
        for leftover in TMP_DIR.glob(f"{file_id}.*"):
            leftover.unlink(missing_ok=True)
