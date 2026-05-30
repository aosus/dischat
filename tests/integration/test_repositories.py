from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from dischat.security.audit import AuditEntry
from dischat.storage.repositories import (
    AuditLogRepository,
    CategoryRepository,
    ChatAccountRepository,
    DeliveryJobRepository,
    DeliveryMessageRepository,
    DiscourseEventRepository,
    PairingSessionRepository,
    RoomLinkRepository,
    UserWatchRepository,
)


async def test_migrations_apply_and_account_pairing_round_trip(pg_pool) -> None:
    accounts = ChatAccountRepository(pg_pool)

    created = await accounts.ensure_account(
        mxid="@alice:aosus.org",
        platform="matrix",
        response_locale="ar",
    )
    paired = await accounts.pair_account(mxid="@alice:aosus.org", discourse_username="alice")
    fetched = await accounts.get_by_mxid("@alice:aosus.org")

    assert created.mxid == "@alice:aosus.org"
    assert paired.discourse_username == "alice"
    assert fetched is not None
    assert fetched.discourse_username == "alice"


async def test_pairing_sessions_replace_previous_active_session(pg_pool) -> None:
    pairing = PairingSessionRepository(pg_pool)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)

    first = await pairing.create_session(
        mxid="@alice:aosus.org",
        discourse_username="alice",
        code_hash="hash-1",
        expires_at=expires_at,
    )
    second = await pairing.create_session(
        mxid="@alice:aosus.org",
        discourse_username="alice",
        code_hash="hash-2",
        expires_at=expires_at,
    )
    active = await pairing.get_active_session("@alice:aosus.org")

    assert first.id != second.id
    assert active is not None
    assert active.code_hash == "hash-2"


async def test_user_watches_and_room_links_round_trip(pg_pool) -> None:
    categories = CategoryRepository(pg_pool)
    watches = UserWatchRepository(pg_pool)
    room_links = RoomLinkRepository(pg_pool)

    support = await categories.upsert_category(
        discourse_category_id=10,
        slug="support",
        name="Support",
        is_public=True,
    )
    await watches.add_category_watch(mxid="@alice:aosus.org", category_id=support.id)
    await watches.add_watch_all(mxid="@alice:aosus.org")
    listed = await watches.list_watches_for_mxid("@alice:aosus.org")

    await room_links.replace_room_links(
        {
            "!room:test": {
                "categories": ["support"],
                "allow_relay": True,
                "full_content": True,
            }
        },
        {"support": support.id},
    )
    linked = await room_links.get_by_room_id("!room:test")

    assert len(listed) == 2
    assert linked is not None
    assert linked.allow_relay is True
    assert linked.category_slugs == ("support",)


async def test_event_and_delivery_deduplication(pg_pool) -> None:
    events = DiscourseEventRepository(pg_pool)
    jobs = DeliveryJobRepository(pg_pool)
    messages = DeliveryMessageRepository(pg_pool)

    event = await events.create_event_if_missing(
        discourse_topic_id=1,
        discourse_post_id=2,
        event_type="new_topic",
        category_id=10,
        author_username="alice",
        target_discourse_username=None,
        raw_payload_json={"id": 2, "topic_id": 1, "username": "alice"},
    )
    duplicate = await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:test",
    )
    duplicate_again = await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:test",
    )
    mapped = await messages.create_mapping(
        discourse_topic_id=1,
        discourse_post_id=2,
        matrix_room_id="!room:test",
        matrix_event_id="$event",
        target_type="room",
        target_mxid=None,
        parent_delivery_message_id=None,
    )
    mapped_again = await messages.create_mapping(
        discourse_topic_id=1,
        discourse_post_id=2,
        matrix_room_id="!room:test",
        matrix_event_id="$event-2",
        target_type="room",
        target_mxid=None,
        parent_delivery_message_id=None,
    )

    assert duplicate.id == duplicate_again.id
    assert mapped.id == mapped_again.id


async def test_delivery_job_claiming_is_atomic(pg_pool) -> None:
    events = DiscourseEventRepository(pg_pool)
    jobs = DeliveryJobRepository(pg_pool)

    event = await events.create_event_if_missing(
        discourse_topic_id=1,
        discourse_post_id=3,
        event_type="new_topic",
        category_id=10,
        author_username="alice",
        target_discourse_username=None,
        raw_payload_json={"id": 3, "topic_id": 1, "username": "alice"},
    )
    await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:test",
    )

    claimed = await asyncio.gather(jobs.claim_next_job(), jobs.claim_next_job())
    ids = {job.id for job in claimed if job is not None}

    assert len(ids) == 1
    assert sum(job is None for job in claimed) == 1


async def test_audit_logs_are_persisted(pg_pool) -> None:
    audit = AuditLogRepository(pg_pool)
    await audit.record(
        AuditEntry(
            action="create_discourse_reply",
            mxid="@alice:aosus.org",
            platform="matrix",
            discourse_username_used="alice",
            topic_id=1,
            post_id=2,
            matrix_room_id="!room:test",
            matrix_event_id="$event",
            success=True,
        )
    )

    async with pg_pool.acquire() as connection:
        count = await connection.fetchval("SELECT COUNT(*) FROM audit_logs")

    assert count == 1
