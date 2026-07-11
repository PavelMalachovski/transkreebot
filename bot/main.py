import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher

import db
from config import settings
from handlers import payments, start, transcribe


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
    dp.include_routers(start.router, payments.router, transcribe.router)

    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
