"""Regression tests for issue #9: restart-safe, idempotent Matrix event processing.

Uses the real Postgres schema (testcontainers) for the durable state pieces and
in-memory fakes for the Matrix/Discourse boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from dischat.bridge import handle_matrix_reply
from dischat.discourse.sync import PollerState
from dischat.main import run_iteration
from dischat.matrix.client import (
    MatrixMessage,
    MatrixSendResult,
    NioMatrixClient,
    event_notice_tx_id,
)
from dischat.matrix.handler import process_sync_messages
from dischat.security.audit import AuditEntry
from dischat.service import DischatService, ServiceResponse
from dischat.storage.db import apply_sql_migrations
from dischat.storage.repositories import (
    ChatAccount,
    DeliveryMessageRecord,
    MatrixStateRepository,
    RoomLinkRecord,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "dischat" / "storage" / "migrations"


class FakeDiscourseClient:
    """Records create_reply calls; each call "succeeds" on Discourse's side."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_post(self, post_id: int) -> dict[str, object]:
        return {"post_number": 2}

    async def create_reply(
        self,
        *,
        topic_id: int,
        raw: str,
        reply_to_post_number: int | None = None,
        api_username: str | None = None,
    ) -> FakeDiscourseWriteResult:
        self.calls.append(
            {
                "topic_id": topic_id,
                "raw": raw,
                "reply_to_post_number": reply_to_post_number,
                "api_username": api_username,
            }
        )
        return FakeDiscourseWriteResult(post_id=100 + len(self.calls), topic_id=topic_id)


@dataclass(slots=True)
class FakeDiscourseWriteResult:
    post_id: int
    topic_id: int


class FakeNoticeMatrixClient:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []
        self.notice_calls: list[tuple[str, str, str | None]] = []
        self.notice_results_by_tx_id: dict[str, MatrixSendResult] = {}
        self.extract_messages_return: list[MatrixMessage] = []
        self.user_id = "@bridge:aosus.org"

    async def send_notice(
        self, room_id: str, body: str, *, tx_id: str | None = None
    ) -> MatrixSendResult:
        self.notice_calls.append((room_id, body, tx_id))
        if tx_id is not None and tx_id in self.notice_results_by_tx_id:
            return self.notice_results_by_tx_id[tx_id]
        self.notices.append((room_id, body))
        result = MatrixSendResult(event_id="$notice", room_id=room_id)
        if tx_id is not None:
            self.notice_results_by_tx_id[tx_id] = result
        return result

    def extract_messages(self, sync_response) -> list[MatrixMessage]:
        return self.extract_messages_return


class CrashDuringNoticeMatrixClient(FakeNoticeMatrixClient):
    """send_notice raises, simulating a crash after the external write."""

    def __init__(self) -> None:
        super().__init__()
        self.crash_on_notice = True

    async def send_notice(
        self, room_id: str, body: str, *, tx_id: str | None = None
    ) -> MatrixSendResult:
        result = await super().send_notice(room_id, body, tx_id=tx_id)
        if self.crash_on_notice:
            raise RuntimeError("simulated crash after the external write")
        return result


class FakeDiscoursePrivateMessages:
    """Records create_private_message calls (the /pair side effect)."""

    def __init__(self) -> None:
        self.pm_calls: list[dict[str, Any]] = []

    async def create_private_message(
        self,
        *,
        target_username: str,
        title: str,
        raw: str,
        api_username: str | None = None,
    ) -> SimpleNamespace:
        self.pm_calls.append({"target_username": target_username, "title": title, "raw": raw})
        return SimpleNamespace(post_id=500, topic_id=600, raw=raw, post_number=1)


class FakeChatAccounts:
    async def ensure_account(
        self, *, mxid: str, platform: str, response_locale: str
    ) -> ChatAccount:
        return ChatAccount(
            id=1,
            mxid=mxid,
            platform=platform,
            discourse_user_id=None,
            discourse_username="alice",
            paired_at=None,
            revoked_at=None,
            notify_on_direct_replies=True,
            notify_on_mentions=True,
            response_locale=response_locale,
        )


class FakeRoomLinks:
    def __init__(self, room_link: RoomLinkRecord | None) -> None:
        self.room_link = room_link

    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord | None:
        return self.room_link


class FakeAuditLogs:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> int:
        self.entries.append(entry)
        return len(self.entries)

    async def update_outcome(self, audit_log_id: int, **kwargs) -> None:
        return None


class FakeDeliveryMessages:
    """In-memory DeliveryMessagesRepo double with a known parent mapping."""

    def __init__(self) -> None:
        self.mappings: dict[tuple[str, str], DeliveryMessageRecord] = {}
        self.fail_next_mapping = False
        self.parent_record = DeliveryMessageRecord(
            id=1,
            discourse_topic_id=20,
            discourse_post_id=30,
            matrix_room_id="!room:test",
            matrix_event_id="$parent",
            target_type="room",
            target_mxid=None,
            parent_delivery_message_id=None,
        )

    async def get_by_matrix_event(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> DeliveryMessageRecord | None:
        if matrix_event_id == "$parent":
            return self.parent_record
        return self.mappings.get((matrix_room_id, matrix_event_id))

    async def create_mapping(
        self,
        *,
        discourse_topic_id: int,
        discourse_post_id: int,
        matrix_room_id: str,
        matrix_event_id: str,
        target_type: Any,
        target_mxid: str | None,
        parent_delivery_message_id: int | None,
    ) -> DeliveryMessageRecord:
        if self.fail_next_mapping:
            self.fail_next_mapping = False
            raise RuntimeError("simulated mapping persistence failure")
        record = DeliveryMessageRecord(
            id=len(self.mappings) + 2,
            discourse_topic_id=discourse_topic_id,
            discourse_post_id=discourse_post_id,
            matrix_room_id=matrix_room_id,
            matrix_event_id=matrix_event_id,
            target_type=target_type,
            target_mxid=target_mxid,
            parent_delivery_message_id=parent_delivery_message_id,
        )
        self.mappings[(matrix_room_id, matrix_event_id)] = record
        return record


def make_reply_message(event_id: str) -> MatrixMessage:
    return MatrixMessage(
        event_id=event_id,
        room_id="!room:test",
        sender="@alice:aosus.org",
        body="hello discourse",
        parent_event_id="$parent",
    )


async def test_duplicate_reply_event_creates_exactly_one_discourse_reply(pg_pool) -> None:
    """Processing the same inbound reply event twice → exactly one Discourse reply."""
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)
    discourse = FakeDiscourseClient()
    message = make_reply_message("$reply-event-1")

    first = await handle_matrix_reply(
        message=message,
        discourse_client=discourse,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )
    replay = await handle_matrix_reply(
        message=message,
        discourse_client=discourse,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    assert first.posted is True
    # The replay short-circuits at the durable fence before writing again.
    assert replay.posted is False
    assert len(discourse.calls) == 1


async def test_replay_after_completed_write_reports_existing_post(pg_pool) -> None:
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)
    discourse = FakeDiscourseClient()
    message = make_reply_message("$reply-event-2")

    first = await handle_matrix_reply(
        message=message,
        discourse_client=discourse,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )
    # Simulate retention/operator cleanup removing the replay marker while the
    # durable Matrix-event mapping remains. The mapping must prevent a fresh
    # claim from reopening the Discourse write.
    async with pg_pool.acquire() as connection:
        await connection.execute(
            "DELETE FROM matrix_event_state WHERE room_id = $1 AND event_id = $2",
            message.room_id,
            message.event_id,
        )
    replay = await handle_matrix_reply(
        message=message,
        discourse_client=discourse,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    assert first.posted is True
    assert replay.discourse_post_id == first.discourse_post_id
    assert replay.error_message is None
    assert len(discourse.calls) == 1


async def test_existing_mapping_reconciles_ambiguous_event_marker(pg_pool) -> None:
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)
    discourse = FakeDiscourseClient()
    message = make_reply_message("$mapped-owned-event")

    first = await handle_matrix_reply(
        message=message,
        discourse_client=discourse,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )
    assert first.posted is True
    async with pg_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE matrix_event_state
            SET status = 'owned', lease_owner = 'dead-owner'
            WHERE room_id = $1 AND event_id = $2
            """,
            message.room_id,
            message.event_id,
        )

    replay = await handle_matrix_reply(
        message=message,
        discourse_client=discourse,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    assert replay.discourse_post_id == first.discourse_post_id
    assert len(discourse.calls) == 1
    marker = await matrix_state.get_event(
        matrix_room_id=message.room_id, matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "processed"


async def test_ambiguous_discourse_transport_failure_keeps_owned_fence(pg_pool) -> None:
    """A transport exception cannot prove that Discourse committed nothing."""
    from typing import cast

    from dischat.bridge import DiscourseReplyWriter

    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)

    class ExplodingDiscourse(FakeDiscourseClient):
        async def create_reply(self, **kwargs):
            raise RuntimeError("discourse unavailable")

    message = make_reply_message("$reply-event-3")
    failing = ExplodingDiscourse()
    call_kwargs: dict[str, Any] = dict(
        message=message,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    try:
        await handle_matrix_reply(
            discourse_client=cast(DiscourseReplyWriter, failing), **call_kwargs
        )  # type: ignore[arg-type]
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the Discourse failure to propagate")

    # The fence remains owned, so a later attempt fails closed instead of
    # risking a duplicate reply.
    healthy = FakeDiscourseClient()
    recovered = await handle_matrix_reply(discourse_client=healthy, **call_kwargs)

    assert recovered.posted is False
    assert recovered.error_message == "event_write_ambiguous"
    assert len(healthy.calls) == 0
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "owned"


async def test_bridge_failure_after_external_write_does_not_release_fence(pg_pool) -> None:
    """A failure AFTER create_reply returned must not release the fence.

    If mapping persistence (or the outcome recording itself) fails once the
    Discourse write completed, releasing the marker would let a replay claim
    the event and write a guaranteed duplicate. The fence must stay standing
    — either the recorded outcome makes it 'written' (replay reconciles), or
    an `owned` marker fails closed for operator reconciliation.
    """
    from typing import cast as _cast

    from dischat.bridge import DiscourseReplyWriter

    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)

    class ExplodingMappingDiscourse(FakeDiscourseClient):
        """The write succeeds, then create_mapping persistence fails."""

        async def create_reply(self, **kwargs):
            result = await super().create_reply(**kwargs)
            self.last_write_result = result
            delivery_messages.fail_next_mapping = True
            return result

    message = make_reply_message("$reply-event-post-write-failure")
    failing = ExplodingMappingDiscourse()
    call_kwargs: dict[str, Any] = dict(
        message=message,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    try:
        await handle_matrix_reply(
            discourse_client=_cast(DiscourseReplyWriter, failing), **call_kwargs
        )  # type: ignore[arg-type]
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the mapping failure to propagate")

    # The fence was NOT released: the marker survives with the recorded
    # outcome, even though the mapping never committed.
    assert delivery_messages.mappings == {}
    written_post_id = failing.last_write_result.post_id
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None
    assert marker.status == "written"
    assert marker.discourse_post_id == written_post_id
    assert len(failing.calls) == 1

    # A replay reconciles from the recorded outcome: no duplicate write.
    recovered = await handle_matrix_reply(discourse_client=FakeDiscourseClient(), **call_kwargs)
    assert recovered.posted is False
    assert recovered.discourse_post_id == written_post_id
    assert len(failing.calls) == 1
    assert len(delivery_messages.mappings) == 1
    final_marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert final_marker is not None and final_marker.status == "processed"


@dataclass(slots=True)
class _Settings:
    poll_interval_seconds: int = 15
    discourse_relay_matrix_username: str = "MatrixRelayUser"
    discourse_relay_telegram_username: str = "TelegramRelayUser"
    discourse_relay_discord_username: str = "DiscordRelayUser"
    discourse_test_category_id: int | None = None


class _SyncClient:
    """sync_once double returning queued next_batch tokens."""

    def __init__(self, next_batches: list[str]) -> None:
        self.next_batches = list(next_batches)
        self.sync_calls: list[dict[str, Any]] = []

    async def sync_once(self, *, since: str | None = None, timeout_ms: int = 0):
        self.sync_calls.append({"since": since, "timeout_ms": timeout_ms})
        batch = self.next_batches.pop(0) if self.next_batches else "batch-tail"
        return SimpleNamespace(next_batch=batch)

    async def accept_invites(self, sync_response) -> None:
        return None

    def extract_messages(self, sync_response) -> list[MatrixMessage]:
        return []


async def test_sync_token_survives_restart(pg_pool) -> None:
    """After an iteration persists a token, a fresh context over the same DB must
    resume from it instead of doing a fresh initial sync (since=None)."""
    matrix_state = MatrixStateRepository(pg_pool)
    settings = _Settings()

    assert await matrix_state.get_sync_next_batch() is None

    class _NoopDiscourse:
        async def list_latest_posts(self, *, before):
            return []

    class _NoopJobs:
        async def claim_next_job(self, *, lease_seconds: int):
            return None

    first_context = SimpleNamespace(
        matrix_client=_SyncClient(next_batches=["batch-after-first"]),
        service=SimpleNamespace(),
        discourse_client=_NoopDiscourse(),
        chat_accounts=None,
        room_links=None,
        delivery_messages=None,
        audit_logs=FakeAuditLogs(),
        categories=None,
        discourse_events=None,
        user_watches=None,
        delivery_jobs=_NoopJobs(),
        matrix_state=matrix_state,
    )
    next_batch = await run_iteration(
        context=first_context,
        settings=settings,
        poll_state=PollerState(),
        sync_since=None,
    )
    assert next_batch == "batch-after-first"

    # Simulate a restart: brand-new client/context instances, same database.
    restarted_client = _SyncClient(next_batches=["batch-after-second"])
    restarted_context = SimpleNamespace(
        matrix_client=restarted_client,
        service=SimpleNamespace(),
        discourse_client=_NoopDiscourse(),
        chat_accounts=None,
        room_links=None,
        delivery_messages=None,
        audit_logs=FakeAuditLogs(),
        categories=None,
        discourse_events=None,
        user_watches=None,
        delivery_jobs=_NoopJobs(),
        matrix_state=matrix_state,
    )
    resumed_since = await matrix_state.get_sync_next_batch()
    await run_iteration(
        context=restarted_context,
        settings=settings,
        poll_state=PollerState(),
        sync_since=resumed_since,
    )

    assert resumed_since == "batch-after-first"
    assert restarted_client.sync_calls[0]["since"] == "batch-after-first"
    assert await matrix_state.get_sync_next_batch() == "batch-after-second"


async def test_discourse_poll_cursor_survives_restart_and_never_moves_backwards(pg_pool) -> None:
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)

    assert await matrix_state.get_discourse_last_seen_post_id() is None
    await matrix_state.set_discourse_last_seen_post_id(125)
    assert await matrix_state.get_discourse_last_seen_post_id() == 125

    # A delayed/stale worker cannot rewind the shared high-water mark.
    await matrix_state.set_discourse_last_seen_post_id(120)
    assert await matrix_state.get_discourse_last_seen_post_id() == 125


async def test_claim_release_and_processed_lifecycle(pg_pool) -> None:
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)

    claimed = await matrix_state.claim_event(matrix_room_id="!room:test", matrix_event_id="$evt")
    replay = await matrix_state.claim_event(matrix_room_id="!room:test", matrix_event_id="$evt")
    assert claimed is not None
    assert replay is None

    # Releasing an unprocessed claim lets a later delivery claim it again.
    await matrix_state.release_event(matrix_room_id="!room:test", matrix_event_id="$evt")
    reclaimed = await matrix_state.claim_event(matrix_room_id="!room:test", matrix_event_id="$evt")
    assert reclaimed is not None

    # Confirming makes the fence permanent: replays never win again...
    await matrix_state.mark_event_processed(matrix_room_id="!room:test", matrix_event_id="$evt")
    processed = await matrix_state.get_event(matrix_room_id="!room:test", matrix_event_id="$evt")
    assert processed is not None
    assert processed.status == "processed"
    after_confirm = await matrix_state.claim_event(
        matrix_room_id="!room:test", matrix_event_id="$evt"
    )
    assert after_confirm is None

    # ...and release cannot resurrect a confirmed marker.
    await matrix_state.release_event(matrix_room_id="!room:test", matrix_event_id="$evt")
    still_processed = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id="$evt"
    )
    assert still_processed is not None
    assert still_processed.status == "processed"


async def test_mark_event_processed_is_guarded_by_status_and_lease(pg_pool) -> None:
    """Confirming a marker is a guarded transition like every other primitive.

    A caller that no longer owns the fence (superseded lease token) must
    update nothing: 'processed' can only be reached from a pre-confirmation
    state under the owner's token, so a future caller can never confirm a
    marker another attempt owns. A tokenless reconcile of a 'written' marker
    (outcome recorded, lease cleared) still confirms it.
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)

    claimed = await matrix_state.claim_event(
        matrix_room_id="!room:test",
        matrix_event_id="$evt-guard-foreign",
        lease_owner="attempt-1",
    )
    assert claimed is not None

    # A foreign token must not confirm the marker.
    await matrix_state.mark_event_processed(
        matrix_room_id="!room:test", matrix_event_id="$evt-guard-foreign", lease_owner="attempt-2"
    )
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id="$evt-guard-foreign"
    )
    assert marker is not None and marker.status == "claimed"

    # The owner confirms it.
    await matrix_state.mark_event_processed(
        matrix_room_id="!room:test", matrix_event_id="$evt-guard-foreign", lease_owner="attempt-1"
    )
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id="$evt-guard-foreign"
    )
    assert marker is not None and marker.status == "processed"


async def test_owned_event_can_record_outcome_but_cannot_be_released_or_taken_over(pg_pool) -> None:
    """Entering an external write is an irreversible, owner-fenced transition."""
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)

    claimed = await matrix_state.claim_event(
        matrix_room_id="!room:test",
        matrix_event_id="$owned-write",
        lease_owner="writer-a",
    )
    assert claimed is not None
    assert await matrix_state.begin_event_write(
        matrix_room_id="!room:test",
        matrix_event_id="$owned-write",
        lease_owner="writer-a",
    )

    await matrix_state.release_event(
        matrix_room_id="!room:test",
        matrix_event_id="$owned-write",
        lease_owner="writer-a",
    )
    assert (
        await matrix_state.adopt_event(
            matrix_room_id="!room:test",
            matrix_event_id="$owned-write",
            lease_owner="writer-b",
        )
        is None
    )

    outcome = await matrix_state.mark_event_written(
        matrix_room_id="!room:test",
        matrix_event_id="$owned-write",
        discourse_topic_id=20,
        discourse_post_id=101,
        lease_owner="writer-a",
    )
    assert outcome.recorded is True
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id="$owned-write"
    )
    assert marker is not None and marker.status == "written"

    # A tokenless reconcile of a 'written' marker (outcome recorded, lease
    # columns cleared by mark_event_written) can still confirm it.
    await matrix_state.claim_event(
        matrix_room_id="!room:test",
        matrix_event_id="$evt-guard-written",
        lease_owner="attempt-1",
    )
    written = await matrix_state.mark_event_written(
        matrix_room_id="!room:test",
        matrix_event_id="$evt-guard-written",
        discourse_topic_id=20,
        discourse_post_id=101,
        lease_owner="attempt-1",
    )
    assert written.recorded is True
    await matrix_state.mark_event_processed(
        matrix_room_id="!room:test", matrix_event_id="$evt-guard-written", lease_owner=None
    )
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id="$evt-guard-written"
    )
    assert marker is not None and marker.status == "processed"


async def test_crash_before_discourse_write_is_recovered_by_fresh_handler(pg_pool) -> None:
    """Crash-point (a): claim succeeded, the external write never happened.

    A fresh handler against the same DB must still deliver the event —
    exactly once — instead of returning event_already_claimed forever.
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)
    message = make_reply_message("$reply-event-crash-a")
    call_kwargs: dict[str, Any] = dict(
        message=message,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    # Attempt 1 died right after claim_event(): the marker is left 'claimed'
    # with no recorded outcome — no release, no write, no mapping. (A crash
    # cannot leave anything else behind at this point.)
    await matrix_state.claim_event(matrix_room_id="!room:test", matrix_event_id=message.event_id)

    # Restart: a fresh handler replays the same event against the same DB.
    restarted = FakeDiscourseClient()
    recovered = await handle_matrix_reply(discourse_client=restarted, **call_kwargs)

    # The orphaned claim is adopted and the event is delivered exactly once.
    assert recovered.posted is True
    assert len(restarted.calls) == 1
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "processed"
    # A further replay must not write again either.
    replay = await handle_matrix_reply(discourse_client=restarted, **call_kwargs)
    assert replay.posted is False
    assert len(restarted.calls) == 1


async def test_crash_after_discourse_write_before_mapping_does_not_duplicate(pg_pool) -> None:
    """Crash-point (b): the write succeeded, the mapping never committed.

    On restart the replay must reconcile the mapping from the recorded
    outcome and never call create_reply again.
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)
    message = make_reply_message("$reply-event-crash-b")
    call_kwargs: dict[str, Any] = dict(
        message=message,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    # Attempt 1: Discourse accepted the reply (post 101), the outcome was
    # recorded on the marker, and THEN the process died before the delivery
    # mapping was committed. Reproduce that exact durable state.
    written_post_id = 101
    await matrix_state.claim_event(matrix_room_id="!room:test", matrix_event_id=message.event_id)
    outcome = await matrix_state.mark_event_written(
        matrix_room_id="!room:test",
        matrix_event_id=message.event_id,
        discourse_topic_id=20,
        discourse_post_id=written_post_id,
    )
    assert outcome.recorded is True
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "written"
    assert marker.discourse_post_id == written_post_id
    assert delivery_messages.mappings == {}

    # Restart: a fresh handler replays the same event against the same DB.
    restarted = FakeDiscourseClient()
    recovered = await handle_matrix_reply(discourse_client=restarted, **call_kwargs)

    # The reply was NOT written again; the mapping was rebuilt from the
    # recorded outcome instead.
    assert len(restarted.calls) == 0
    assert recovered.discourse_post_id == written_post_id
    rebuilt = await delivery_messages.get_by_matrix_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert rebuilt is not None and rebuilt.discourse_post_id == written_post_id
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "processed"


async def test_fenced_pair_command_sends_pm_exactly_once_across_crash(pg_pool) -> None:
    """Replay of a side-effecting /pair command → the pairing PM is created once.

    Crash right after the PM write (before the notice and the sync-token
    update): the replay must deliver the stored notice without re-running the
    command — no second PM.
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)
    discourse = FakeDiscoursePrivateMessages()
    matrix = CrashDuringNoticeMatrixClient()
    service = SimpleNamespace(
        handle_message=AsyncMock(
            return_value=ServiceResponse(
                body="code 123456 sent to alice_d",
                pairing_code_to_deliver="123456",
                pairing_target_username="alice_d",
            )
        )
    )
    call_kwargs: dict[str, Any] = dict(
        matrix_client=matrix,
        service=service,
        discourse_client=discourse,
        chat_accounts=None,
        room_links=None,
        delivery_messages=None,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
        live_e2e_category_id=None,
    )
    sync_response = SimpleNamespace(next_batch="batch-1")
    message = MatrixMessage(
        event_id="$cmd-1",
        room_id="!room:test",
        sender="@alice:aosus.org",
        body="/pair alice_d",
        parent_event_id=None,
    )

    # Attempt 1: the PM is created, then the process dies right when the
    # notice send would happen (simulated via the crashing client).
    matrix.extract_messages_return = [message]
    with pytest.raises(RuntimeError, match="simulated crash"):
        await process_sync_messages(sync_response=sync_response, **call_kwargs)
    assert len(discourse.pm_calls) == 1
    marker = await matrix_state.get_event(matrix_room_id="!room:test", matrix_event_id="$cmd-1")
    assert marker is not None and marker.status == "written"

    # Restart: the same event is replayed against the same DB.
    service.handle_message.reset_mock()
    matrix.extract_messages_return = [message]
    matrix.crash_on_notice = False
    await process_sync_messages(sync_response=sync_response, **call_kwargs)

    # The PM was NOT sent again; the stored notice was delivered instead.
    assert len(discourse.pm_calls) == 1
    assert matrix.notices == [("!room:test", "code 123456 sent to alice_d")]
    expected_tx_id = event_notice_tx_id("!room:test", "$cmd-1")
    assert matrix.notice_calls == [
        ("!room:test", "code 123456 sent to alice_d", expected_tx_id),
        ("!room:test", "code 123456 sent to alice_d", expected_tx_id),
    ]
    service.handle_message.assert_not_called()
    marker = await matrix_state.get_event(matrix_room_id="!room:test", matrix_event_id="$cmd-1")
    assert marker is not None and marker.status == "processed"


async def test_fenced_pair_command_survives_crash_before_pm(pg_pool) -> None:
    """Crash after claiming the command but before the PM: a replay runs the
    command fresh and delivers exactly one PM (crash-point (a) for commands).
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)
    discourse = FakeDiscoursePrivateMessages()
    matrix = FakeNoticeMatrixClient()
    service = SimpleNamespace(
        handle_message=AsyncMock(
            return_value=ServiceResponse(
                body="code 123456 sent to alice_d",
                pairing_code_to_deliver="123456",
                pairing_target_username="alice_d",
            )
        )
    )
    call_kwargs: dict[str, Any] = dict(
        matrix_client=matrix,
        service=service,
        discourse_client=discourse,
        chat_accounts=None,
        room_links=None,
        delivery_messages=None,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
        live_e2e_category_id=None,
    )
    sync_response = SimpleNamespace(next_batch="batch-1")
    matrix.extract_messages_return = [
        MatrixMessage(
            event_id="$cmd-crash-a",
            room_id="!room:test",
            sender="@alice:aosus.org",
            body="/pair alice_d",
            parent_event_id=None,
        )
    ]

    # Crash immediately after the claim, before the command ran at all.
    await matrix_state.claim_event(matrix_room_id="!room:test", matrix_event_id="$cmd-crash-a")
    await process_sync_messages(sync_response=sync_response, **call_kwargs)

    # The orphaned claim was adopted: exactly one PM, one notice.
    assert len(discourse.pm_calls) == 1
    assert len(matrix.notices) == 1
    assert service.handle_message.await_count == 1
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id="$cmd-crash-a"
    )
    assert marker is not None and marker.status == "processed"


async def test_non_pm_command_notice_failure_keeps_fence_and_replay_never_reruns(pg_pool) -> None:
    """A command without a pairing PM (/unpair) whose room notice fails must
    keep its fence: the command's side effects already happened, so releasing
    the marker would let a replay re-run /unpair. The replay must instead
    deliver the stored notice exactly once and never re-execute the command.
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)
    discourse = FakeDiscoursePrivateMessages()
    matrix = CrashDuringNoticeMatrixClient()
    service = SimpleNamespace(
        handle_message=AsyncMock(
            return_value=ServiceResponse(body="account unpaired", pairing_code_to_deliver=None)
        )
    )
    call_kwargs: dict[str, Any] = dict(
        matrix_client=matrix,
        service=service,
        discourse_client=discourse,
        chat_accounts=None,
        room_links=None,
        delivery_messages=None,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
        live_e2e_category_id=None,
    )
    sync_response = SimpleNamespace(next_batch="batch-1")
    message = MatrixMessage(
        event_id="$cmd-unpair-notice-crash",
        room_id="!room:test",
        sender="@alice:aosus.org",
        body="/unpair",
        parent_event_id=None,
    )

    # Attempt 1: the command runs, its outcome is recorded, then the notice
    # send raises (simulated crash / Matrix outage right after the send call
    # is entered).
    matrix.extract_messages_return = [message]
    with pytest.raises(RuntimeError, match="simulated crash"):
        await process_sync_messages(sync_response=sync_response, **call_kwargs)
    assert service.handle_message.await_count == 1
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    # The fence survived the notice failure: 'written' with the pending
    # notice, not deleted for a command re-run.
    assert marker is not None
    assert marker.status == "written"
    assert marker.response_notice == "account unpaired"

    # Attempt 2: the same event replays after recovery. The stored notice is
    # delivered and the command is NOT re-run.
    service.handle_message.reset_mock()
    matrix.extract_messages_return = [message]
    matrix.crash_on_notice = False
    await process_sync_messages(sync_response=sync_response, **call_kwargs)
    assert matrix.notices == [("!room:test", "account unpaired")]
    expected_tx_id = event_notice_tx_id("!room:test", message.event_id)
    assert matrix.notice_calls == [
        ("!room:test", "account unpaired", expected_tx_id),
        ("!room:test", "account unpaired", expected_tx_id),
    ]
    service.handle_message.assert_not_called()
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "processed"


# ---------------------------------------------------------------------------
# Round-3 review regressions (PR #17): exclusive lease ownership.
# ---------------------------------------------------------------------------


async def test_adopt_event_is_exclusive_and_only_takes_stale_claims(pg_pool) -> None:
    """Concurrent adoptions of the same marker: exactly one winner.

    - a second adopt after the stale marker receives a fresh lease loses;
    - a live claim with an unexpired lease is never taken over;
    - an owned marker cannot be re-adopted because it may already represent
      an ambiguous external write;
    - 'written' outcome markers are never adopted (the external write already
      happened and must not be repeated).
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)

    # A live worker claims the event; its lease is fresh.
    first = await matrix_state.claim_event(
        matrix_room_id="!room:test",
        matrix_event_id="$lease-race",
        lease_owner="worker-a",
    )
    assert first is not None and first.status == "claimed"

    # A replaying handler must not take the fence from a live, unexpired lease.
    lost = await matrix_state.adopt_event(
        matrix_room_id="!room:test",
        matrix_event_id="$lease-race",
        lease_owner="replay-b",
    )
    assert lost is None

    # The claim is only adoptable once its lease has demonstrably lapsed.
    async with pg_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE matrix_event_state
            SET lease_expires_at = NOW() - INTERVAL '1 second'
            WHERE room_id = '!room:test' AND event_id = '$lease-race'
            """
        )
    won = await matrix_state.adopt_event(
        matrix_room_id="!room:test",
        matrix_event_id="$lease-race",
        lease_owner="replay-b",
    )
    assert won is not None
    assert won.status == "claimed"

    # The lease replay-b just took is fresh, so a second racing replay loses.
    lost_two = await matrix_state.adopt_event(
        matrix_room_id="!room:test",
        matrix_event_id="$lease-race",
        lease_owner="replay-c",
    )
    assert lost_two is None

    # A 'written' marker (outcome recorded) must never be adopted either.
    outcome = await matrix_state.mark_event_written(
        matrix_room_id="!room:test",
        matrix_event_id="$lease-race",
        discourse_topic_id=20,
        discourse_post_id=101,
        lease_owner="replay-b",
    )
    assert outcome.recorded is True
    lost_after_write = await matrix_state.adopt_event(
        matrix_room_id="!room:test",
        matrix_event_id="$lease-race",
        lease_owner="replay-d",
    )
    assert lost_after_write is None


async def test_two_concurrent_handlers_produce_exactly_one_discourse_write(pg_pool) -> None:
    """The reviewer's concurrency regression: two handlers see the same
    'claimed' event and only ONE external write may occur.

    Handler B replays while handler A's lease is fresh (the live-worker race
    the previous adopt_claimed_event() lost). B must lose the fence, write
    nothing, and — crucially — release nothing and stamp nothing.
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)
    message = make_reply_message("$concurrent-reply")

    call_kwargs: dict[str, Any] = dict(
        message=message,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    live_calls_gate = asyncio.Event()
    release_live_write = asyncio.Event()

    class GatedDiscourse(FakeDiscourseClient):
        """A's create_reply holds mid-flight until the test lets it finish."""

        async def create_reply(self, **kwargs):
            live_calls_gate.set()
            await release_live_write.wait()
            return await super().create_reply(**kwargs)

    live = GatedDiscourse()
    replayed = FakeDiscourseClient()

    async def attempt_a():
        return await handle_matrix_reply(discourse_client=live, **call_kwargs)

    async def attempt_b():
        # Start B only once A is verifiably inside its external write while
        # holding a fresh lease — the exact race the previous
        # adopt_claimed_event() lost.
        await live_calls_gate.wait()
        return await handle_matrix_reply(discourse_client=replayed, **call_kwargs)

    task_a = asyncio.create_task(attempt_a())
    task_b = asyncio.create_task(attempt_b())

    # B sees the same event while A holds a fresh, unexpired lease mid-write.
    second = await task_b
    assert second.posted is False
    # A has already entered the non-replayable external-write region. From
    # another handler's perspective the remote outcome is therefore
    # ambiguous until A records it, so the durable fence fails closed.
    assert second.error_message == "event_write_ambiguous"
    assert len(replayed.calls) == 0

    # A finishes its single write.
    release_live_write.set()
    first = await task_a
    assert first.posted is True
    assert len(live.calls) == 1

    # A third delivery after the dust settles still writes nothing new.
    third = await handle_matrix_reply(discourse_client=replayed, **call_kwargs)
    assert third.posted is False
    assert len(replayed.calls) == 0
    assert len(live.calls) == 1

    # The superseded attempt must not have been able to tear down A's fence
    # either: the marker survives as 'processed' with A's outcome.
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "processed"
    assert marker.discourse_post_id == first.discourse_post_id


async def test_crash_after_discourse_write_before_mark_event_written_fails_closed(
    pg_pool,
) -> None:
    """The reviewer's second crash test: the external API has returned but
    mark_event_written() never ran (process died in between).

    This is ambiguous because Discourse has no idempotency key. The durable
    ``owned`` marker therefore fails closed: a replay must never repeat the
    write and an operator must reconcile whether the remote post exists.
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)
    message = make_reply_message("$crash-after-write")
    call_kwargs: dict[str, Any] = dict(
        message=message,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    # Crash-point (c): create_reply returned (Discourse post 101 exists) but
    # the process died before mark_event_written(). Reproduce the exact
    # durable state: a claimed/owned marker with NO recorded outcome.
    await matrix_state.claim_event(
        matrix_room_id="!room:test",
        matrix_event_id=message.event_id,
        lease_owner="dead-attempt",
    )
    async with pg_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            UPDATE matrix_event_state
            SET status = 'owned',
                lease_expires_at = NOW() - INTERVAL '1 second'
            WHERE room_id = '!room:test' AND event_id = $1
            RETURNING id
            """,
            message.event_id,
        )
    assert row is not None

    # Restart: a fresh handler replays the same event against the same DB.
    # The owned marker is terminal for automatic takeover even after its
    # lease expires, because the remote write may already exist.
    recovered = FakeDiscourseClient()
    result = await handle_matrix_reply(discourse_client=recovered, **call_kwargs)
    assert result.posted is False
    assert result.error_message == "event_write_ambiguous"
    assert len(recovered.calls) == 0

    # Further replays remain fenced until explicit operator reconciliation.
    again = await handle_matrix_reply(discourse_client=recovered, **call_kwargs)
    assert again.posted is False
    assert again.error_message == "event_write_ambiguous"
    assert len(recovered.calls) == 0
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "owned"


async def test_superseded_attempt_cannot_stamp_or_release_fence(pg_pool) -> None:
    """An attempt that loses the fence mid-flight must be inert.

    While the loser's HTTP call is in flight a takeover reassigns the lease.
    When the loser's call returns, its mark_event_written() and its
    release_event() must both be no-ops — the winner's outcome and fence
    state stay intact and only one Discourse post is ever mapped.
    """
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    delivery_messages = FakeDeliveryMessages()
    matrix_state = MatrixStateRepository(pg_pool)
    message = make_reply_message("$superseded-reply")
    call_kwargs: dict[str, Any] = dict(
        message=message,
        matrix_client=FakeNoticeMatrixClient(),
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(None),
        delivery_messages=delivery_messages,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    # Attempt 1 claims and enters create_reply; mid-flight, attempt 2 wins a
    # lease takeover (simulated by directly reassigning the row) and records
    # ITS outcome as the winner.
    await matrix_state.claim_event(
        matrix_room_id="!room:test",
        matrix_event_id=message.event_id,
        lease_owner="attempt-1",
    )
    async with pg_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE matrix_event_state
            SET status = 'owned', lease_owner = 'attempt-2',
                lease_expires_at = NOW() + INTERVAL '10 minutes'
            WHERE room_id = '!room:test' AND event_id = $1
            """,
            message.event_id,
        )
    winner_outcome = await matrix_state.mark_event_written(
        matrix_room_id="!room:test",
        matrix_event_id=message.event_id,
        discourse_topic_id=20,
        discourse_post_id=101,
        lease_owner="attempt-2",
    )
    assert winner_outcome.recorded is True

    # Attempt 1's write now returns and it tries to stamp its own (conflicting)
    # outcome under its stale lease token.
    loser_outcome = await matrix_state.mark_event_written(
        matrix_room_id="!room:test",
        matrix_event_id=message.event_id,
        discourse_topic_id=20,
        discourse_post_id=999,
        lease_owner="attempt-1",
    )
    assert loser_outcome.recorded is False
    assert loser_outcome.conflicting_post_id == 101

    # Attempt 1 then fails and tries to release the fence — also a no-op.
    await matrix_state.release_event(
        matrix_room_id="!room:test",
        matrix_event_id=message.event_id,
        lease_owner="attempt-1",
    )
    marker = await matrix_state.get_event(
        matrix_room_id="!room:test", matrix_event_id=message.event_id
    )
    assert marker is not None and marker.status == "written"
    assert marker.discourse_post_id == 101

    # The replay reconciles from the winner's outcome: one write, ever.
    recovered = await handle_matrix_reply(discourse_client=FakeDiscourseClient(), **call_kwargs)
    assert recovered.posted is False
    assert recovered.discourse_post_id == 101


async def test_fenced_pair_command_exactly_one_pm_when_two_handlers_race(pg_pool) -> None:
    """Command-path concurrency regression: a replay racing a LIVE fenced
    /pair — while the first handler holds a fresh lease inside its pairing-PM
    write — must lose the fence and never send a second pairing PM."""
    await apply_sql_migrations(pg_pool, MIGRATIONS_DIR)
    matrix_state = MatrixStateRepository(pg_pool)
    matrix = FakeNoticeMatrixClient()
    service = SimpleNamespace(
        handle_message=AsyncMock(
            return_value=ServiceResponse(
                body="code 123456 sent to alice_d",
                pairing_code_to_deliver="123456",
                pairing_target_username="alice_d",
            )
        )
    )
    sync_response = SimpleNamespace(next_batch="batch-1")
    message = MatrixMessage(
        event_id="$cmd-race",
        room_id="!room:test",
        sender="@alice:aosus.org",
        body="/pair alice_d",
        parent_event_id=None,
    )

    pm_started = asyncio.Event()
    release_pm = asyncio.Event()

    class GatedDiscoursePrivateMessages(FakeDiscoursePrivateMessages):
        """A's create_private_message holds mid-flight until the test lets
        it finish — the reply path's GatedDiscourse, command edition."""

        async def create_private_message(self, **kwargs):
            pm_started.set()
            await release_pm.wait()
            return await super().create_private_message(**kwargs)

    gated_discourse = GatedDiscoursePrivateMessages()
    replay_discourse = FakeDiscoursePrivateMessages()

    async def attempt_a():
        return await process_sync_messages(
            matrix_client=cast("NioMatrixClient", matrix),
            service=cast("DischatService", service),
            discourse_client=gated_discourse,
            chat_accounts=None,
            room_links=None,
            delivery_messages=None,
            audit_logs=FakeAuditLogs(),
            event_state=matrix_state,
            relay_matrix_username="MatrixRelayUser",
            relay_telegram_username="TelegramRelayUser",
            relay_discord_username="DiscordRelayUser",
            live_e2e_category_id=None,
            sync_response=sync_response,
        )

    async def attempt_b():
        # Start B only once A is verifiably inside its external PM write
        # while holding a fresh lease — the exact race the name describes.
        await pm_started.wait()
        return await process_sync_messages(
            matrix_client=cast("NioMatrixClient", matrix),
            service=cast("DischatService", service),
            discourse_client=replay_discourse,
            chat_accounts=None,
            room_links=None,
            delivery_messages=None,
            audit_logs=FakeAuditLogs(),
            event_state=matrix_state,
            relay_matrix_username="MatrixRelayUser",
            relay_telegram_username="TelegramRelayUser",
            relay_discord_username="DiscordRelayUser",
            live_e2e_category_id=None,
            sync_response=sync_response,
        )

    matrix.extract_messages_return = [message]
    task_a = asyncio.create_task(attempt_a())
    task_b = asyncio.create_task(attempt_b())

    # B sees the same event while A holds a fresh, unexpired lease mid-PM.
    await task_b
    assert len(replay_discourse.pm_calls) == 0
    # B lost the fence before ever running the command: the command (and its
    # side effects) belong to A alone.
    assert service.handle_message.await_count == 1

    # A finishes its single PM write, records the outcome, sends the notice.
    release_pm.set()
    await task_a
    assert len(gated_discourse.pm_calls) == 1
    assert matrix.notices == [("!room:test", "code 123456 sent to alice_d")]
    marker = await matrix_state.get_event(matrix_room_id="!room:test", matrix_event_id="$cmd-race")
    assert marker is not None and marker.status == "processed"

    # A third delivery after the dust settles re-runs nothing.
    matrix.extract_messages_return = [message]
    await process_sync_messages(
        matrix_client=cast("NioMatrixClient", matrix),
        service=cast("DischatService", service),
        discourse_client=replay_discourse,
        chat_accounts=None,
        room_links=None,
        delivery_messages=None,
        audit_logs=FakeAuditLogs(),
        event_state=matrix_state,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
        live_e2e_category_id=None,
        sync_response=sync_response,
    )
    assert len(gated_discourse.pm_calls) == 1
    assert len(replay_discourse.pm_calls) == 0
    assert service.handle_message.await_count == 1
