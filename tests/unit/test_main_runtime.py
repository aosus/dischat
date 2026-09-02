from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from dischat.config import FileConfig
from dischat.discourse.sync import PollerState, poll_once
from dischat.main import drain_delivery_jobs, refresh_category_visibility, run_iteration
from dischat.storage.repositories import (
    DEFAULT_JOB_LEASE_SECONDS,
    DeliveryJobRecord,
    RoomLinkRecord,
    TargetType,
)

DEFAULT_TEST_LEASE_SECONDS = DEFAULT_JOB_LEASE_SECONDS


class FakeMatrixClient:
    def __init__(self) -> None:
        self.sync_calls: list[dict[str, Any]] = []
        self.accepted: list[object] = []

    async def sync_once(self, *, since: str | None = None, timeout_ms: int = 0):
        self.sync_calls.append({"since": since, "timeout_ms": timeout_ms})
        return SimpleNamespace(next_batch="batch-2")

    async def accept_invites(self, sync_response) -> None:
        self.accepted.append(sync_response)


class FakeDiscourseClient:
    def __init__(self) -> None:
        self.list_categories_calls = 0
        self.categories: list[dict[str, object]] = []
        self.fail_categories = False
        self.latest_posts: list[dict[str, object]] = []
        self.topics: dict[int, dict[str, object]] = {}
        self.category_topics: list[dict[str, object]] = []

    async def list_categories(self) -> list[dict[str, object]]:
        self.list_categories_calls += 1
        if self.fail_categories:
            raise RuntimeError("discourse category listing down")
        return self.categories

    async def list_latest_posts(self, *, before: int | None = None) -> list[dict[str, object]]:
        return self.latest_posts

    async def list_category_latest_posts(
        self, *, category_slug: str, category_id: int
    ) -> list[dict[str, object]]:
        return self.category_topics

    async def get_topic(self, topic_id: int) -> dict[str, object]:
        return self.topics[topic_id]


class FakeSyncCategoriesRepo:
    """Records what sync_categories_from_discourse would have written."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.disable_calls: list[list[int]] = []
        self.by_discourse_id: dict[int, Any] = {}

    async def disable_categories_not_in(self, discourse_category_ids: list[int]) -> None:
        self.disable_calls.append(discourse_category_ids)

    async def upsert_category(
        self,
        *,
        discourse_category_id: int,
        slug: str,
        name: str,
        is_public: bool,
        enabled: bool = True,
    ):
        self.calls.append(
            {
                "discourse_category_id": discourse_category_id,
                "slug": slug,
                "name": name,
                "is_public": is_public,
                "enabled": enabled,
            }
        )

        class _Record:
            def __init__(self, slug: str) -> None:
                self.id = 1
                self.slug = slug
                self.is_public = is_public
                self.enabled = enabled

        record = _Record(slug)
        self.by_discourse_id[discourse_category_id] = record
        return record

    async def get_by_discourse_category_id(self, discourse_category_id: int):
        return self.by_discourse_id.get(discourse_category_id)


class FakeDeliveryJobs:
    def __init__(self, jobs: list[DeliveryJobRecord] | None = None) -> None:
        self.jobs = jobs or []
        self.completed: list[int] = []
        self.failed: list[dict[str, object]] = []
        self.enqueued: list[dict[str, object]] = []
        self.claim_lease_seconds: list[int] = []

    async def claim_next_job(self, *, lease_seconds: int = DEFAULT_TEST_LEASE_SECONDS):
        self.claim_lease_seconds.append(lease_seconds)
        if not self.jobs:
            return None
        return self.jobs.pop(0)

    async def mark_complete(self, job_id: int, *, claim_token: str = "") -> bool:
        self.completed.append(job_id)
        return True

    async def mark_failed(
        self, job_id: int, *, claim_token: str = "", error: str, next_attempt_at: datetime
    ) -> bool:
        self.failed.append({"job_id": job_id, "error": error, "next_attempt_at": next_attempt_at})
        return True

    async def enqueue(
        self,
        *,
        event_id: int,
        target_type: TargetType,
        target_mxid: str | None,
        matrix_room_id: str | None,
    ) -> None:
        self.enqueued.append(
            {
                "event_id": event_id,
                "target_type": target_type,
                "target_mxid": target_mxid,
                "matrix_room_id": matrix_room_id,
            }
        )
        # Make the job claimable so drain_delivery_jobs (with a stubbed deliver_job) can
        # complete it, mirroring the real repository's pending queue.
        self.jobs.append(
            DeliveryJobRecord(
                id=event_id,
                event_id=event_id,
                target_type=target_type,
                target_mxid=target_mxid,
                matrix_room_id=matrix_room_id,
                status="pending",
                attempts=0,
                next_attempt_at=datetime.now(UTC),
                last_error=None,
            )
        )


class FakeDeliveryMessages:
    async def list_by_discourse_post(self, *, discourse_post_id: int):
        return []


class FakeChatAccounts:
    async def list_by_discourse_username(self, discourse_username: str):
        return []


class FakeUserWatches:
    def __init__(self) -> None:
        self.mxids_by_category: dict[int, list[str]] = {}

    async def list_mxids_for_category(self, *, category_id: int, include_non_public: bool = False):
        return self.mxids_by_category.get(category_id, [])


class FakeRoomLinks:
    def __init__(self, room_link: RoomLinkRecord | None = None) -> None:
        self.room_link = room_link
        self.replaced: list[dict[str, dict[str, Any]]] = []
        self.by_category: dict[str, list[RoomLinkRecord]] = {}

    async def replace_room_links(
        self, room_links: dict[str, dict[str, Any]], category_lookup: dict[str, int]
    ) -> None:
        self.replaced.append(room_links)

    async def list_links_matching_category(
        self, category_slug: str, *, include_non_public: bool = False
    ):
        return self.by_category.get(category_slug, [])


class FakeDiscourseEvents:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_event_if_missing(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=len(self.created))


async def test_drain_delivery_jobs_marks_completed_and_failed(monkeypatch) -> None:
    job_complete = DeliveryJobRecord(
        id=1,
        event_id=1,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:test",
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(UTC),
        last_error=None,
    )
    job_failed = DeliveryJobRecord(
        id=2,
        event_id=2,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:test",
        status="pending",
        attempts=1,
        next_attempt_at=datetime.now(UTC),
        last_error=None,
    )
    context = SimpleNamespace(
        delivery_jobs=FakeDeliveryJobs([job_complete, job_failed]),
        discourse_events=object(),
        delivery_messages=object(),
        chat_accounts=object(),
        room_links=object(),
        matrix_client=object(),
    )

    async def fake_deliver_job(**kwargs):
        if kwargs["job"].id == 1:
            return SimpleNamespace(complete=True, error=None)
        return SimpleNamespace(complete=False, error="boom")

    monkeypatch.setattr("dischat.main.deliver_job", fake_deliver_job)

    delivered = await drain_delivery_jobs(context)

    assert delivered == 1
    assert context.delivery_jobs.completed == [1]
    assert context.delivery_jobs.failed[0]["job_id"] == 2
    assert context.delivery_jobs.failed[0]["error"] == "boom"


async def test_run_iteration_syncs_processes_and_returns_next_batch(monkeypatch) -> None:
    process_calls: list[dict[str, Any]] = []
    poll_calls: list[dict[str, Any]] = []
    drain_calls: list[object] = []

    async def fake_process_sync_messages(**kwargs) -> None:
        process_calls.append(kwargs)

    async def fake_poll_once(**kwargs) -> int:
        poll_calls.append(kwargs)
        return 2

    async def fake_drain_delivery_jobs(context) -> int:
        drain_calls.append(context)
        return 3

    monkeypatch.setattr("dischat.main.process_sync_messages", fake_process_sync_messages)
    monkeypatch.setattr("dischat.main.poll_once", fake_poll_once)
    monkeypatch.setattr("dischat.main.drain_delivery_jobs", fake_drain_delivery_jobs)

    context = SimpleNamespace(
        matrix_client=FakeMatrixClient(),
        service=object(),
        discourse_client=object(),
        chat_accounts=object(),
        room_links=object(),
        delivery_messages=object(),
        audit_logs=object(),
        categories=object(),
        discourse_events=object(),
        user_watches=object(),
        delivery_jobs=object(),
        matrix_state=None,
    )
    settings = SimpleNamespace(
        poll_interval_seconds=15,
        discourse_relay_matrix_username="MatrixRelayUser",
        discourse_relay_telegram_username="TelegramRelayUser",
        discourse_relay_discord_username="DiscordRelayUser",
        discourse_test_category_id=56,
    )

    next_batch = await run_iteration(
        context=context,
        settings=settings,
        poll_state=PollerState(),
        sync_since=None,
    )

    assert next_batch == "batch-2"
    assert context.matrix_client.sync_calls == [{"since": None, "timeout_ms": 0}]
    assert len(context.matrix_client.accepted) == 1
    assert process_calls[0]["sync_response"].next_batch == "batch-2"
    assert poll_calls[0]["state"].last_seen_post_id is None
    assert drain_calls == [context]


async def test_run_iteration_uses_long_poll_after_initial_sync(monkeypatch) -> None:
    async def fake_process_sync_messages(**kwargs) -> None:
        return None

    async def fake_poll_once(**kwargs) -> int:
        return 0

    async def fake_drain_delivery_jobs(context) -> int:
        return 0

    monkeypatch.setattr("dischat.main.process_sync_messages", fake_process_sync_messages)
    monkeypatch.setattr("dischat.main.poll_once", fake_poll_once)
    monkeypatch.setattr("dischat.main.drain_delivery_jobs", fake_drain_delivery_jobs)

    context = SimpleNamespace(
        matrix_client=FakeMatrixClient(),
        service=object(),
        discourse_client=object(),
        chat_accounts=object(),
        room_links=object(),
        delivery_messages=object(),
        audit_logs=object(),
        categories=object(),
        discourse_events=object(),
        user_watches=object(),
        delivery_jobs=object(),
        matrix_state=None,
    )
    settings = SimpleNamespace(
        poll_interval_seconds=15,
        discourse_relay_matrix_username="MatrixRelayUser",
        discourse_relay_telegram_username="TelegramRelayUser",
        discourse_relay_discord_username="DiscordRelayUser",
        discourse_test_category_id=56,
    )

    await run_iteration(
        context=context,
        settings=settings,
        poll_state=PollerState(),
        sync_since="batch-1",
    )

    assert context.matrix_client.sync_calls == [{"since": "batch-1", "timeout_ms": 15000}]


def _refresh_settings() -> SimpleNamespace:
    return SimpleNamespace(discourse_test_category_id=None)


def _refresh_context(discourse_client) -> SimpleNamespace:
    return SimpleNamespace(
        discourse_client=discourse_client,
        categories=FakeSyncCategoriesRepo(),
        room_links=FakeRoomLinks(),
        file_config=FileConfig(),
    )


class FakeRoomLinksRepo(FakeRoomLinks):
    """Alias kept for readability in refresh-specific tests."""


async def test_refresh_categories_runs_when_stale_and_updates_visibility() -> None:
    discourse = FakeDiscourseClient()
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": False},
        {"id": 99, "slug": "private", "name": "Private", "read_restricted": True},
    ]
    context = _refresh_context(discourse)
    poll_state = PollerState()

    refreshed = await refresh_category_visibility(
        context=context,
        settings=_refresh_settings(),
        poll_state=poll_state,
    )

    assert refreshed is True
    assert discourse.list_categories_calls == 1
    upserts = {str(call["discourse_category_id"]): call for call in context.categories.calls}
    assert upserts["10"]["is_public"] is True
    assert upserts["10"]["enabled"] is True
    assert upserts["99"]["is_public"] is False
    assert upserts["99"]["enabled"] is False
    # File-configured room links are re-materialized against the fresh category lookup.
    assert len(context.room_links.replaced) == 1


async def test_refresh_revalidates_visibility_on_every_call() -> None:
    """There is no refresh cadence: every call revalidates against the live category
    listing, so a visibility change is picked up by the very next poll. A snapshot that
    skips a refresh would leave a window in which a `public -> private` transition keeps
    routing posts."""

    discourse = FakeDiscourseClient()
    context = _refresh_context(discourse)
    poll_state = PollerState()

    for _ in range(3):
        refreshed = await refresh_category_visibility(
            context=context,
            settings=_refresh_settings(),
            poll_state=poll_state,
        )
        assert refreshed is True

    assert discourse.list_categories_calls == 3


async def test_run_iteration_revalidates_visibility_before_every_poll(monkeypatch) -> None:
    async def fake_process_sync_messages(**kwargs) -> None:
        return None

    async def fake_poll_once(**kwargs) -> int:
        return 0

    async def fake_drain_delivery_jobs(context) -> int:
        return 0

    monkeypatch.setattr("dischat.main.process_sync_messages", fake_process_sync_messages)
    monkeypatch.setattr("dischat.main.poll_once", fake_poll_once)
    monkeypatch.setattr("dischat.main.drain_delivery_jobs", fake_drain_delivery_jobs)

    discourse = FakeDiscourseClient()
    context = SimpleNamespace(
        matrix_client=FakeMatrixClient(),
        service=object(),
        discourse_client=discourse,
        chat_accounts=object(),
        room_links=FakeRoomLinks(),
        file_config=FileConfig(),
        delivery_messages=object(),
        audit_logs=object(),
        categories=FakeSyncCategoriesRepo(),
        discourse_events=object(),
        user_watches=object(),
        delivery_jobs=object(),
    )
    settings = SimpleNamespace(
        poll_interval_seconds=15,
        discourse_relay_matrix_username="MatrixRelayUser",
        discourse_relay_telegram_username="TelegramRelayUser",
        discourse_relay_discord_username="DiscordRelayUser",
        discourse_test_category_id=None,
    )
    poll_state = PollerState()

    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since=None,
    )
    first_count = discourse.list_categories_calls
    assert first_count == 1

    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since="batch-1",
    )

    # No cadence: every production poll is preceded by a fresh visibility revalidation,
    # so a `public -> private` transition can never be missed between refreshes.
    assert discourse.list_categories_calls == first_count + 1


async def test_refresh_failure_keeps_last_known_snapshot_and_fails_closed(monkeypatch) -> None:
    class FailingDiscourseClient:
        def __init__(self) -> None:
            self.list_categories_calls = 0
            self.fail_categories = False

        async def list_categories(self) -> list[dict[str, object]]:
            self.list_categories_calls += 1
            if self.fail_categories:
                raise RuntimeError("discourse down")
            return [{"id": 10, "slug": "support", "name": "Support", "read_restricted": False}]

    discourse = FailingDiscourseClient()
    context = _refresh_context(discourse)
    poll_state = PollerState()

    # Fail closed: the failed refresh must mark the snapshot stale ...
    # (the fake raises while fail_categories is set)
    discourse.fail_categories = True

    refreshed = await refresh_category_visibility(
        context=context,
        settings=_refresh_settings(),
        poll_state=poll_state,
    )

    assert refreshed is False
    assert discourse.list_categories_calls == 1
    # Fail closed: the failed refresh must mark the snapshot stale ...
    assert poll_state.visibility_stale is True

    # ... and a successful later refresh revalidates and clears the flag.
    discourse.fail_categories = False
    refreshed = await refresh_category_visibility(
        context=context,
        settings=_refresh_settings(),
        poll_state=poll_state,
    )

    assert refreshed is True
    assert poll_state.visibility_stale is False


async def test_refresh_failure_marks_snapshot_stale_even_after_previous_success() -> None:
    discourse = FakeDiscourseClient()
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": False}
    ]
    context = _refresh_context(discourse)
    poll_state = PollerState()

    # First refresh succeeds; snapshot is fresh and not stale.
    assert (
        await refresh_category_visibility(
            context=context,
            settings=_refresh_settings(),
            poll_state=poll_state,
        )
        is True
    )
    assert poll_state.visibility_stale is False

    # Then the refresh starts failing: even though a recent snapshot exists, it is stale
    # and must be flagged.
    discourse.fail_categories = True

    refreshed = await refresh_category_visibility(
        context=context,
        settings=_refresh_settings(),
        poll_state=poll_state,
    )

    assert refreshed is False
    assert poll_state.visibility_stale is True


async def test_stale_snapshot_retries_refresh_every_iteration_until_success() -> None:
    class FlakyDiscourseClient:
        def __init__(self) -> None:
            self.list_categories_calls = 0
            self.fail_until_call = 2

        async def list_categories(self) -> list[dict[str, object]]:
            self.list_categories_calls += 1
            if self.list_categories_calls <= self.fail_until_call:
                raise RuntimeError("discourse down")
            return [{"id": 10, "slug": "support", "name": "Support", "read_restricted": False}]

    discourse = FlakyDiscourseClient()
    context = _refresh_context(discourse)
    poll_state = PollerState()

    # Attempt 1: fails, snapshot goes stale.
    assert (
        await refresh_category_visibility(
            context=context,
            settings=_refresh_settings(),
            poll_state=poll_state,
        )
        is False
    )
    assert poll_state.visibility_stale is True

    # Attempt 2 (next iteration): retries immediately, still stale.
    assert (
        await refresh_category_visibility(
            context=context,
            settings=_refresh_settings(),
            poll_state=poll_state,
        )
        is False
    )
    assert poll_state.visibility_stale is True
    assert discourse.list_categories_calls == 2

    # Attempt 3: succeeds, flag clears and polling may resume.
    assert (
        await refresh_category_visibility(
            context=context,
            settings=_refresh_settings(),
            poll_state=poll_state,
        )
        is True
    )
    assert poll_state.visibility_stale is False
    assert discourse.list_categories_calls == 3


async def test_run_iteration_polls_and_enqueues_after_successful_refresh(monkeypatch) -> None:
    """Baseline for the fail-closed regression: with a working refresh, a new public
    category topic flows through poll -> event -> delivery job -> delivery."""

    async def fake_process_sync_messages(**kwargs) -> None:
        return None

    async def fake_deliver_job(**kwargs) -> Any:
        return SimpleNamespace(complete=True, error=None)

    monkeypatch.setattr("dischat.main.process_sync_messages", fake_process_sync_messages)
    monkeypatch.setattr("dischat.main.deliver_job", fake_deliver_job)

    discourse = FakeDiscourseClient()
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": False}
    ]
    discourse.latest_posts = [_support_post(post_id=31)]
    discourse.topics[20] = {"category_id": 10, "title": "Support topic"}

    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=True,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("support",),
    )
    room_links = FakeRoomLinks(room_link=room_link)
    room_links.by_category["support"] = [room_link]
    delivery_jobs = FakeDeliveryJobs()
    categories = FakeSyncCategoriesRepo()

    context = SimpleNamespace(
        matrix_client=FakeMatrixClient(),
        service=object(),
        discourse_client=discourse,
        chat_accounts=FakeChatAccounts(),
        room_links=room_links,
        file_config=FileConfig(),
        delivery_messages=FakeDeliveryMessages(),
        audit_logs=object(),
        categories=categories,
        discourse_events=FakeDiscourseEvents(),
        user_watches=FakeUserWatches(),
        delivery_jobs=delivery_jobs,
    )
    settings = SimpleNamespace(
        poll_interval_seconds=15,
        discourse_relay_matrix_username="",
        discourse_relay_telegram_username="",
        discourse_relay_discord_username="",
        discourse_test_category_id=None,
    )
    poll_state = PollerState()

    next_batch = await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since=None,
    )

    assert next_batch == "batch-2"
    assert len(context.discourse_events.created) == 1
    assert delivery_jobs.enqueued, "public post should be enqueued for delivery"
    assert poll_state.visibility_stale is False
    assert poll_state.last_seen_post_id == 31


def _support_post(*, post_id: int) -> dict[str, object]:
    return {
        "id": post_id,
        "topic_id": 20,
        "category_id": 10,
        "username": "alice",
        "raw": "public body",
    }


def _private_post(*, post_id: int) -> dict[str, object]:
    return {
        "id": post_id,
        "topic_id": 20,
        "category_id": 10,
        "username": "mallory",
        "raw": "secret body",
    }


async def test_public_to_private_with_refresh_failure_delivers_nothing(monkeypatch) -> None:
    """Required regression: public -> private transition while the category listing
    refresh fails but the admin-authenticated post feed still works. While visibility is
    stale, no delivery may occur — proving the fail-closed behavior end to end through
    the real poller, router, and delivery draining (only process_sync_messages faked)."""

    async def fake_process_sync_messages(**kwargs) -> None:
        return None

    async def fake_deliver_job(**kwargs) -> Any:
        return SimpleNamespace(complete=True, error=None)

    monkeypatch.setattr("dischat.main.process_sync_messages", fake_process_sync_messages)
    monkeypatch.setattr("dischat.main.deliver_job", fake_deliver_job)

    discourse = FakeDiscourseClient()
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": False}
    ]
    discourse.topics[20] = {"category_id": 10, "title": "Support topic"}

    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=True,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("support",),
    )
    room_links = FakeRoomLinks(room_link=room_link)
    room_links.by_category["support"] = [room_link]
    delivery_jobs = FakeDeliveryJobs()
    categories = FakeSyncCategoriesRepo()
    discourse_events = FakeDiscourseEvents()

    context = SimpleNamespace(
        matrix_client=FakeMatrixClient(),
        service=object(),
        discourse_client=discourse,
        chat_accounts=FakeChatAccounts(),
        room_links=room_links,
        file_config=FileConfig(),
        delivery_messages=FakeDeliveryMessages(),
        audit_logs=object(),
        categories=categories,
        discourse_events=discourse_events,
        user_watches=FakeUserWatches(),
        delivery_jobs=delivery_jobs,
    )
    settings = SimpleNamespace(
        poll_interval_seconds=15,
        discourse_relay_matrix_username="MatrixRelayUser",
        discourse_relay_telegram_username="TelegramRelayUser",
        discourse_relay_discord_username="DiscordRelayUser",
        discourse_test_category_id=None,
    )
    poll_state = PollerState()

    # --- Iteration 1: category public, refresh works, post is delivered. --------------
    discourse.latest_posts = [_support_post(post_id=31)]
    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since=None,
    )
    assert poll_state.visibility_stale is False
    assert len(context.discourse_events.created) == 1
    assert len(delivery_jobs.enqueued) == 1
    assert delivery_jobs.completed and not delivery_jobs.failed

    # --- Admin makes category 10 read-restricted; the admin post feed still shows a new
    # private post, but the category listing refresh now fails (e.g. upstream outage). --
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": True}
    ]
    discourse.fail_categories = True
    discourse.latest_posts = [_support_post(post_id=31), _private_post(post_id=32)]
    jobs_before = len(delivery_jobs.enqueued)
    events_before = len(context.discourse_events.created)

    # --- Iteration 2 (cadence due + refresh failing): must fail closed. ---------------
    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since="batch-2",
    )
    assert poll_state.visibility_stale is True
    # No event, no enqueue, no delivery for the private post while visibility is stale.
    assert len(context.discourse_events.created) == events_before
    assert len(delivery_jobs.enqueued) == jobs_before
    assert len(delivery_jobs.completed) == 1
    assert delivery_jobs.failed == []
    assert poll_state.last_seen_post_id == 31

    # --- Iteration 3 (still failing): stays closed and keeps retrying the refresh. ----
    discourse.latest_posts = [_support_post(post_id=31), _private_post(post_id=32)]
    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since="batch-2",
    )
    assert poll_state.visibility_stale is True
    assert len(context.discourse_events.created) == events_before
    assert len(delivery_jobs.enqueued) == jobs_before
    assert len(delivery_jobs.completed) == 1
    assert poll_state.last_seen_post_id == 31

    # --- Recovery: listing works again and reports the category restricted. -----------
    # The stored row flips to non-public/disabled, the private post is skipped by the
    # category gate, and the flag clears.
    discourse.fail_categories = False
    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since="batch-2",
    )
    assert poll_state.visibility_stale is False
    # No new events: the only unseen post (32) is private now, so it must be skipped.
    assert len(context.discourse_events.created) == events_before
    assert len(delivery_jobs.enqueued) == jobs_before
    assert len(delivery_jobs.completed) == 1
    # Skipped posts intentionally do not advance last_seen_post_id (they may be
    # re-evaluated against a later, hopefully-public, snapshot).
    assert poll_state.last_seen_post_id == 31

    # --- A new public post after recovery is delivered again. --------------------------
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": False}
    ]
    discourse.latest_posts = [
        _support_post(post_id=31),
        _private_post(post_id=32),
        _support_post(post_id=33),
    ]
    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since="batch-2",
    )
    assert poll_state.visibility_stale is False
    # With the category public again, the previously withheld post 32 is re-evaluated
    # (skipped posts never advanced last_seen_post_id) and both 32 and 33 bridge now.
    assert len(context.discourse_events.created) == events_before + 2
    assert len(delivery_jobs.enqueued) == jobs_before + 2
    assert len(delivery_jobs.completed) == 3
    assert poll_state.last_seen_post_id == 33


async def test_public_to_private_between_successful_refreshes_delivers_nothing(monkeypatch) -> None:
    """Required regression: public -> private transition between two SUCCESSFUL
    refreshes — no outage, no failed refresh anywhere. Because visibility is
    revalidated before every production poll, the very first poll after the admin
    restriction revalidates against the fresh category listing, sees the category is no
    longer public, and withholds the new post. A cadence-based refresh would keep the
    stored `is_public=TRUE` row authoritative until the next scheduled refresh and
    leak the post inside that window; per-poll revalidation leaves no such window."""

    async def fake_process_sync_messages(**kwargs) -> None:
        return None

    async def fake_deliver_job(**kwargs) -> Any:
        return SimpleNamespace(complete=True, error=None)

    monkeypatch.setattr("dischat.main.process_sync_messages", fake_process_sync_messages)
    monkeypatch.setattr("dischat.main.deliver_job", fake_deliver_job)

    discourse = FakeDiscourseClient()
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": False}
    ]
    discourse.topics[20] = {"category_id": 10, "title": "Support topic"}

    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=True,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("support",),
    )
    room_links = FakeRoomLinks(room_link=room_link)
    room_links.by_category["support"] = [room_link]
    delivery_jobs = FakeDeliveryJobs()
    categories = FakeSyncCategoriesRepo()
    discourse_events = FakeDiscourseEvents()

    context = SimpleNamespace(
        matrix_client=FakeMatrixClient(),
        service=object(),
        discourse_client=discourse,
        chat_accounts=FakeChatAccounts(),
        room_links=room_links,
        file_config=FileConfig(),
        delivery_messages=FakeDeliveryMessages(),
        audit_logs=object(),
        categories=categories,
        discourse_events=discourse_events,
        user_watches=FakeUserWatches(),
        delivery_jobs=delivery_jobs,
    )
    settings = SimpleNamespace(
        poll_interval_seconds=15,
        discourse_relay_matrix_username="MatrixRelayUser",
        discourse_relay_telegram_username="TelegramRelayUser",
        discourse_relay_discord_username="DiscordRelayUser",
        discourse_test_category_id=None,
    )
    poll_state = PollerState()

    # --- Iteration 1 (refresh #1 succeeds): category public, post 31 is delivered. ----
    discourse.latest_posts = [_support_post(post_id=31)]
    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since=None,
    )
    assert poll_state.visibility_stale is False
    assert len(context.discourse_events.created) == 1
    assert len(delivery_jobs.enqueued) == 1
    assert delivery_jobs.completed and not delivery_jobs.failed
    refreshes_after_public = discourse.list_categories_calls
    assert refreshes_after_public == 1

    # --- t1: admin makes category 10 read-restricted. The category listing keeps working
    # (no outage — every refresh below SUCCEEDS), but the admin-authenticated post feed
    # still shows a new post 32 in the now-private category, arriving well before any
    # hypothetical next scheduled refresh. --------------------------------------------
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": True}
    ]
    discourse.latest_posts = [_support_post(post_id=31), _private_post(post_id=32)]
    events_before = len(context.discourse_events.created)
    jobs_before = len(delivery_jobs.enqueued)

    # --- Iteration 2 (refresh #2 also succeeds): the pre-poll revalidation observes the
    # restriction and the category gate withholds the post. ---------------------------
    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since="batch-2",
    )
    assert poll_state.visibility_stale is False  # refresh succeeded; not an outage path
    assert discourse.list_categories_calls == refreshes_after_public + 1  # revalidated
    assert categories.by_discourse_id[10].is_public is False  # stored flag flipped
    # No event, no enqueue, no delivery for the private post.
    assert len(context.discourse_events.created) == events_before
    assert len(delivery_jobs.enqueued) == jobs_before
    assert len(delivery_jobs.completed) == 1
    assert delivery_jobs.failed == []
    assert poll_state.last_seen_post_id == 31

    # --- Iteration 3: the post stays withheld on every subsequent poll; a NEW PUBLIC
    # post 33 in another category bridges normally. ------------------------------------
    discourse.latest_posts = [
        _support_post(post_id=31),
        _private_post(post_id=32),
        {"id": 33, "topic_id": 21, "category_id": 11, "username": "alice", "raw": "other"},
    ]
    discourse.categories = [
        {"id": 10, "slug": "support", "name": "Support", "read_restricted": True},
        {"id": 11, "slug": "general", "name": "General", "read_restricted": False},
    ]
    discourse.topics[21] = {"category_id": 11, "title": "General topic"}
    room_links.by_category["general"] = [room_link]
    await run_iteration(
        context=context,
        settings=settings,
        poll_state=poll_state,
        sync_since="batch-2",
    )
    assert poll_state.visibility_stale is False
    # Only the genuinely public post 33 was created; private post 32 never leaks.
    assert len(context.discourse_events.created) == events_before + 1
    assert len(delivery_jobs.enqueued) == jobs_before + 1
    assert poll_state.last_seen_post_id == 33


def _poll_context(discourse: FakeDiscourseClient, poll_state: PollerState) -> SimpleNamespace:
    return SimpleNamespace(
        matrix_client=FakeMatrixClient(),
        service=object(),
        discourse_client=discourse,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        file_config=FileConfig(),
        delivery_messages=FakeDeliveryMessages(),
        audit_logs=object(),
        categories=FakeSyncCategoriesRepo(),
        discourse_events=FakeDiscourseEvents(),
        user_watches=FakeUserWatches(),
        delivery_jobs=FakeDeliveryJobs(),
    )


async def test_poll_once_refuses_to_run_while_visibility_stale() -> None:
    """Defense in depth: even a direct caller (live-E2E harness style) that passes a
    stale poll state must not evaluate any posts against unverified visibility."""

    class NeverCalledClient(FakeDiscourseClient):
        async def list_latest_posts(self, *, before: int | None = None):
            raise AssertionError("poll_once must not fetch posts while visibility is stale")

        async def get_topic(self, topic_id: int):
            raise AssertionError("poll_once must not fetch topics while visibility is stale")

        async def list_category_latest_posts(self, *, category_slug: str, category_id: int):
            raise AssertionError("poll_once must not fetch topics while visibility is stale")

    discourse = NeverCalledClient()
    categories = FakeSyncCategoriesRepo()
    discourse_events = FakeDiscourseEvents()
    jobs = FakeDeliveryJobs()
    room_links = FakeRoomLinks()
    watches = FakeUserWatches()

    processed = await poll_once(
        client=discourse,
        state=PollerState(visibility_stale=True),
        categories=categories,
        discourse_events=discourse_events,
        room_links=room_links,
        chat_accounts=FakeChatAccounts(),
        user_watches=watches,
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
    )

    assert processed == 0
    assert discourse_events.created == []
    assert jobs.enqueued == []


async def test_poll_once_stale_gate_does_not_block_live_e2e_category() -> None:
    """The live-E2E exception is unaffected by the fail-closed gate: in live-E2E mode the
    single test category is polled via its own category feed and explicit include flag."""

    discourse = FakeDiscourseClient()
    discourse.category_topics = [{"id": 20, "slug": "testing", "title": "Live E2E topic"}]
    discourse.topics[20] = {
        "id": 20,
        "category_id": 56,
        "title": "Live E2E topic",
        "post_stream": {
            "posts": [
                {
                    "id": 41,
                    "topic_id": 20,
                    "post_number": 1,
                    "username": "e2e_user",
                    "cooked": "<p>live body</p>",
                }
            ]
        },
    }
    categories = FakeSyncCategoriesRepo()
    categories.by_discourse_id[56] = SimpleNamespace(id=1, slug="testing", is_public=False)

    discourse_events = FakeDiscourseEvents()
    jobs = FakeDeliveryJobs()
    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=False,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("testing",),
    )
    room_links = FakeRoomLinks(room_link=room_link)
    room_links.by_category["testing"] = [room_link]
    watches = FakeUserWatches()
    delivery_messages = FakeDeliveryMessages()

    processed = await poll_once(
        client=discourse,
        state=PollerState(visibility_stale=True),
        categories=categories,
        discourse_events=discourse_events,
        room_links=room_links,
        chat_accounts=FakeChatAccounts(),
        user_watches=watches,
        delivery_messages=delivery_messages,
        delivery_jobs=jobs,
        live_e2e_category_id=56,
        visibility_stale=True,
    )

    assert processed == 1
    assert len(discourse_events.created) == 1
    assert len(jobs.enqueued) == 1
