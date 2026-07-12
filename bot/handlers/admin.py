from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

import db
from config import settings

router = Router(name="admin")


@router.message(Command("stats"), F.from_user.id.in_(settings.admin_id_set))
async def cmd_stats(message: Message) -> None:
    s = await db.get_stats()
    avg = f"{s['avg_processing']}s" if s["avg_processing"] is not None else "—"
    await message.answer(
        "📊 <b>Stats</b>\n\n"
        f"👥 Users: {s['users_total']} (+{s['users_7d']} in 7 days)\n"
        f"💳 Active subscriptions: {s['subs_active']}\n\n"
        f"🎬 Requests total: {s['requests_total']}\n"
        f"— last 24h: {s['requests_24h']}\n"
        f"— errors last 24h: {s['errors_24h']}\n"
        f"⏱ Avg processing time: {avg}\n"
        f"🗃 Cached transcripts: {s['cached_total']}",
        parse_mode="HTML",
    )
