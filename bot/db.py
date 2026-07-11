import json
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

CREATE TABLE IF NOT EXISTS transcripts (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    duration INT NOT NULL,
    segments JSONB NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS requests (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    video_duration INT,
    processing_seconds REAL,
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


async def get_cached_transcript(url_hash: str) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM transcripts WHERE url_hash = $1", url_hash
        )


async def cache_transcript(
    url_hash: str, url: str, title: str, duration: int,
    segments: list[tuple[float, float, str]],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO transcripts (url_hash, url, title, duration, segments)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (url_hash) DO NOTHING
            """,
            url_hash, url, title, duration, json.dumps(segments),
        )


async def log_request(
    telegram_id: int, url: str, status: str,
    video_duration: int | None = None,
    processing_seconds: float | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO requests (telegram_id, url, status, video_duration, processing_seconds)
            VALUES ($1, $2, $3, $4, $5)
            """,
            telegram_id, url, status, video_duration, processing_seconds,
        )


async def get_stats() -> asyncpg.Record:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM users) AS users_total,
                (SELECT count(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days') AS users_7d,
                (SELECT count(*) FROM users
                    WHERE subscription_active AND subscription_until > NOW()) AS subs_active,
                (SELECT count(*) FROM requests) AS requests_total,
                (SELECT count(*) FROM requests
                    WHERE created_at > NOW() - INTERVAL '24 hours') AS requests_24h,
                (SELECT count(*) FROM requests
                    WHERE status NOT IN ('ok', 'cached')
                    AND created_at > NOW() - INTERVAL '24 hours') AS errors_24h,
                (SELECT round(avg(processing_seconds)::numeric, 1) FROM requests
                    WHERE status = 'ok') AS avg_processing,
                (SELECT count(*) FROM transcripts) AS cached_total
            """
        )
