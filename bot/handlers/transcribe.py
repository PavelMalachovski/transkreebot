import hashlib
import html
import json
import logging
import re
import time
import uuid
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
import transcriber
from config import settings

logger = logging.getLogger(__name__)

router = Router(name="transcribe")

URL_RE = re.compile(r"https?://\S+")

# Telegram message hard limit is 4096 chars; leave headroom.
CHUNK_SIZE = 4000

# "Processing" messages of in-flight jobs, edited on shutdown so users
# aren't left staring at a stuck status after a redeploy.
_active_statuses: set[Message] = set()

# consecutive Instagram download failures; 3 in a row usually means the
# cookies expired, so alert the admins once
_insta_failures = 0
INSTA_ALERT_THRESHOLD = 3

# anti-spam: one user may only have this many jobs in flight at once
MAX_USER_JOBS = 2
_user_jobs: dict[int, int] = {}


def _too_many_jobs(user_id: int) -> bool:
    return _user_jobs.get(user_id, 0) >= MAX_USER_JOBS


def _job_started(user_id: int) -> None:
    _user_jobs[user_id] = _user_jobs.get(user_id, 0) + 1


def _job_finished(user_id: int) -> None:
    left = _user_jobs.get(user_id, 1) - 1
    if left > 0:
        _user_jobs[user_id] = left
    else:
        _user_jobs.pop(user_id, None)


async def _check_quota(message: Message, user) -> tuple[bool, bool]:
    """Returns (unlimited, allowed); sends the denial message itself."""
    unlimited = (
        db.has_active_subscription(user)
        or message.from_user.id in settings.free_user_id_set
    )
    if not unlimited and user["free_videos_used"] >= settings.free_video_limit:
        renew_note = ""
        if user["free_week_start"] is not None:
            renew_date = user["free_week_start"] + timedelta(days=7)
            renew_note = f"New free videos on {renew_date.strftime('%d.%m.%Y')}. "
        await message.answer(
            f"You've reached the weekly free limit ({settings.free_video_limit} videos). 😔\n"
            f"{renew_note}Or subscribe for unlimited transcriptions: /subscribe"
        )
        return unlimited, False
    return unlimited, True


async def notify_restart(**kwargs) -> None:
    for status in list(_active_statuses):
        try:
            await status.edit_text("♻️ The bot is updating. Please send the link again in a minute 🙏")
        except Exception:
            pass


def cache_key(url: str) -> str:
    parts = urlsplit(url)
    # Instagram/TikTok share links carry per-share query junk (?igsh=...);
    # YouTube needs its query (watch?v=...), so only strip for the former
    if "instagram.com" in parts.netloc or "tiktok.com" in parts.netloc:
        url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def format_timestamp(seconds: float) -> str:
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_transcript(t: transcriber.Transcript) -> str:
    header = f"🎬 <b>{html.escape(t.title)}</b>"
    if t.duration:
        header += f" · {format_timestamp(t.duration)}"
    lines = [
        f"<b>{format_timestamp(start)}</b>  {html.escape(text)}"
        for start, _end, text in t.segments
    ]
    return header + "\n\n" + "\n".join(lines)


def to_txt(segments: list) -> str:
    return "\n".join(f"[{format_timestamp(s)}] {text}" for s, _e, text in segments)


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(segments: list) -> str:
    blocks = [
        f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(segments, 1)
    ]
    return "\n".join(blocks)


def export_keyboard(url_hash: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📄 Download .txt", callback_data=f"exp:txt:{url_hash}"),
        InlineKeyboardButton(text="🎬 Subtitles .srt", callback_data=f"exp:srt:{url_hash}"),
    ]])


def split_into_chunks(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Split by lines so a segment (and its HTML tags) is never cut in half."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        # +1 for the newline
        if current and current_len + len(line) + 1 > size:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _alert_admins_cookies(message: Message) -> None:
    for admin_id in settings.admin_id_set:
        try:
            await message.bot.send_message(
                admin_id,
                f"⚠️ {INSTA_ALERT_THRESHOLD} Instagram downloads failed in a row — "
                "the cookies may have expired (YTDLP_COOKIES on Railway).",
            )
        except Exception:
            logger.exception("Failed to alert admin %s", admin_id)


@router.message(F.text.regexp(URL_RE.pattern))
async def handle_url(message: Message) -> None:
    url = URL_RE.search(message.text).group(0)
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    unlimited, allowed = await _check_quota(message, user)
    if not allowed:
        return

    url_hash = cache_key(url)

    cached = await db.get_cached_transcript(url_hash)
    if cached is not None:
        transcript = transcriber.Transcript(
            title=cached["title"],
            duration=cached["duration"],
            segments=[tuple(seg) for seg in json.loads(cached["segments"])],
        )
        await db.log_request(
            message.from_user.id, url, "cached", video_duration=transcript.duration
        )
        await _send_transcript(message, None, transcript, url_hash, user, unlimited)
        return

    if _too_many_jobs(message.from_user.id):
        await message.answer(
            f"You already have {MAX_USER_JOBS} videos processing — "
            "please wait for them to finish. 🙏"
        )
        return

    _job_started(message.from_user.id)
    try:
        await _process_url(message, user, unlimited, url, url_hash)
    finally:
        _job_finished(message.from_user.id)


async def _process_url(
    message: Message, user, unlimited: bool, url: str, url_hash: str
) -> None:
    global _insta_failures

    status = await message.answer("⬇️ Downloading the video...")
    _active_statuses.add(status)

    async def progress(text: str) -> None:
        try:
            await status.edit_text(text)
        except TelegramBadRequest:
            pass  # e.g. text identical to the current one

    max_duration = settings.sub_max_duration if unlimited else settings.free_max_duration
    priority = transcriber.PRIORITY_SUBSCRIBER if unlimited else transcriber.PRIORITY_FREE
    started = time.monotonic()
    try:
        transcript = await transcriber.transcribe_url(url, max_duration, progress, priority)
    except transcriber.VideoTooLongError as e:
        limit_note = (
            "" if unlimited
            else f"\nSubscribers get a higher limit — {settings.sub_max_duration // 60} minutes: /subscribe"
        )
        await status.edit_text(
            f"This video is too long ({format_timestamp(e.duration)}). "
            f"The current limit is {e.limit // 60} minutes.{limit_note}"
        )
        await db.log_request(message.from_user.id, url, "too_long", video_duration=e.duration)
        return
    except transcriber.DownloadError as e:
        logger.exception("Download failed for %s (user %s)", url, message.from_user.id)
        if "DRM" in str(e):
            await status.edit_text(
                "This video is DRM protected — it can't be downloaded and transcribed. 😕"
            )
        elif "Sign in to confirm" in str(e):
            await status.edit_text(
                "YouTube didn't serve this video on the first try (anti-bot check). 😕\n"
                "Send the link again in a minute or two — it usually works."
            )
        elif "status code 10240" in str(e):
            await status.edit_text(
                "TikTok says this video is unavailable — it was removed, is private, "
                "or is blocked in the server's region. 😕"
            )
        else:
            await status.edit_text(
                "Couldn't download this video. 😕\n"
                "Make sure the link works, the video isn't private or deleted, "
                "and it's from YouTube, Instagram or TikTok. "
                "It's also possible the video has no audio track.\n"
                "Instagram sometimes blocks downloads — if so, try again later."
            )
        await db.log_request(message.from_user.id, url, "download_error")
        if "instagram.com" in url:
            _insta_failures += 1
            if _insta_failures == INSTA_ALERT_THRESHOLD:
                await _alert_admins_cookies(message)
        return
    except Exception:
        logger.exception("Transcription failed for %s (user %s)", url, message.from_user.id)
        await status.edit_text("Something went wrong during transcription. Please try again later. 🙏")
        await db.log_request(message.from_user.id, url, "error")
        return
    finally:
        _active_statuses.discard(status)

    if "instagram.com" in url:
        _insta_failures = 0

    elapsed = time.monotonic() - started
    await db.log_request(
        message.from_user.id, url, "ok",
        video_duration=transcript.duration, processing_seconds=elapsed,
    )

    if not transcript.segments:
        await status.edit_text("No speech found in this video — the transcript is empty. 🤷")
        return

    await db.cache_transcript(
        url_hash, url, transcript.title, transcript.duration, transcript.segments
    )
    await _send_transcript(message, status, transcript, url_hash, user, unlimited)


async def _send_transcript(
    message: Message,
    status: Message | None,
    transcript: transcriber.Transcript,
    url_hash: str,
    user,
    unlimited: bool,
) -> None:
    # Quota is spent only on successfully delivered transcripts.
    if not unlimited:
        await db.increment_free_videos(message.from_user.id)

    chunks = split_into_chunks(format_transcript(transcript))
    keyboard = export_keyboard(url_hash)
    for i, chunk in enumerate(chunks):
        markup = keyboard if i == len(chunks) - 1 else None
        if i == 0 and status is not None:
            await status.edit_text(chunk, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(chunk, parse_mode="HTML", reply_markup=markup)

    if not unlimited:
        left = settings.free_video_limit - user["free_videos_used"] - 1
        if left > 0:
            await message.answer(f"Free videos left this week: {left}.")
        else:
            await message.answer(
                "That was your last free video this week. "
                "Go unlimited with a subscription: /subscribe"
            )


# Telegram's getFile API refuses files above 20 MB for bots
MAX_TG_FILE_SIZE = 20 * 1024 * 1024


def _media_info(message: Message):
    """Returns (media, title, duration_seconds) or (None, "", 0)."""
    if message.voice:
        return message.voice, "Voice message", message.voice.duration
    if message.audio:
        title = message.audio.title or message.audio.file_name or "Audio"
        return message.audio, title, message.audio.duration
    if message.video_note:
        return message.video_note, "Video message", message.video_note.duration
    if message.video:
        return message.video, message.video.file_name or "Video", message.video.duration
    return None, "", 0


@router.message(F.voice | F.audio | F.video_note | F.video)
async def handle_media(message: Message) -> None:
    media, title, duration = _media_info(message)
    if media is None:
        return
    duration = duration or 0
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    unlimited, allowed = await _check_quota(message, user)
    if not allowed:
        return

    # forwards of the same file hit the cache via its stable unique id
    url = f"tg://{media.file_unique_id}"
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:32]
    cached = await db.get_cached_transcript(url_hash)
    if cached is not None:
        transcript = transcriber.Transcript(
            title=cached["title"],
            duration=cached["duration"],
            segments=[tuple(seg) for seg in json.loads(cached["segments"])],
        )
        await db.log_request(
            message.from_user.id, url, "cached", video_duration=transcript.duration
        )
        await _send_transcript(message, None, transcript, url_hash, user, unlimited)
        return

    max_duration = settings.sub_max_duration if unlimited else settings.free_max_duration
    if duration > max_duration:
        limit_note = (
            "" if unlimited
            else f"\nSubscribers get a higher limit — {settings.sub_max_duration // 60} minutes: /subscribe"
        )
        await message.answer(
            f"This recording is too long ({format_timestamp(duration)}). "
            f"The current limit is {max_duration // 60} minutes.{limit_note}"
        )
        return
    if media.file_size and media.file_size > MAX_TG_FILE_SIZE:
        await message.answer(
            "This file is larger than 20 MB — Telegram doesn't let bots download "
            "files that big. 😕 If it's a video, try sending a link instead."
        )
        return

    if _too_many_jobs(message.from_user.id):
        await message.answer(
            f"You already have {MAX_USER_JOBS} jobs processing — "
            "please wait for them to finish. 🙏"
        )
        return

    _job_started(message.from_user.id)
    try:
        await _process_media(message, user, unlimited, media, title, duration, url, url_hash)
    finally:
        _job_finished(message.from_user.id)


async def _process_media(
    message: Message, user, unlimited: bool, media, title: str,
    duration: int, url: str, url_hash: str,
) -> None:
    status = await message.answer("⬇️ Downloading the file...")
    _active_statuses.add(status)

    async def progress(text: str) -> None:
        try:
            await status.edit_text(text)
        except TelegramBadRequest:
            pass

    priority = transcriber.PRIORITY_SUBSCRIBER if unlimited else transcriber.PRIORITY_FREE
    path = transcriber.TMP_DIR / f"{uuid.uuid4().hex}.media"
    started = time.monotonic()
    try:
        await message.bot.download(media, destination=str(path))
        segments = await transcriber.transcribe_local(path, duration, progress, priority)
    except Exception:
        logger.exception("Media transcription failed (user %s)", message.from_user.id)
        await status.edit_text("Something went wrong during transcription. Please try again later. 🙏")
        await db.log_request(message.from_user.id, url, "error")
        return
    finally:
        _active_statuses.discard(status)
        path.unlink(missing_ok=True)

    elapsed = time.monotonic() - started
    await db.log_request(
        message.from_user.id, url, "ok",
        video_duration=duration, processing_seconds=elapsed,
    )

    if not segments:
        await status.edit_text("No speech found in this recording — the transcript is empty. 🤷")
        return

    transcript = transcriber.Transcript(title=title, duration=duration, segments=segments)
    await db.cache_transcript(url_hash, url, title, duration, segments)
    await _send_transcript(message, status, transcript, url_hash, user, unlimited)


@router.callback_query(F.data.startswith("exp:"))
async def export_transcript(callback: CallbackQuery) -> None:
    _, fmt, url_hash = callback.data.split(":", 2)
    row = await db.get_cached_transcript(url_hash)
    if row is None:
        await callback.answer("Transcript not found — please send the link again.", show_alert=True)
        return
    segments = [tuple(seg) for seg in json.loads(row["segments"])]
    content = to_srt(segments) if fmt == "srt" else to_txt(segments)
    file = BufferedInputFile(content.encode("utf-8"), filename=f"transcript.{fmt}")
    await callback.message.answer_document(file)
    await callback.answer()


@router.message(F.text)
async def handle_other_text(message: Message) -> None:
    await message.answer(
        "Send me a video link (YouTube, Instagram, TikTok), a voice message "
        "or an audio file — I'll reply with a timestamped transcript."
    )
