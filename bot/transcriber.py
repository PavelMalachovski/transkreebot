import asyncio
import base64
import binascii
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from faster_whisper import WhisperModel

from config import settings

logger = logging.getLogger(__name__)

TMP_DIR = Path("/tmp")

_model: WhisperModel | None = None

# Single worker: transcriptions run strictly one at a time. Parallel whisper
# runs thrash the CPU (everyone gets slower) and multiply peak RAM usage.
_executor = ThreadPoolExecutor(max_workers=1)

# jobs submitted and not yet finished; mutated only from the event loop thread
queue_size = 0

ProgressCallback = Callable[[str], Awaitable[None]]


class DownloadError(Exception):
    """yt-dlp could not download the video (private, geo-blocked, bad URL...)."""


class VideoTooLongError(Exception):
    def __init__(self, duration: int, limit: int):
        super().__init__(f"video is {duration}s, limit is {limit}s")
        self.duration = duration
        self.limit = limit


@dataclass
class Transcript:
    title: str
    duration: int  # seconds, 0 if unknown
    segments: list[tuple[float, float, str]]  # (start, end, text)


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


def _download(url: str, file_id: str, max_duration: int | None) -> tuple[Path, dict]:
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
    if max_duration:
        # rejects during extraction, before any bytes are downloaded
        opts["match_filter"] = yt_dlp.utils.match_filter_func(f"duration <= {max_duration}")
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

    info = info or {}
    files = list(TMP_DIR.glob(f"{file_id}.*"))
    if not files:
        duration = int(info.get("duration") or 0)
        if max_duration and duration > max_duration:
            raise VideoTooLongError(duration, max_duration)
        raise DownloadError("Download finished but no media file was produced")
    # prefer the extracted audio if the original somehow survived alongside it
    files.sort(key=lambda f: f.suffix != ".m4a")
    return files[0], info


def _transcribe_file(path: Path) -> list[tuple[float, float, str]]:
    model = _get_model()
    logger.info("Transcribing %s...", path.name)
    started = time.monotonic()
    # no language set: whisper auto-detects and transcribes in the original;
    # vad_filter skips silence, which speeds things up and reduces hallucinations
    segments, info = model.transcribe(str(path), vad_filter=True)
    logger.info(
        "Detected language: %s (probability %.2f)",
        info.language,
        info.language_probability,
    )
    result = []
    for segment in segments:  # generator: transcription happens while iterating
        text = segment.text.strip()
        if text:
            result.append((float(segment.start), float(segment.end), text))
    logger.info("Transcribed %s in %.1fs", path.name, time.monotonic() - started)
    return result


async def transcribe_url(
    url: str,
    max_duration: int | None = None,
    progress: ProgressCallback | None = None,
) -> Transcript:
    """Download the video and return a timestamped transcript. Raises
    DownloadError / VideoTooLongError on failures. Temp files are always
    removed. Jobs are processed strictly one at a time."""
    global queue_size
    loop = asyncio.get_running_loop()
    file_id = uuid.uuid4().hex
    ahead = queue_size
    queue_size += 1
    try:
        if progress and ahead:
            await progress(f"⏳ В очереди — впереди видео: {ahead}. Начну, как только освобожусь...")
        path, info = await loop.run_in_executor(_executor, _download, url, file_id, max_duration)
        logger.info("Downloaded %s -> %s", url, path.name)
        duration = int(info.get("duration") or 0)
        if progress:
            note = f" ({duration // 60}:{duration % 60:02d})" if duration else ""
            await progress(f"🎙 Расшифровываю{note}...")
        segments = await loop.run_in_executor(_executor, _transcribe_file, path)
        return Transcript(
            title=info.get("title") or "Видео",
            duration=duration,
            segments=segments,
        )
    finally:
        queue_size -= 1
        for leftover in TMP_DIR.glob(f"{file_id}.*"):
            leftover.unlink(missing_ok=True)
