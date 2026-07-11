from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import db
from config import settings

router = Router(name="start")

WELCOME = (
    "👋 Привет! Я превращаю видео в текст.\n\n"
    "Пришли ссылку на видео из <b>YouTube</b>, <b>Instagram</b> или <b>TikTok</b> — "
    "я распознаю речь и верну расшифровку с таймкодами:\n\n"
    "<blockquote>🎬 <b>Название видео</b> · 0:49\n"
    "<b>0:00</b>  Первые слова из видео…\n"
    "<b>0:05</b>  Следующая фраза…\n"
    "<b>0:12</b>  И так далее до конца.</blockquote>\n\n"
    "⚡ Обычно это занимает меньше минуты — можно сразу присылать несколько ссылок. "
    "Под каждой расшифровкой есть кнопки, чтобы скачать её файлом (.txt) "
    "или субтитрами (.srt).\n\n"
    "<b>Тарифы</b>\n"
    f"🎁 {settings.free_video_limit} видео в неделю — бесплатно "
    f"(до {settings.free_max_duration // 60} минут каждое)\n"
    f"⭐ Подписка — {settings.subscription_stars} Stars в месяц: безлимит "
    f"и видео до {settings.sub_max_duration // 3600} часов. Оформить: /subscribe\n\n"
    "<b>Команды</b>\n"
    "/status — остаток бесплатных видео и подписка\n"
    "/subscribe — оформить подписку\n"
    "/cancel — отключить продление подписки"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(WELCOME, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    if message.from_user.id in settings.free_user_id_set:
        await message.answer("⭐ Для тебя всё бесплатно и без ограничений.")
        return

    if db.has_active_subscription(user):
        until = user["subscription_until"].strftime("%d.%m.%Y")
        if user["cancel_at_period_end"]:
            sub_line = f"⭐ Подписка активна до {until}, продление отключено (/subscribe — включить обратно)."
        else:
            sub_line = f"⭐ Подписка активна до {until}, продлится автоматически."
        await message.answer(f"{sub_line}\nВидео — без ограничений. 🎉")
    else:
        used = user["free_videos_used"]
        left = max(settings.free_video_limit - used, 0)
        renew_line = ""
        if user["free_week_start"] is not None:
            renew_date = user["free_week_start"] + timedelta(days=7)
            renew_line = f"Лимит обновится {renew_date.strftime('%d.%m.%Y')}.\n"
        await message.answer(
            f"🎁 На этой неделе использовано {used} из {settings.free_video_limit} "
            f"бесплатных видео (осталось {left}).\n"
            f"{renew_line}"
            "⭐ Подписка не активна. Оформить: /subscribe"
        )
