from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import db
from config import settings

router = Router(name="start")

WELCOME = (
    "👋 Hi! I turn videos into text.\n\n"
    "Send me a link to a <b>YouTube</b>, <b>Instagram</b> or <b>TikTok</b> video — "
    "I'll recognize the speech and reply with a timestamped transcript:\n\n"
    "<blockquote>🎬 <b>Video title</b> · 0:49\n"
    "<b>0:00</b>  The first words of the video…\n"
    "<b>0:05</b>  The next phrase…\n"
    "<b>0:12</b>  And so on until the end.</blockquote>\n\n"
    "⚡ It usually takes less than a minute — feel free to send several links at once. "
    "Voice messages, video notes and audio files work too — just forward them to me. "
    "Every transcript comes with buttons to download it as a file (.txt) "
    "or as subtitles (.srt).\n\n"
    "<b>Pricing</b>\n"
    f"🎁 {settings.free_video_limit} videos per week — free "
    f"(up to {settings.free_max_duration // 60} minutes each)\n"
    f"⭐ Subscription — {settings.subscription_stars} Stars per month: unlimited videos "
    f"up to {settings.sub_max_duration // 3600} hours long. Subscribe: /subscribe\n\n"
    "<b>Commands</b>\n"
    "/status — free videos left and subscription info\n"
    "/subscribe — get a subscription\n"
    "/cancel — turn off subscription renewal"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(WELCOME, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    if message.from_user.id in settings.free_user_id_set:
        await message.answer("⭐ Everything is free and unlimited for you.")
        return

    if db.has_active_subscription(user):
        until = user["subscription_until"].strftime("%d.%m.%Y")
        if user["cancel_at_period_end"]:
            sub_line = f"⭐ Subscription active until {until}, renewal is off (/subscribe to turn it back on)."
        else:
            sub_line = f"⭐ Subscription active until {until}, renews automatically."
        await message.answer(f"{sub_line}\nUnlimited videos. 🎉")
    else:
        used = user["free_videos_used"]
        left = max(settings.free_video_limit - used, 0)
        renew_line = ""
        if user["free_week_start"] is not None:
            renew_date = user["free_week_start"] + timedelta(days=7)
            renew_line = f"The limit resets on {renew_date.strftime('%d.%m.%Y')}.\n"
        await message.answer(
            f"🎁 This week you've used {used} of {settings.free_video_limit} "
            f"free videos ({left} left).\n"
            f"{renew_line}"
            "⭐ No active subscription. Subscribe: /subscribe"
        )
