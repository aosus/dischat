from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from dischat.security.audit import AuditEntry


@dataclass(slots=True, frozen=True)
class ChatAccount:
    mxid: str
    platform: str
    discourse_username: str | None
    paired_at: datetime | None
    response_locale: str


class ChatAccountRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_chat_account(
        self, *, mxid: str, platform: str, response_locale: str = "ar"
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO chat_accounts (mxid, platform, response_locale, created_at, updated_at)
                VALUES ($1, $2, $3, NOW(), NOW())
                ON CONFLICT (mxid)
                DO UPDATE SET platform = EXCLUDED.platform, response_locale = EXCLUDED.response_locale, updated_at = NOW()
                """,
                mxid,
                platform,
                response_locale,
            )

    async def pair_account(self, *, mxid: str, discourse_username: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE chat_accounts
                SET discourse_username = $2, paired_at = NOW(), revoked_at = NULL, updated_at = NOW()
                WHERE mxid = $1
                """,
                mxid,
                discourse_username,
            )

    async def get_by_mxid(self, mxid: str) -> ChatAccount | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT mxid, platform, discourse_username, paired_at, response_locale
                FROM chat_accounts
                WHERE mxid = $1
                """,
                mxid,
            )
        if row is None:
            return None
        return ChatAccount(
            mxid=row["mxid"],
            platform=row["platform"],
            discourse_username=row["discourse_username"],
            paired_at=row["paired_at"],
            response_locale=row["response_locale"],
        )


class AuditLogRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(self, entry: AuditEntry) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO audit_logs (
                    action,
                    mxid,
                    platform,
                    discourse_username_used,
                    discourse_user_id_used,
                    topic_id,
                    post_id,
                    matrix_room_id,
                    matrix_event_id,
                    success,
                    error_message,
                    created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                entry.action,
                entry.mxid,
                entry.platform,
                entry.discourse_username_used,
                entry.discourse_user_id_used,
                entry.topic_id,
                entry.post_id,
                entry.matrix_room_id,
                entry.matrix_event_id,
                entry.success,
                entry.error_message,
                datetime.now(UTC),
            )
