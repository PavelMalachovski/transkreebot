import html
import logging
import re

from aiogram import F, Router
from aiogram.types import Message

import db
import transcriber
from config import settings

logger = logging.getLogger(__name__)

router = Router(name="transcribe")

URL_RE = re.compile(r"https?://\S+")

# Telegram message hard limit is 4096 chars; leave headroom.
CHUNK_SIZE = 4000


def format_timestamp(seconds: int) -> str:
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
        for start, text in t.segments
    ]
    return header + "\n\n" + "\n".join(lines)


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


@router.message(F.text.regexp(URL_RE.pattern))
async def handle_url(message: Message) -> None:
    url = URL_RE.search(message.text).group(0)
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    # whitelisted users are always free, no limits
    unlimited = (
        db.has_active_subscription(user)
        or message.from_user.id in settings.free_user_id_set
    )
    if not unlimited and user["free_videos_used"] >= settings.free_video_limit:
        await message.answer(
            f"Бесплатный лимит ({settings.free_video_limit} видео) исчерпан. 😔\n"
            "Оформи подписку за €3/мес и расшифровывай без ограничений: /subscribe"
        )
        return

    status = await message.answer("Processing... ⏳")

    try:
        transcript = await transcriber.transcribe_url(url)
    except transcriber.DownloadError:
        logger.exception("Download failed for %s (user %s)", url, message.from_user.id)
        await status.edit_text(
            "Не получилось скачать это видео. 😕\n"
            "Проверь, что ссылка рабочая, видео не приватное и не удалено, "
            "и что это YouTube, Instagram или TikTok.\n"
            "Instagram иногда блокирует скачивание — в таком случае попробуй позже."
        )
        return
    except Exception:
        logger.exception("Transcription failed for %s (user %s)", url, message.from_user.id)
        await status.edit_text("Что-то пошло не так при расшифровке. Попробуй ещё раз позже. 🙏")
        return

    if not transcript.segments:
        await status.edit_text("В этом видео не нашлось речи — расшифровка пустая. 🤷")
        return

    # Quota is spent only after a successful transcription.
    if not unlimited:
        await db.increment_free_videos(message.from_user.id)

    chunks = split_into_chunks(format_transcript(transcript))
    await status.edit_text(chunks[0], parse_mode="HTML")
    for chunk in chunks[1:]:
        await message.answer(chunk, parse_mode="HTML")

    if not unlimited:
        left = settings.free_video_limit - user["free_videos_used"] - 1
        if left > 0:
            await message.answer(f"Осталось бесплатных видео: {left}.")
        else:
            await message.answer(
                "Это было последнее бесплатное видео. "
                "Дальше — подписка €3/мес: /subscribe"
            )


@router.message(F.text)
async def handle_other_text(message: Message) -> None:
    await message.answer(
        "Пришли мне ссылку на видео (YouTube, Instagram, TikTok) — верну текст с таймкодами."
    )
