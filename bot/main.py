import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher

import db
from config import settings
from handlers import admin, payments, start, transcribe


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.info("Starting transkreebot...")

    await db.init(settings.database_url)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    # transcribe last: it has a catch-all text handler
    dp.include_routers(start.router, admin.router, payments.router, transcribe.router)
    # tell users with in-flight jobs about the restart instead of leaving
    # them staring at a frozen status message
    dp.shutdown.register(transcribe.notify_restart)

    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
