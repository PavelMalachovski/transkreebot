import logging
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT,
    free_videos_used INT DEFAULT 0,
    subscription_active BOOLEAN DEFAULT FALSE,
    subscription_until TIMESTAMP,
    cancel_at_period_end BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);
"""


def utcnow() -> datetime:
    # naive UTC to match TIMESTAMP (without time zone) columns
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def init(database_url: str) -> None:
    global pool
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    logger.info("Database pool ready")


async def close() -> None:
    if pool is not None:
        await pool.close()


async def get_or_create_user(telegram_id: int, username: str | None) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (telegram_id, username)
            VALUES ($1, $2)
            ON CONFLICT (telegram_id)
            DO UPDATE SET username = EXCLUDED.username
            RETURNING *
            """,
            telegram_id,
            username,
        )
    return row


def has_active_subscription(user: asyncpg.Record) -> bool:
    return bool(
        user["subscription_active"]
        and user["subscription_until"] is not None
        and user["subscription_until"] > utcnow()
    )


async def increment_free_videos(telegram_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET free_videos_used = free_videos_used + 1 WHERE telegram_id = $1",
            telegram_id,
        )


async def activate_subscription(telegram_id: int, days: int) -> datetime:
    until = utcnow() + timedelta(days=days)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET subscription_active = TRUE,
                subscription_until = $2,
                cancel_at_period_end = FALSE
            WHERE telegram_id = $1
            """,
            telegram_id,
            until,
        )
    return until


async def set_cancel_at_period_end(telegram_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET cancel_at_period_end = TRUE WHERE telegram_id = $1",
            telegram_id,
        )
