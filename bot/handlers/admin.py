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
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {s['users_total']} (+{s['users_7d']} за 7 дней)\n"
        f"💳 Активных подписок: {s['subs_active']}\n\n"
        f"🎬 Запросов всего: {s['requests_total']}\n"
        f"— за 24 часа: {s['requests_24h']}\n"
        f"— ошибок за 24 часа: {s['errors_24h']}\n"
        f"⏱ Среднее время обработки: {avg}\n"
        f"🗃 Расшифровок в кэше: {s['cached_total']}",
        parse_mode="HTML",
    )
