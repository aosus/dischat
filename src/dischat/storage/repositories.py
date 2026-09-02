from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import asyncpg

from dischat.security.audit import AuditEntry

WatchMode = Literal["category", "all_public_categories"]
JobStatus = Literal["pending", "running", "complete", "failed"]
TargetType = Literal["dm", "room"]
MatrixEventStatus = Literal["claimed", "owned", "written", "processed"]

# How long a processing lease is held before a replaying attempt may take the
# fence over. Generous relative to normal processing latency but short enough
# that a crashed worker does not stall an event forever.
DEFAULT_LEASE_SECONDS = 900


def new_lease_owner() -> str:
    """Fresh per-attempt ownership token for the event fence."""
    return secrets.token_urlsafe(16)


# Default lease for claimed delivery jobs; a 'running' job becomes claimable
# again once its lease expires (crashed-worker recovery window).
DEFAULT_JOB_LEASE_SECONDS = 120


@dataclass(slots=True, frozen=True)
class ChatAccount:
    id: int
    mxid: str
    platform: str
    discourse_user_id: int | None
    discourse_username: str | None
    paired_at: datetime | None
    revoked_at: datetime | None
    notify_on_direct_replies: bool
    notify_on_mentions: bool
    response_locale: str


@dataclass(slots=True, frozen=True)
class PairingSessionRecord:
    id: int
    mxid: str
    discourse_username: str
    discourse_user_id: int | None
    code_hash: str
    expires_at: datetime
    consumed_at: datetime | None
    attempt_count: int


@dataclass(slots=True)
class PairingRateLimitState:
    mxid: str
    discourse_username: str | None
    issuance_count: int
    failure_count: int
    window_started_at: datetime
    cooldown_until: datetime | None


@dataclass(slots=True, frozen=True)
class CategoryRecord:
    id: int
    discourse_category_id: int
    slug: str
    name: str
    is_public: bool
    enabled: bool


@dataclass(slots=True, frozen=True)
class UserWatchRecord:
    id: int
    mxid: str
    mode: WatchMode
    category_id: int | None
    category_slug: str | None


@dataclass(slots=True, frozen=True)
class RoomLinkRecord:
    id: int
    matrix_room_id: str
    include_all_public_categories: bool
    allow_relay: bool
    full_content: bool
    enabled: bool
    category_slugs: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DiscourseEventRecord:
    id: int
    discourse_topic_id: int
    discourse_post_id: int
    event_type: str
    category_id: int | None
    author_username: str
    target_discourse_username: str | None
    raw_payload_json: dict[str, Any]


@dataclass(slots=True, frozen=True)
class DeliveryJobRecord:
    id: int
    event_id: int
    target_type: TargetType
    target_mxid: str | None
    matrix_room_id: str | None
    status: JobStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    matrix_tx_id: str | None = None
    matrix_device_id: str | None = None
    matrix_dm_room_id: str | None = None
    claim_token: str | None = None


@dataclass(slots=True, frozen=True)
class DeliveryMessageRecord:
    id: int
    discourse_topic_id: int
    discourse_post_id: int
    matrix_room_id: str
    matrix_event_id: str
    target_type: TargetType
    target_mxid: str | None
    parent_delivery_message_id: int | None


@dataclass(slots=True, frozen=True)
class MatrixEventStateRecord:
    id: int
    room_id: str
    event_id: str
    status: MatrixEventStatus
    discourse_topic_id: int | None = None
    discourse_post_id: int | None = None
    response_notice: str | None = None


@dataclass(slots=True, frozen=True)
class EventOutcome:
    """Result of attempting to record an external write in the ledger."""

    recorded: bool
    conflicting_topic_id: int | None = None
    conflicting_post_id: int | None = None


def _record_to_chat_account(row: asyncpg.Record) -> ChatAccount:
    return ChatAccount(
        id=row["id"],
        mxid=row["mxid"],
        platform=row["platform"],
        discourse_user_id=row["discourse_user_id"],
        discourse_username=row["discourse_username"],
        paired_at=row["paired_at"],
        revoked_at=row["revoked_at"],
        notify_on_direct_replies=row["notify_on_direct_replies"],
        notify_on_mentions=row["notify_on_mentions"],
        response_locale=row["response_locale"],
    )


def _record_to_pairing_session(row: asyncpg.Record) -> PairingSessionRecord:
    return PairingSessionRecord(
        id=row["id"],
        mxid=row["mxid"],
        discourse_username=row["discourse_username"],
        discourse_user_id=row["discourse_user_id"],
        code_hash=row["code_hash"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        attempt_count=row["attempt_count"],
    )


def _record_to_category(row: asyncpg.Record) -> CategoryRecord:
    return CategoryRecord(
        id=row["id"],
        discourse_category_id=row["discourse_category_id"],
        slug=row["slug"],
        name=row["name"],
        is_public=row["is_public"],
        enabled=row["enabled"],
    )


def _record_to_discourse_event(row: asyncpg.Record) -> DiscourseEventRecord:
    raw_payload = row["raw_payload_json"]
    if isinstance(raw_payload, str):
        raw_payload = json.loads(raw_payload)
    return DiscourseEventRecord(
        id=row["id"],
        discourse_topic_id=row["discourse_topic_id"],
        discourse_post_id=row["discourse_post_id"],
        event_type=row["event_type"],
        category_id=row["category_id"],
        author_username=row["author_username"],
        target_discourse_username=row["target_discourse_username"],
        raw_payload_json=raw_payload,
    )


def _record_to_delivery_job(row: asyncpg.Record) -> DeliveryJobRecord:
    return DeliveryJobRecord(
        id=row["id"],
        event_id=row["event_id"],
        target_type=row["target_type"],
        target_mxid=row["target_mxid"],
        matrix_room_id=row["matrix_room_id"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"],
        claimed_at=row["claimed_at"],
        lease_expires_at=row["lease_expires_at"],
        matrix_tx_id=row["matrix_tx_id"],
        matrix_device_id=row["matrix_device_id"],
        matrix_dm_room_id=row["matrix_dm_room_id"],
        claim_token=row["claim_token"],
    )


def _record_to_delivery_message(row: asyncpg.Record) -> DeliveryMessageRecord:
    return DeliveryMessageRecord(
        id=row["id"],
        discourse_topic_id=row["discourse_topic_id"],
        discourse_post_id=row["discourse_post_id"],
        matrix_room_id=row["matrix_room_id"],
        matrix_event_id=row["matrix_event_id"],
        target_type=row["target_type"],
        target_mxid=row["target_mxid"],
        parent_delivery_message_id=row["parent_delivery_message_id"],
    )


class ChatAccountRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_account(
        self, *, mxid: str, platform: str, response_locale: str
    ) -> ChatAccount:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO chat_accounts (mxid, platform, response_locale, created_at, updated_at)
                VALUES ($1, $2, $3, NOW(), NOW())
                ON CONFLICT (mxid)
                DO UPDATE SET platform = EXCLUDED.platform, response_locale = EXCLUDED.response_locale, updated_at = NOW()
                RETURNING *
                """,
                mxid,
                platform,
                response_locale,
            )
        assert row is not None
        return _record_to_chat_account(row)

    async def get_by_mxid(self, mxid: str) -> ChatAccount | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM chat_accounts WHERE mxid = $1", mxid)
        return _record_to_chat_account(row) if row is not None else None

    async def pair_account(
        self,
        *,
        mxid: str,
        discourse_username: str,
        discourse_user_id: int | None = None,
    ) -> ChatAccount:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE chat_accounts
                SET discourse_username = $2,
                    discourse_user_id = $3,
                    paired_at = NOW(),
                    revoked_at = NULL,
                    updated_at = NOW()
                WHERE mxid = $1
                RETURNING *
                """,
                mxid,
                discourse_username,
                discourse_user_id,
            )
        assert row is not None
        return _record_to_chat_account(row)

    async def unpair_account(self, *, mxid: str) -> ChatAccount | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE chat_accounts
                SET discourse_username = NULL,
                    discourse_user_id = NULL,
                    revoked_at = NOW(),
                    updated_at = NOW()
                WHERE mxid = $1
                RETURNING *
                """,
                mxid,
            )
        return _record_to_chat_account(row) if row is not None else None

    async def list_by_discourse_username(self, discourse_username: str) -> list[ChatAccount]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM chat_accounts
                WHERE discourse_username = $1 AND revoked_at IS NULL
                ORDER BY id
                """,
                discourse_username,
            )
        return [_record_to_chat_account(row) for row in rows]


class PairingSessionRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_session(
        self,
        *,
        mxid: str,
        discourse_username: str,
        code_hash: str,
        expires_at: datetime,
        discourse_user_id: int | None = None,
    ) -> PairingSessionRecord:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", mxid
                )
                await connection.execute(
                    "UPDATE pairing_sessions SET consumed_at = NOW() "
                    "WHERE mxid = $1 AND consumed_at IS NULL",
                    mxid,
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO pairing_sessions (
                        mxid,
                        discourse_username,
                        discourse_user_id,
                        code_hash,
                        expires_at,
                        created_at
                    )
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    RETURNING *
                    """,
                    mxid,
                    discourse_username,
                    discourse_user_id,
                    code_hash,
                    expires_at,
                )
        assert row is not None
        return _record_to_pairing_session(row)

    async def get_active_session(self, mxid: str) -> PairingSessionRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM pairing_sessions
                WHERE mxid = $1 AND consumed_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                mxid,
            )
        return _record_to_pairing_session(row) if row is not None else None

    async def increment_attempt_count(self, session_id: int) -> PairingSessionRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE pairing_sessions
                SET attempt_count = attempt_count + 1
                WHERE id = $1 AND consumed_at IS NULL
                RETURNING *
                """,
                session_id,
            )
        return _record_to_pairing_session(row) if row is not None else None

    async def consume_session(self, session_id: int) -> PairingSessionRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE pairing_sessions
                SET consumed_at = NOW()
                WHERE id = $1 AND consumed_at IS NULL
                RETURNING *
                """,
                session_id,
            )
        return _record_to_pairing_session(row) if row is not None else None

    async def cancel_session(self, mxid: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE pairing_sessions
                SET consumed_at = NOW()
                WHERE mxid = $1 AND consumed_at IS NULL
                """,
                mxid,
            )


class PairingRateLimitRepository:
    """Persistent pairing rate-limit counters that survive session replacement.

    State is keyed by (mxid, discourse_username) so limits hold even when a new
    pairing session replaces the previous unconsumed one.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def reserve_issuance(
        self,
        *,
        mxid: str,
        discourse_username: str,
        now: datetime,
        window: timedelta,
        max_issuances: int,
    ) -> datetime | None:
        """Atomically reserve both user and target rolling-window capacity."""
        target = discourse_username.lower()
        cutoff = now - window
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", mxid
                )
                # Opportunistic global retention: issuance is the only path
                # that grows these tables, so pruning here bounds stale user
                # identifiers without a separate scheduler.
                await connection.execute(
                    "DELETE FROM pairing_issuance_events WHERE issued_at <= $1",
                    cutoff,
                )
                await connection.execute(
                    """
                    DELETE FROM pairing_rate_limits
                    WHERE updated_at <= $1
                      AND (cooldown_until IS NULL OR cooldown_until <= $2)
                    """,
                    cutoff,
                    now,
                )
                cooldown_rows = await connection.fetch(
                    """
                    SELECT cooldown_until
                    FROM pairing_rate_limits
                    WHERE mxid = $1
                      AND COALESCE(discourse_username, '') = ANY($2::text[])
                      AND cooldown_until > $3
                    """,
                    mxid,
                    ["", target],
                    now,
                )
                if cooldown_rows:
                    return max(row["cooldown_until"] for row in cooldown_rows)
                rows = await connection.fetch(
                    """
                    SELECT discourse_username, COUNT(*) AS count, MIN(issued_at) AS oldest
                    FROM pairing_issuance_events
                    WHERE mxid = $1 AND discourse_username = ANY($2::text[])
                    GROUP BY discourse_username
                    """,
                    mxid,
                    ["", target],
                )
                blocked = [row for row in rows if row["count"] >= max_issuances]
                if blocked:
                    return max(row["oldest"] + window for row in blocked)
                await connection.executemany(
                    "INSERT INTO pairing_issuance_events "
                    "(mxid, discourse_username, issued_at) VALUES ($1, $2, $3)",
                    [(mxid, "", now), (mxid, target, now)],
                )
        return None

    async def record_failure_and_apply_cooldown(
        self,
        *,
        mxid: str,
        discourse_username: str,
        now: datetime,
        max_failures: int,
        cooldown: timedelta,
    ) -> datetime | None:
        """Update user and target failure scopes in one locked transaction."""
        target = discourse_username.lower()
        armed_until: datetime | None = None
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", mxid
                )
                for username in (None, target):
                    row = await connection.fetchrow(
                        """
                        INSERT INTO pairing_rate_limits
                            (mxid, discourse_username, failure_count, updated_at)
                        VALUES ($1, $2, 1, $3)
                        ON CONFLICT (mxid, COALESCE(discourse_username, '')) DO UPDATE SET
                            failure_count = CASE
                                WHEN pairing_rate_limits.cooldown_until IS NOT NULL
                                 AND pairing_rate_limits.cooldown_until <= $3 THEN 1
                                ELSE pairing_rate_limits.failure_count + 1 END,
                            cooldown_until = CASE
                                WHEN pairing_rate_limits.cooldown_until IS NOT NULL
                                 AND pairing_rate_limits.cooldown_until <= $3 THEN NULL
                                ELSE pairing_rate_limits.cooldown_until END,
                            updated_at = $3
                        RETURNING failure_count, cooldown_until
                        """,
                        mxid,
                        username,
                        now,
                    )
                    assert row is not None
                    active = row["cooldown_until"] is not None and now < row["cooldown_until"]
                    if row["failure_count"] >= max_failures and not active:
                        candidate = now + cooldown
                        await connection.execute(
                            """
                            UPDATE pairing_rate_limits
                            SET failure_count = 0,
                                cooldown_until = GREATEST(COALESCE(cooldown_until, $3), $3),
                                updated_at = $4
                            WHERE mxid = $1
                              AND COALESCE(discourse_username, '') = COALESCE($2, '')
                            """,
                            mxid,
                            username,
                            candidate,
                            now,
                        )
                        armed_until = max(armed_until or candidate, candidate)
        return armed_until

    @staticmethod
    def _record_to_state(row: asyncpg.Record) -> PairingRateLimitState:
        return PairingRateLimitState(
            mxid=row["mxid"],
            discourse_username=row["discourse_username"],
            issuance_count=row["issuance_count"],
            failure_count=row["failure_count"],
            window_started_at=row["window_started_at"],
            cooldown_until=row["cooldown_until"],
        )

    async def get_state(
        self, *, mxid: str, discourse_username: str | None
    ) -> PairingRateLimitState | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM pairing_rate_limits
                WHERE mxid = $1 AND COALESCE(discourse_username, '') = COALESCE($2, '')
                """,
                mxid,
                discourse_username,
            )
        return self._record_to_state(row) if row is not None else None

    async def record_issuance(
        self,
        *,
        mxid: str,
        discourse_username: str | None,
        now: datetime,
        window: timedelta,
    ) -> PairingRateLimitState:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO pairing_rate_limits (
                    mxid, discourse_username, issuance_count, window_started_at, updated_at
                )
                VALUES ($1, $2, 1, $3::timestamptz, $3::timestamptz)
                ON CONFLICT (mxid, COALESCE(discourse_username, '')) DO UPDATE SET
                    issuance_count =
                        CASE
                            WHEN pairing_rate_limits.window_started_at <= $3::timestamptz - $4::interval
                            THEN 1
                            ELSE pairing_rate_limits.issuance_count + 1
                        END,
                    window_started_at =
                        CASE
                            WHEN pairing_rate_limits.window_started_at <= $3::timestamptz - $4::interval
                            THEN $3::timestamptz
                            ELSE pairing_rate_limits.window_started_at
                        END,
                    updated_at = $3::timestamptz
                RETURNING *
                """,
                mxid,
                discourse_username,
                now,
                window,
            )
        assert row is not None
        return self._record_to_state(row)

    async def record_failure(
        self,
        *,
        mxid: str,
        discourse_username: str | None,
        now: datetime,
    ) -> PairingRateLimitState:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO pairing_rate_limits (mxid, discourse_username, failure_count, updated_at)
                VALUES ($1, $2, 1, $3::timestamptz)
                ON CONFLICT (mxid, COALESCE(discourse_username, '')) DO UPDATE SET
                    failure_count = pairing_rate_limits.failure_count + 1,
                    updated_at = $3::timestamptz
                RETURNING *
                """,
                mxid,
                discourse_username,
                now,
            )
        assert row is not None
        return self._record_to_state(row)

    async def apply_cooldown(
        self,
        *,
        mxid: str,
        discourse_username: str | None,
        cooldown_until: datetime,
        now: datetime,
        reset_failure_count: bool = False,
    ) -> None:
        """Arm (or extend) the cooldown; optionally reset the failure counter.

        Resetting on re-arm gives each cooldown a fresh threshold: the next
        cooldown requires ``max_failures`` new failures after the previous one
        expired, instead of the stale counter re-triggering instantly.
        """
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO pairing_rate_limits
                    (mxid, discourse_username, cooldown_until, updated_at)
                VALUES ($1, $2, $4::timestamptz, $3::timestamptz)
                ON CONFLICT (mxid, COALESCE(discourse_username, '')) DO UPDATE SET
                    cooldown_until = EXCLUDED.cooldown_until,
                    failure_count =
                        CASE WHEN $5::bool THEN 0
                             ELSE pairing_rate_limits.failure_count END,
                    updated_at = EXCLUDED.updated_at
                """,
                mxid,
                discourse_username,
                now,
                cooldown_until,
                reset_failure_count,
            )
        return None


class CategoryRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_category(
        self,
        *,
        discourse_category_id: int,
        slug: str,
        name: str,
        is_public: bool,
        enabled: bool = True,
    ) -> CategoryRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO categories (discourse_category_id, slug, name, is_public, enabled, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (discourse_category_id)
                DO UPDATE SET slug = EXCLUDED.slug,
                              name = EXCLUDED.name,
                              is_public = EXCLUDED.is_public,
                              enabled = EXCLUDED.enabled,
                              updated_at = NOW()
                RETURNING *
                """,
                discourse_category_id,
                slug,
                name,
                is_public,
                enabled,
            )
        assert row is not None
        return _record_to_category(row)

    async def list_categories(self) -> list[CategoryRecord]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT * FROM categories WHERE enabled = TRUE ORDER BY slug"
            )
        return [_record_to_category(row) for row in rows]

    async def get_by_slug(self, slug: str) -> CategoryRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM categories WHERE slug = $1 AND enabled = TRUE", slug
            )
        return _record_to_category(row) if row is not None else None

    async def get_by_discourse_category_id(
        self, discourse_category_id: int
    ) -> CategoryRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM categories WHERE discourse_category_id = $1", discourse_category_id
            )
        return _record_to_category(row) if row is not None else None

    async def disable_categories_not_in(self, discourse_category_ids: list[int]) -> None:
        # Fail-closed companion to the periodic visibility refresh: any category row that
        # no longer appears in the fresh Discourse listing is disabled so it can never be
        # bridged off a stale public snapshot. Enabled rows are restored automatically by
        # the next upsert if the category reappears.
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE categories
                SET enabled = FALSE, updated_at = NOW()
                WHERE enabled = TRUE
                  AND NOT (discourse_category_id = ANY($1::int[]))
                """,
                discourse_category_ids,
            )


class UserWatchRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def add_category_watch(self, *, mxid: str, category_id: int) -> UserWatchRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO user_watches (mxid, mode, category_id, created_at)
                VALUES ($1, 'category', $2, NOW())
                ON CONFLICT DO NOTHING
                RETURNING id, mxid, mode, category_id, NULL::TEXT AS category_slug
                """,
                mxid,
                category_id,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT uw.id, uw.mxid, uw.mode, uw.category_id, c.slug AS category_slug
                    FROM user_watches uw
                    LEFT JOIN categories c ON c.id = uw.category_id
                    WHERE uw.mxid = $1 AND uw.mode = 'category' AND uw.category_id = $2
                    """,
                    mxid,
                    category_id,
                )
        assert row is not None
        return UserWatchRecord(**dict(row))

    async def add_watch_all(self, *, mxid: str) -> UserWatchRecord:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM user_watches
                WHERE mxid = $1 AND mode = 'all_public_categories' AND category_id IS NULL
                """,
                mxid,
            )
            row = await connection.fetchrow(
                """
                INSERT INTO user_watches (mxid, mode, category_id, created_at)
                VALUES ($1, 'all_public_categories', NULL, NOW())
                RETURNING id, mxid, mode, category_id, NULL::TEXT AS category_slug
                """,
                mxid,
            )
        assert row is not None
        return UserWatchRecord(**dict(row))

    async def remove_category_watch(self, *, mxid: str, category_id: int) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM user_watches
                WHERE mxid = $1 AND mode = 'category' AND category_id = $2
                """,
                mxid,
                category_id,
            )

    async def remove_watch_all(self, *, mxid: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM user_watches
                WHERE mxid = $1 AND mode = 'all_public_categories' AND category_id IS NULL
                """,
                mxid,
            )

    async def list_watches_for_mxid(self, mxid: str) -> list[UserWatchRecord]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT uw.id, uw.mxid, uw.mode, uw.category_id, c.slug AS category_slug
                FROM user_watches uw
                LEFT JOIN categories c ON c.id = uw.category_id
                WHERE uw.mxid = $1
                ORDER BY uw.mode, c.slug NULLS FIRST
                """,
                mxid,
            )
        return [UserWatchRecord(**dict(row)) for row in rows]

    async def list_mxids_for_category(
        self, *, category_id: int, include_non_public: bool = False
    ) -> list[str]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT uw.mxid
                FROM user_watches uw
                WHERE (
                    (uw.mode = 'category' AND uw.category_id = $1 AND (
                        $2::boolean
                        OR EXISTS (
                            SELECT 1 FROM categories c
                            WHERE c.id = $1 AND c.is_public = TRUE AND c.enabled = TRUE
                        )
                    ))
                    -- all_public_categories subscribers must never be unlocked by
                    -- include_non_public: the live-E2E escape hatch authorizes only the
                    -- explicitly configured category watch / room link path, so a private
                    -- (e.g. live-E2E test) category never fans out to "all public" watchers.
                    OR (uw.mode = 'all_public_categories' AND uw.category_id IS NULL AND EXISTS (
                        SELECT 1 FROM categories c
                        WHERE c.id = $1 AND c.is_public = TRUE AND c.enabled = TRUE
                    ))
                )
                ORDER BY uw.mxid
                """,
                category_id,
                include_non_public,
            )
        return [str(row["mxid"]) for row in rows]


class RoomLinkRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def replace_room_links(
        self, room_links: dict[str, dict[str, Any]], category_lookup: dict[str, int]
    ) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("DELETE FROM room_link_categories")
                await connection.execute("DELETE FROM room_links")
                for room_id, config in room_links.items():
                    categories = list(config.get("categories", []))
                    include_all = "all" in categories
                    row = await connection.fetchrow(
                        """
                        INSERT INTO room_links (
                            matrix_room_id,
                            include_all_public_categories,
                            allow_relay,
                            full_content,
                            enabled,
                            created_at,
                            updated_at
                        )
                        VALUES ($1, $2, $3, $4, TRUE, NOW(), NOW())
                        RETURNING id
                        """,
                        room_id,
                        include_all,
                        bool(config.get("allow_relay", False)),
                        bool(config.get("full_content", False)),
                    )
                    assert row is not None
                    room_link_id = row["id"]
                    for slug in categories:
                        if slug == "all":
                            continue
                        category_id = category_lookup.get(slug)
                        if category_id is None:
                            continue
                        await connection.execute(
                            """
                            INSERT INTO room_link_categories (room_link_id, category_id)
                            VALUES ($1, $2)
                            ON CONFLICT DO NOTHING
                            """,
                            room_link_id,
                            category_id,
                        )

    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord | None:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT rl.id,
                       rl.matrix_room_id,
                       rl.include_all_public_categories,
                       rl.allow_relay,
                       rl.full_content,
                       rl.enabled,
                       c.slug AS category_slug
                FROM room_links rl
                LEFT JOIN room_link_categories rlc ON rlc.room_link_id = rl.id
                LEFT JOIN categories c ON c.id = rlc.category_id
                WHERE rl.matrix_room_id = $1 AND rl.enabled = TRUE
                ORDER BY c.slug
                """,
                matrix_room_id,
            )
        if not rows:
            return None
        first = rows[0]
        return RoomLinkRecord(
            id=first["id"],
            matrix_room_id=first["matrix_room_id"],
            include_all_public_categories=first["include_all_public_categories"],
            allow_relay=first["allow_relay"],
            full_content=first["full_content"],
            enabled=first["enabled"],
            category_slugs=tuple(
                row["category_slug"] for row in rows if row["category_slug"] is not None
            ),
        )

    async def list_links_matching_category(
        self, category_slug: str, *, include_non_public: bool = False
    ) -> list[RoomLinkRecord]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT rl.id,
                       rl.matrix_room_id,
                       rl.include_all_public_categories,
                       rl.allow_relay,
                       rl.full_content,
                       rl.enabled,
                       c.slug AS category_slug
                FROM room_links rl
                LEFT JOIN room_link_categories rlc ON rlc.room_link_id = rl.id
                LEFT JOIN categories c ON c.id = rlc.category_id
                  AND ($2::boolean OR (c.is_public = TRUE AND c.enabled = TRUE))
                WHERE rl.enabled = TRUE
                  AND (
                        (
                          rl.include_all_public_categories = TRUE
                          AND EXISTS (
                              SELECT 1 FROM categories tc
                              WHERE tc.slug = $1
                                -- include_all_public_categories rooms must never be
                                -- unlocked by include_non_public: the live-E2E escape
                                -- hatch authorizes only the explicitly configured
                                -- category path, so a private (e.g. live-E2E test)
                                -- category never matches "all public" rooms.
                                AND tc.is_public = TRUE AND tc.enabled = TRUE
                          )
                        )
                        OR c.slug = $1
                  )
                ORDER BY rl.id, c.slug
                """,
                category_slug,
                include_non_public,
            )
        grouped: dict[int, RoomLinkRecord] = {}
        category_map: dict[int, list[str]] = {}
        for row in rows:
            room_id = row["id"]
            if room_id not in grouped:
                grouped[room_id] = RoomLinkRecord(
                    id=row["id"],
                    matrix_room_id=row["matrix_room_id"],
                    include_all_public_categories=row["include_all_public_categories"],
                    allow_relay=row["allow_relay"],
                    full_content=row["full_content"],
                    enabled=row["enabled"],
                    category_slugs=(),
                )
                category_map[room_id] = []
            if row["category_slug"] is not None:
                category_map[room_id].append(row["category_slug"])
        return [
            RoomLinkRecord(
                id=record.id,
                matrix_room_id=record.matrix_room_id,
                include_all_public_categories=record.include_all_public_categories,
                allow_relay=record.allow_relay,
                full_content=record.full_content,
                enabled=record.enabled,
                category_slugs=tuple(category_map[record.id]),
            )
            for record in grouped.values()
        ]


class DiscourseEventRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_event_if_missing(
        self,
        *,
        discourse_topic_id: int,
        discourse_post_id: int,
        event_type: str,
        category_id: int | None,
        author_username: str,
        target_discourse_username: str | None,
        raw_payload_json: dict[str, Any],
    ) -> DiscourseEventRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO discourse_events (
                    discourse_topic_id,
                    discourse_post_id,
                    event_type,
                    category_id,
                    author_username,
                    target_discourse_username,
                    raw_payload_json,
                    discovered_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW())
                ON CONFLICT (discourse_post_id)
                DO UPDATE SET event_type = EXCLUDED.event_type,
                              category_id = EXCLUDED.category_id,
                              author_username = EXCLUDED.author_username,
                              target_discourse_username = EXCLUDED.target_discourse_username,
                              raw_payload_json = EXCLUDED.raw_payload_json
                RETURNING *
                """,
                discourse_topic_id,
                discourse_post_id,
                event_type,
                category_id,
                author_username,
                target_discourse_username,
                json.dumps(raw_payload_json),
            )
        assert row is not None
        return _record_to_discourse_event(row)

    async def get_by_discourse_post_id(self, discourse_post_id: int) -> DiscourseEventRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM discourse_events WHERE discourse_post_id = $1", discourse_post_id
            )
        return _record_to_discourse_event(row) if row is not None else None

    async def get_by_id(self, event_id: int) -> DiscourseEventRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM discourse_events WHERE id = $1", event_id
            )
        return _record_to_discourse_event(row) if row is not None else None


class DeliveryJobRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def enqueue(
        self,
        *,
        event_id: int,
        target_type: TargetType,
        target_mxid: str | None,
        matrix_room_id: str | None,
    ) -> DeliveryJobRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO delivery_jobs (
                    event_id,
                    target_type,
                    target_mxid,
                    matrix_room_id,
                    status,
                    attempts,
                    next_attempt_at,
                    created_at,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, 'pending', 0, NOW(), NOW(), NOW())
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                event_id,
                target_type,
                target_mxid,
                matrix_room_id,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM delivery_jobs
                    WHERE event_id = $1
                      AND target_type = $2
                      AND COALESCE(target_mxid, '') = COALESCE($3, '')
                      AND COALESCE(matrix_room_id, '') = COALESCE($4, '')
                    """,
                    event_id,
                    target_type,
                    target_mxid,
                    matrix_room_id,
                )
        assert row is not None
        return _record_to_delivery_job(row)

    async def claim_next_job(
        self, *, lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS
    ) -> DeliveryJobRecord | None:
        """Claim the next due job and hold a bounded lease on it.

        Claimable jobs are 'pending'/'failed' jobs that are due, plus 'running'
        jobs whose lease has expired (crashed worker recovery). The lease is
        stamped on claim so a worker that dies mid-delivery cannot strand the
        job in 'running' forever; once the lease lapses the next claim cycle
        reclaims it. It also holds the at-least-once caveat: because an expired
        'running' row means the previous attempt's outcome is unknown, callers
        must reconcile against the delivery-message mapping before re-sending
        (see deliver_job).
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH next_job AS (
                    SELECT id
                    FROM delivery_jobs
                    WHERE (
                            status IN ('pending', 'failed')
                            AND next_attempt_at <= NOW()
                        )
                        OR (
                            status = 'running'
                            AND COALESCE(lease_expires_at, NOW()) <= NOW()
                        )
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE delivery_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    claimed_at = NOW(),
                    lease_expires_at = NOW() + make_interval(secs => $1),
                    -- Fencing token: a fresh, unique token per claim so renewal
                    -- and terminal updates from an earlier (stale) claim are
                    -- rejected by the claim_token guard below.
                    claim_token = 'claim-' || id::text || '-' || gen_random_uuid()::text,
                    updated_at = NOW()
                WHERE id IN (SELECT id FROM next_job)
                RETURNING *
                """,
                float(lease_seconds),
            )
        return _record_to_delivery_job(row) if row is not None else None

    async def ensure_matrix_tx_id(self, job_id: int) -> str:
        """Return the job's durable Matrix transaction id, creating it once.

        The id is persisted BEFORE any Matrix write. Every attempt of this job
        reuses it, so a retry after a crash between homeserver acceptance and
        mapping persistence deduplicates on the homeserver instead of posting
        the message twice (Matrix dedupes sends by (device, transaction id)).
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE delivery_jobs
                SET matrix_tx_id = COALESCE(
                        matrix_tx_id,
                        'dischat-' || id::text || '-' || gen_random_uuid()::text
                    ),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING matrix_tx_id
                """,
                job_id,
            )
        assert row is not None
        return row["matrix_tx_id"]

    async def ensure_matrix_device_id(self, job_id: int, *, device_id: str) -> str:
        """Stamp the bot's Matrix device id on the job (idempotent).

        Transaction ids are scoped to a device: deduplication only works if the
        retrying process logs in as the SAME device. The client resolves its
        (stable) device id and stamps it here so restarts reuse it instead of
        minting a new device per password login.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE delivery_jobs
                SET matrix_device_id = COALESCE(matrix_device_id, $2),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING matrix_device_id
                """,
                job_id,
                device_id,
            )
        assert row is not None
        return row["matrix_device_id"]

    async def get_matrix_dm_room_id(self, job_id: int) -> str | None:
        """Return the DM room pinned for this job, if any."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT matrix_dm_room_id FROM delivery_jobs WHERE id = $1", job_id
            )
        assert row is not None
        return row["matrix_dm_room_id"]

    async def pin_matrix_dm_room(self, job_id: int, *, room_id: str) -> str:
        """Pin the DM room a job's sends must go to (idempotent).

        Transaction ids are scoped per HTTP endpoint (/rooms/{roomId}/send/...).
        If a retry re-resolved the DM room and picked a different one, the same
        tx id would hit a different endpoint and could not deduplicate — so the
        first resolved room is persisted and every attempt reuses it.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE delivery_jobs
                SET matrix_dm_room_id = COALESCE(matrix_dm_room_id, $2),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING matrix_dm_room_id
                """,
                job_id,
                room_id,
            )
        assert row is not None
        return row["matrix_dm_room_id"]

    async def renew_lease(
        self, job_id: int, *, claim_token: str, lease_seconds: int
    ) -> datetime | None:
        """Extend the lease on a claimed job if the caller still owns the claim.

        The update is fenced on the claim token AND `status = 'running'`: after
        a lease expiry and reclaim, the previous worker's token no longer
        matches and its renewal is ignored (returns None) instead of extending
        the newer claim's lease.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE delivery_jobs
                SET lease_expires_at = NOW() + make_interval(secs => $3),
                    updated_at = NOW()
                WHERE id = $1 AND claim_token = $2 AND status = 'running'
                RETURNING lease_expires_at
                """,
                job_id,
                claim_token,
                float(lease_seconds),
            )
        return row["lease_expires_at"] if row is not None else None

    async def mark_complete(self, job_id: int, *, claim_token: str) -> bool:
        """Mark a job complete if the caller still owns the claim.

        Returns False (and changes nothing) when the claim token no longer
        matches — e.g. the caller's lease expired and another worker reclaimed
        the job — or when the job is not 'running' anymore.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE delivery_jobs
                SET status = 'complete', updated_at = NOW(), last_error = NULL
                WHERE id = $1 AND claim_token = $2 AND status = 'running'
                RETURNING id
                """,
                job_id,
                claim_token,
            )
        return row is not None

    async def mark_failed(
        self, job_id: int, *, claim_token: str, error: str, next_attempt_at: datetime
    ) -> bool:
        """Mark a job failed (retryable) if the caller still owns the claim.

        Fenced exactly like mark_complete: a stale worker whose job was
        reclaimed cannot clobber the newer claim's state or schedule a
        competing retry. Returns False when the update was rejected.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE delivery_jobs
                SET status = 'failed',
                    last_error = $3,
                    next_attempt_at = $4,
                    updated_at = NOW()
                WHERE id = $1 AND claim_token = $2 AND status = 'running'
                RETURNING id
                """,
                job_id,
                claim_token,
                error,
                next_attempt_at,
            )
        return row is not None

    async def get(self, job_id: int) -> DeliveryJobRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM delivery_jobs WHERE id = $1", job_id)
        return _record_to_delivery_job(row) if row is not None else None


class DeliveryMessageRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_mapping(
        self,
        *,
        discourse_topic_id: int,
        discourse_post_id: int,
        matrix_room_id: str,
        matrix_event_id: str,
        target_type: TargetType,
        target_mxid: str | None,
        parent_delivery_message_id: int | None,
    ) -> DeliveryMessageRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO delivery_messages (
                    discourse_topic_id,
                    discourse_post_id,
                    matrix_room_id,
                    matrix_event_id,
                    target_type,
                    target_mxid,
                    parent_delivery_message_id,
                    created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                discourse_topic_id,
                discourse_post_id,
                matrix_room_id,
                matrix_event_id,
                target_type,
                target_mxid,
                parent_delivery_message_id,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM delivery_messages
                    WHERE discourse_post_id = $1
                      AND matrix_room_id = $2
                      AND target_type = $3
                      AND COALESCE(target_mxid, '') = COALESCE($4, '')
                    """,
                    discourse_post_id,
                    matrix_room_id,
                    target_type,
                    target_mxid,
                )
        assert row is not None
        return _record_to_delivery_message(row)

    async def get_by_matrix_event(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> DeliveryMessageRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM delivery_messages
                WHERE matrix_room_id = $1 AND matrix_event_id = $2
                """,
                matrix_room_id,
                matrix_event_id,
            )
        return _record_to_delivery_message(row) if row is not None else None

    async def get_by_discourse_post_and_room(
        self, *, discourse_post_id: int, matrix_room_id: str
    ) -> DeliveryMessageRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM delivery_messages
                WHERE discourse_post_id = $1 AND matrix_room_id = $2
                ORDER BY id DESC
                LIMIT 1
                """,
                discourse_post_id,
                matrix_room_id,
            )
        return _record_to_delivery_message(row) if row is not None else None

    async def list_by_discourse_post(
        self, *, discourse_post_id: int
    ) -> list[DeliveryMessageRecord]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT * FROM delivery_messages WHERE discourse_post_id = $1 ORDER BY id",
                discourse_post_id,
            )
        return [_record_to_delivery_message(row) for row in rows]


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


class MatrixStateRepository:
    """Durable state for restart-safe Matrix processing (sync token + event ledger)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_sync_next_batch(self) -> str | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow("SELECT next_batch FROM matrix_sync_state")
        if row is None:
            return None
        return str(row["next_batch"])

    async def set_sync_next_batch(self, next_batch: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO matrix_sync_state (singleton, next_batch, updated_at)
                VALUES (TRUE, $1, NOW())
                ON CONFLICT (singleton)
                DO UPDATE SET next_batch = EXCLUDED.next_batch, updated_at = NOW()
                """,
                next_batch,
            )

    async def claim_event(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        lease_owner: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> MatrixEventStateRecord | None:
        """Seed the marker row and take an exclusive processing lease.

        The INSERT is the durable fence: it wins the unique
        ``(room_id, event_id)`` boundary, so exactly one concurrent attempt
        proceeds to the external Discourse write. The lease columns make the
        claim auditable and let a replaying attempt take over only a
        demonstrably stale lease.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO matrix_event_state (
                    room_id,
                    event_id,
                    status,
                    lease_owner,
                    lease_expires_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    $1, $2, 'claimed', $3::text,
                    CASE WHEN $3::text IS NULL THEN NULL
                         ELSE NOW() + make_interval(secs => $4::double precision) END,
                    NOW(), NOW()
                )
                ON CONFLICT (room_id, event_id) DO UPDATE SET
                    lease_expires_at = CASE WHEN $3::text IS NULL THEN NULL
                        ELSE NOW() + make_interval(secs => $4::double precision) END,
                    updated_at = NOW()
                WHERE matrix_event_state.status = 'claimed'
                  AND $3::text IS NOT NULL
                  AND matrix_event_state.lease_owner = $3::text
                RETURNING id, room_id, event_id, status,
                          discourse_topic_id, discourse_post_id, response_notice
                """,
                matrix_room_id,
                matrix_event_id,
                lease_owner,
                lease_seconds,
            )
        return _record_to_matrix_event_state(row) if row is not None else None

    async def adopt_event(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        lease_owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> MatrixEventStateRecord | None:
        """Atomically take exclusive ownership of an orphaned marker.

        Exactly one concurrent caller can win this transition: the row moves
        from ``claimed``/``owned`` to the ``owned`` state under the caller's
        ``lease_owner`` token, so a loser gets ``None`` back and must not
        perform the external write. Takeover only succeeds when the fence is
        demonstrably stale — either the previous owner never held a lease (a
        pre-lease marker) or its lease has already lapsed — so a live worker
        is never dispossessed. A fence that lapsed *before* adoption (status
        ``claimed``) and one that lapsed *after* adoption but before any
        external write (status ``owned``, e.g. the process died between the
        write call returning and ``mark_event_written``) are both recoverable;
        ``written``/``processed`` markers are not.

        Returns the owned record when this caller now holds the fence;
        ``None`` otherwise (another attempt owns it, or an outcome was
        already recorded as ``written``/``processed``).
        """
        if lease_owner is None:
            raise ValueError("lease_owner is required to adopt an event")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE matrix_event_state
                SET status = 'owned',
                    lease_owner = $3,
                    lease_expires_at = NOW() + make_interval(secs => $4::double precision),
                    updated_at = NOW()
                WHERE room_id = $1 AND event_id = $2
                  AND status IN ('claimed', 'owned')
                  AND (
                      lease_expires_at IS NULL
                      OR lease_expires_at <= NOW()
                  )
                RETURNING id, room_id, event_id, status,
                          discourse_topic_id, discourse_post_id, response_notice
                """,
                matrix_room_id,
                matrix_event_id,
                lease_owner,
                lease_seconds,
            )
        return _record_to_matrix_event_state(row) if row is not None else None

    async def mark_event_written(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        discourse_topic_id: int | None = None,
        discourse_post_id: int | None = None,
        response_notice: str | None = None,
        lease_owner: str | None = None,
    ) -> EventOutcome:
        """Durably record the external write of a fenced event.

        This is the reconciliation primitive behind both reply and command
        fencing: the row answers "did an external write already happen, and
        what did it produce?". A crash between the external write and the
        delivery mapping leaves a ``written`` marker, and the replay adopts
        the recorded outcome instead of writing a duplicate.

        Only the attempt that holds the fence may stamp its outcome here:
        with ``lease_owner`` set, the marker must still be in the state this
        attempt left it in (``claimed`` for a fresh claim, ``owned`` after an
        adoption) and carry this attempt's lease token. A superseded attempt
        — one that raced a takeover and lost — therefore cannot overwrite the
        winner's record; it learns the winning outcome and must surface that
        instead of its own.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE matrix_event_state
                SET status = 'written',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    discourse_topic_id = COALESCE($3, discourse_topic_id),
                    discourse_post_id = COALESCE($4, discourse_post_id),
                    response_notice = COALESCE($5, response_notice),
                    updated_at = NOW()
                WHERE room_id = $1 AND event_id = $2
                  AND status IN ('claimed', 'owned')
                  AND ($6::text IS NULL OR lease_owner = $6::text)
                RETURNING discourse_topic_id, discourse_post_id
                """,
                matrix_room_id,
                matrix_event_id,
                discourse_topic_id,
                discourse_post_id,
                response_notice,
                lease_owner,
            )
            if row is not None:
                topic_id = row["discourse_topic_id"]
                return EventOutcome(
                    recorded=True,
                    conflicting_topic_id=int(topic_id) if topic_id is not None else None,
                    conflicting_post_id=int(row["discourse_post_id"])
                    if row["discourse_post_id"] is not None
                    else None,
                )
            existing = await self.get_event(
                matrix_room_id=matrix_room_id, matrix_event_id=matrix_event_id
            )
        return EventOutcome(
            recorded=False,
            conflicting_topic_id=existing.discourse_topic_id if existing else None,
            conflicting_post_id=existing.discourse_post_id if existing else None,
        )

    async def mark_event_processed(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        lease_owner: str | None = None,
    ) -> None:
        """Confirm the marker once all side effects are durably recorded.

        Like every other ledger primitive, the transition is guarded: with a
        lease token the marker must still belong to this attempt, and it must
        be in a pre-confirmation state (``claimed``/``owned``/``written``).
        A caller that lost the fence — or one racing a just-confirmed
        replay — updates nothing instead of confirming over the winner.
        """
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE matrix_event_state
                SET status = 'processed', updated_at = NOW()
                WHERE room_id = $1 AND event_id = $2
                  AND status IN ('claimed', 'owned', 'written')
                  AND ($3::text IS NULL OR lease_owner = $3::text)
                """,
                matrix_room_id,
                matrix_event_id,
                lease_owner,
            )

    async def prune_processed_events(self, *, older_than_days: int = 7) -> int:
        """Delete confirmed markers past the retention window.

        The fence only needs a ``processed`` marker to outlive the Matrix
        ``/sync`` replay horizon (bounded by the stored sync token), so old
        confirmed rows are safe to drop. Rows in ``claimed``, ``owned``, or
        ``written`` states are never deleted: removing one could re-open an
        external write. Returns the number of rows removed.
        """
        async with self._pool.acquire() as connection:
            status = await connection.execute(
                """
                DELETE FROM matrix_event_state
                WHERE status = 'processed'
                  AND updated_at < NOW() - make_interval(days => $1::int)
                """,
                older_than_days,
            )
        return int(status.split()[-1])

    async def get_event(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> MatrixEventStateRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, room_id, event_id, status,
                       discourse_topic_id, discourse_post_id, response_notice
                FROM matrix_event_state
                WHERE room_id = $1 AND event_id = $2
                """,
                matrix_room_id,
                matrix_event_id,
            )
        return _record_to_matrix_event_state(row) if row is not None else None

    async def release_event(
        self, *, matrix_room_id: str, matrix_event_id: str, lease_owner: str | None = None
    ) -> None:
        """Give up an unprocessed claim so a later delivery can retry cleanly.

        Only a marker that provably made no external write can be released:
        once the outcome is recorded (``written``) or confirmed
        (``processed``), the fence must survive so a replay never re-writes.
        With ``lease_owner`` set, the marker must still carry this attempt's
        token — a superseded attempt must never tear down the fence of the
        attempt that took the lease over.
        """
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM matrix_event_state
                WHERE room_id = $1 AND event_id = $2
                  AND status IN ('claimed', 'owned')
                  AND ($3::text IS NULL OR lease_owner = $3::text)
                """,
                matrix_room_id,
                matrix_event_id,
                lease_owner,
            )


def _record_to_matrix_event_state(row: asyncpg.Record) -> MatrixEventStateRecord:
    return MatrixEventStateRecord(
        id=row["id"],
        room_id=row["room_id"],
        event_id=row["event_id"],
        status=row["status"],
        discourse_topic_id=row["discourse_topic_id"],
        discourse_post_id=row["discourse_post_id"],
        response_notice=row["response_notice"],
    )
