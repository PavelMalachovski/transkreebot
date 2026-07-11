import asyncio
import logging
import uuid
from pathlib import Path

import whisper
import yt_dlp

logger = logging.getLogger(__name__)

TMP_DIR = Path("/tmp")

_model: whisper.Whisper | None = None


class DownloadError(Exception):
    """yt-dlp could not download the video (private, geo-blocked, bad URL...)."""


def _get_model() -> whisper.Whisper:
    global _model
    if _model is None:
        logger.info("Loading whisper model 'small' (first request only)...")
        _model = whisper.load_model("small")
        logger.info("Whisper model loaded")
    return _model


def _download(url: str, file_id: str) -> Path:
    outtmpl = str(TMP_DIR / f"{file_id}.%(ext)s")
    opts = {
        # audio is all whisper needs; skips huge video streams
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 500 * 1024 * 1024,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(str(e)) from e

    files = list(TMP_DIR.glob(f"{file_id}.*"))
    if not files:
        raise DownloadError("Download finished but no media file was produced")
    return files[0]


def _transcribe_file(path: Path) -> str:
    model = _get_model()
    result = model.transcribe(str(path), language="ru")
    lines = []
    for segment in result["segments"]:
        text = segment["text"].strip()
        if text:
            lines.append(f"[{int(segment['start'])}] {text}")
    return "\n".join(lines)


async def transcribe_url(url: str) -> str:
    """Download the video and return timestamped text. Raises DownloadError on
    download failures. Temp files are always removed."""
    loop = asyncio.get_event_loop()
    file_id = uuid.uuid4().hex
    try:
        path = await loop.run_in_executor(None, _download, url, file_id)
        logger.info("Downloaded %s -> %s", url, path.name)
        return await loop.run_in_executor(None, _transcribe_file, path)
    finally:
        for leftover in TMP_DIR.glob(f"{file_id}.*"):
            leftover.unlink(missing_ok=True)
