from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg

from dischat.security.audit import AuditEntry

WatchMode = Literal['category', 'all_public_categories']
JobStatus = Literal['pending', 'running', 'complete', 'failed']
TargetType = Literal['dm', 'room']


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


def _record_to_chat_account(row: asyncpg.Record) -> ChatAccount:
    return ChatAccount(
        id=row['id'],
        mxid=row['mxid'],
        platform=row['platform'],
        discourse_user_id=row['discourse_user_id'],
        discourse_username=row['discourse_username'],
        paired_at=row['paired_at'],
        revoked_at=row['revoked_at'],
        notify_on_direct_replies=row['notify_on_direct_replies'],
        notify_on_mentions=row['notify_on_mentions'],
        response_locale=row['response_locale'],
    )


class ChatAccountRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_account(self, *, mxid: str, platform: str, response_locale: str) -> ChatAccount:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                INSERT INTO chat_accounts (mxid, platform, response_locale, created_at, updated_at)
                VALUES ($1, $2, $3, NOW(), NOW())
                ON CONFLICT (mxid)
                DO UPDATE SET platform = EXCLUDED.platform, response_locale = EXCLUDED.response_locale, updated_at = NOW()
                RETURNING *
                ''',
                mxid,
                platform,
                response_locale,
            )
        assert row is not None
        return _record_to_chat_account(row)

    async def get_by_mxid(self, mxid: str) -> ChatAccount | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow('SELECT * FROM chat_accounts WHERE mxid = $1', mxid)
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
                '''
                UPDATE chat_accounts
                SET discourse_username = $2,
                    discourse_user_id = $3,
                    paired_at = NOW(),
                    revoked_at = NULL,
                    updated_at = NOW()
                WHERE mxid = $1
                RETURNING *
                ''',
                mxid,
                discourse_username,
                discourse_user_id,
            )
        assert row is not None
        return _record_to_chat_account(row)

    async def unpair_account(self, *, mxid: str) -> ChatAccount | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                UPDATE chat_accounts
                SET discourse_username = NULL,
                    discourse_user_id = NULL,
                    revoked_at = NOW(),
                    updated_at = NOW()
                WHERE mxid = $1
                RETURNING *
                ''',
                mxid,
            )
        return _record_to_chat_account(row) if row is not None else None

    async def list_by_discourse_username(self, discourse_username: str) -> list[ChatAccount]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                '''
                SELECT * FROM chat_accounts
                WHERE discourse_username = $1 AND revoked_at IS NULL
                ORDER BY id
                ''',
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
                await connection.execute('DELETE FROM pairing_sessions WHERE mxid = $1 AND consumed_at IS NULL', mxid)
                row = await connection.fetchrow(
                    '''
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
                    ''',
                    mxid,
                    discourse_username,
                    discourse_user_id,
                    code_hash,
                    expires_at,
                )
        assert row is not None
        return PairingSessionRecord(**dict(row))

    async def get_active_session(self, mxid: str) -> PairingSessionRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                SELECT * FROM pairing_sessions
                WHERE mxid = $1 AND consumed_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                mxid,
            )
        return PairingSessionRecord(**dict(row)) if row is not None else None

    async def increment_attempt_count(self, session_id: int) -> PairingSessionRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                UPDATE pairing_sessions
                SET attempt_count = attempt_count + 1
                WHERE id = $1
                RETURNING *
                ''',
                session_id,
            )
        assert row is not None
        return PairingSessionRecord(**dict(row))

    async def consume_session(self, session_id: int) -> PairingSessionRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                UPDATE pairing_sessions
                SET consumed_at = NOW()
                WHERE id = $1
                RETURNING *
                ''',
                session_id,
            )
        assert row is not None
        return PairingSessionRecord(**dict(row))

    async def cancel_session(self, mxid: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                '''
                UPDATE pairing_sessions
                SET consumed_at = NOW()
                WHERE mxid = $1 AND consumed_at IS NULL
                ''',
                mxid,
            )


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
                '''
                INSERT INTO categories (discourse_category_id, slug, name, is_public, enabled, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (discourse_category_id)
                DO UPDATE SET slug = EXCLUDED.slug,
                              name = EXCLUDED.name,
                              is_public = EXCLUDED.is_public,
                              enabled = EXCLUDED.enabled,
                              updated_at = NOW()
                RETURNING *
                ''',
                discourse_category_id,
                slug,
                name,
                is_public,
                enabled,
            )
        assert row is not None
        return CategoryRecord(**dict(row))

    async def list_categories(self) -> list[CategoryRecord]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch('SELECT * FROM categories WHERE enabled = TRUE ORDER BY slug')
        return [CategoryRecord(**dict(row)) for row in rows]

    async def get_by_slug(self, slug: str) -> CategoryRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow('SELECT * FROM categories WHERE slug = $1 AND enabled = TRUE', slug)
        return CategoryRecord(**dict(row)) if row is not None else None

    async def get_by_discourse_category_id(self, discourse_category_id: int) -> CategoryRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                'SELECT * FROM categories WHERE discourse_category_id = $1', discourse_category_id
            )
        return CategoryRecord(**dict(row)) if row is not None else None


class UserWatchRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def add_category_watch(self, *, mxid: str, category_id: int) -> UserWatchRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                INSERT INTO user_watches (mxid, mode, category_id, created_at)
                VALUES ($1, 'category', $2, NOW())
                ON CONFLICT DO NOTHING
                RETURNING id, mxid, mode, category_id, NULL::TEXT AS category_slug
                ''',
                mxid,
                category_id,
            )
            if row is None:
                row = await connection.fetchrow(
                    '''
                    SELECT uw.id, uw.mxid, uw.mode, uw.category_id, c.slug AS category_slug
                    FROM user_watches uw
                    LEFT JOIN categories c ON c.id = uw.category_id
                    WHERE uw.mxid = $1 AND uw.mode = 'category' AND uw.category_id = $2
                    ''',
                    mxid,
                    category_id,
                )
        assert row is not None
        return UserWatchRecord(**dict(row))

    async def add_watch_all(self, *, mxid: str) -> UserWatchRecord:
        async with self._pool.acquire() as connection:
            await connection.execute(
                '''
                DELETE FROM user_watches
                WHERE mxid = $1 AND mode = 'all_public_categories' AND category_id IS NULL
                ''',
                mxid,
            )
            row = await connection.fetchrow(
                '''
                INSERT INTO user_watches (mxid, mode, category_id, created_at)
                VALUES ($1, 'all_public_categories', NULL, NOW())
                RETURNING id, mxid, mode, category_id, NULL::TEXT AS category_slug
                ''',
                mxid,
            )
        assert row is not None
        return UserWatchRecord(**dict(row))

    async def remove_category_watch(self, *, mxid: str, category_id: int) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                '''
                DELETE FROM user_watches
                WHERE mxid = $1 AND mode = 'category' AND category_id = $2
                ''',
                mxid,
                category_id,
            )

    async def remove_watch_all(self, *, mxid: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                '''
                DELETE FROM user_watches
                WHERE mxid = $1 AND mode = 'all_public_categories' AND category_id IS NULL
                ''',
                mxid,
            )

    async def list_watches_for_mxid(self, mxid: str) -> list[UserWatchRecord]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                '''
                SELECT uw.id, uw.mxid, uw.mode, uw.category_id, c.slug AS category_slug
                FROM user_watches uw
                LEFT JOIN categories c ON c.id = uw.category_id
                WHERE uw.mxid = $1
                ORDER BY uw.mode, c.slug NULLS FIRST
                ''',
                mxid,
            )
        return [UserWatchRecord(**dict(row)) for row in rows]


class RoomLinkRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def replace_room_links(self, room_links: dict[str, dict[str, Any]], category_lookup: dict[str, int]) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute('DELETE FROM room_link_categories')
                await connection.execute('DELETE FROM room_links')
                for room_id, config in room_links.items():
                    categories = list(config.get('categories', []))
                    include_all = 'all' in categories
                    row = await connection.fetchrow(
                        '''
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
                        ''',
                        room_id,
                        include_all,
                        bool(config.get('allow_relay', False)),
                        bool(config.get('full_content', False)),
                    )
                    assert row is not None
                    room_link_id = row['id']
                    for slug in categories:
                        if slug == 'all':
                            continue
                        category_id = category_lookup.get(slug)
                        if category_id is None:
                            continue
                        await connection.execute(
                            '''
                            INSERT INTO room_link_categories (room_link_id, category_id)
                            VALUES ($1, $2)
                            ON CONFLICT DO NOTHING
                            ''',
                            room_link_id,
                            category_id,
                        )

    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord | None:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                '''
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
                ''',
                matrix_room_id,
            )
        if not rows:
            return None
        first = rows[0]
        return RoomLinkRecord(
            id=first['id'],
            matrix_room_id=first['matrix_room_id'],
            include_all_public_categories=first['include_all_public_categories'],
            allow_relay=first['allow_relay'],
            full_content=first['full_content'],
            enabled=first['enabled'],
            category_slugs=tuple(row['category_slug'] for row in rows if row['category_slug'] is not None),
        )

    async def list_links_matching_category(self, category_slug: str) -> list[RoomLinkRecord]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                '''
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
                WHERE rl.enabled = TRUE
                  AND (rl.include_all_public_categories = TRUE OR c.slug = $1)
                ORDER BY rl.id, c.slug
                ''',
                category_slug,
            )
        grouped: dict[int, RoomLinkRecord] = {}
        category_map: dict[int, list[str]] = {}
        for row in rows:
            room_id = row['id']
            if room_id not in grouped:
                grouped[room_id] = RoomLinkRecord(
                    id=row['id'],
                    matrix_room_id=row['matrix_room_id'],
                    include_all_public_categories=row['include_all_public_categories'],
                    allow_relay=row['allow_relay'],
                    full_content=row['full_content'],
                    enabled=row['enabled'],
                    category_slugs=(),
                )
                category_map[room_id] = []
            if row['category_slug'] is not None:
                category_map[room_id].append(row['category_slug'])
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
                '''
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
                ''',
                discourse_topic_id,
                discourse_post_id,
                event_type,
                category_id,
                author_username,
                target_discourse_username,
                json.dumps(raw_payload_json),
            )
        assert row is not None
        return DiscourseEventRecord(**dict(row))

    async def get_by_discourse_post_id(self, discourse_post_id: int) -> DiscourseEventRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                'SELECT * FROM discourse_events WHERE discourse_post_id = $1', discourse_post_id
            )
        return DiscourseEventRecord(**dict(row)) if row is not None else None

    async def get_by_id(self, event_id: int) -> DiscourseEventRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow('SELECT * FROM discourse_events WHERE id = $1', event_id)
        return DiscourseEventRecord(**dict(row)) if row is not None else None


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
                '''
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
                ''',
                event_id,
                target_type,
                target_mxid,
                matrix_room_id,
            )
            if row is None:
                row = await connection.fetchrow(
                    '''
                    SELECT * FROM delivery_jobs
                    WHERE event_id = $1
                      AND target_type = $2
                      AND COALESCE(target_mxid, '') = COALESCE($3, '')
                      AND COALESCE(matrix_room_id, '') = COALESCE($4, '')
                    ''',
                    event_id,
                    target_type,
                    target_mxid,
                    matrix_room_id,
                )
        assert row is not None
        return DeliveryJobRecord(**dict(row))

    async def claim_next_job(self) -> DeliveryJobRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                WITH next_job AS (
                    SELECT id
                    FROM delivery_jobs
                    WHERE status IN ('pending', 'failed')
                      AND next_attempt_at <= NOW()
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE delivery_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    updated_at = NOW()
                WHERE id IN (SELECT id FROM next_job)
                RETURNING *
                '''
            )
        return DeliveryJobRecord(**dict(row)) if row is not None else None

    async def mark_complete(self, job_id: int) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                '''
                UPDATE delivery_jobs
                SET status = 'complete', updated_at = NOW(), last_error = NULL
                WHERE id = $1
                ''',
                job_id,
            )

    async def mark_failed(self, job_id: int, *, error: str, next_attempt_at: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                '''
                UPDATE delivery_jobs
                SET status = 'failed',
                    last_error = $2,
                    next_attempt_at = $3,
                    updated_at = NOW()
                WHERE id = $1
                ''',
                job_id,
                error,
                next_attempt_at,
            )

    async def get(self, job_id: int) -> DeliveryJobRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow('SELECT * FROM delivery_jobs WHERE id = $1', job_id)
        return DeliveryJobRecord(**dict(row)) if row is not None else None


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
                '''
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
                ''',
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
                    '''
                    SELECT * FROM delivery_messages
                    WHERE discourse_post_id = $1
                      AND matrix_room_id = $2
                      AND target_type = $3
                      AND COALESCE(target_mxid, '') = COALESCE($4, '')
                    ''',
                    discourse_post_id,
                    matrix_room_id,
                    target_type,
                    target_mxid,
                )
        assert row is not None
        return DeliveryMessageRecord(**dict(row))

    async def get_by_matrix_event(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> DeliveryMessageRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                SELECT * FROM delivery_messages
                WHERE matrix_room_id = $1 AND matrix_event_id = $2
                ''',
                matrix_room_id,
                matrix_event_id,
            )
        return DeliveryMessageRecord(**dict(row)) if row is not None else None

    async def get_by_discourse_post_and_room(
        self, *, discourse_post_id: int, matrix_room_id: str
    ) -> DeliveryMessageRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                '''
                SELECT * FROM delivery_messages
                WHERE discourse_post_id = $1 AND matrix_room_id = $2
                ORDER BY id DESC
                LIMIT 1
                ''',
                discourse_post_id,
                matrix_room_id,
            )
        return DeliveryMessageRecord(**dict(row)) if row is not None else None


class AuditLogRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(self, entry: AuditEntry) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                '''
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
                ''',
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
