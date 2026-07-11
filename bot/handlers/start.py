from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import db
from config import settings

router = Router(name="start")

WELCOME = (
    "Привет! Я превращаю видео в текст с таймкодами.\n\n"
    "Просто пришли мне ссылку на видео (YouTube, Instagram, TikTok) — "
    "я скачаю его и верну расшифровку вида:\n"
    "<code>[0] слова слова слова\n[14] другие слова</code>\n\n"
    f"🎁 Бесплатно: {settings.free_video_limit} видео.\n"
    "💳 Дальше — подписка €3/мес, безлимит: /subscribe\n\n"
    "Команды:\n"
    "/status — сколько видео осталось и статус подписки\n"
    "/subscribe — оформить подписку\n"
    "/cancel — отменить продление подписки"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(WELCOME, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    if db.has_active_subscription(user):
        until = user["subscription_until"].strftime("%d.%m.%Y")
        if user["cancel_at_period_end"]:
            sub_line = f"💳 Подписка активна до {until}, продление отключено."
        else:
            sub_line = f"💳 Подписка активна до {until}."
        await message.answer(f"{sub_line}\nВидео — без ограничений. 🎉")
    else:
        used = user["free_videos_used"]
        left = max(settings.free_video_limit - used, 0)
        await message.answer(
            f"🎁 Бесплатных видео использовано: {used} из {settings.free_video_limit} "
            f"(осталось {left}).\n"
            "💳 Подписка не активна. Оформить: /subscribe"
        )
