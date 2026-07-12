import hashlib
import html
import json
import logging
import re
import time
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
    global _insta_failures
    url = URL_RE.search(message.text).group(0)
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    # whitelisted users are always free, no limits
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

    status = await message.answer("⬇️ Downloading the video...")
    _active_statuses.add(status)

    async def progress(text: str) -> None:
        try:
            await status.edit_text(text)
        except TelegramBadRequest:
            pass  # e.g. text identical to the current one

    max_duration = settings.sub_max_duration if unlimited else settings.free_max_duration
    started = time.monotonic()
    try:
        transcript = await transcriber.transcribe_url(url, max_duration, progress)
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
        "Send me a video link (YouTube, Instagram, TikTok) and I'll reply "
        "with a timestamped transcript."
    )
