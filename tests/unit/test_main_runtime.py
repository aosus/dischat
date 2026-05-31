from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from dischat.discourse.sync import PollerState
from dischat.main import drain_delivery_jobs, run_iteration
from dischat.storage.repositories import DeliveryJobRecord


class FakeMatrixClient:
    def __init__(self) -> None:
        self.sync_calls: list[dict[str, Any]] = []
        self.accepted: list[object] = []

    async def sync_once(self, *, since: str | None = None, timeout_ms: int = 0):
        self.sync_calls.append({"since": since, "timeout_ms": timeout_ms})
        return SimpleNamespace(next_batch="batch-2")

    async def accept_invites(self, sync_response) -> None:
        self.accepted.append(sync_response)


class FakeDeliveryJobs:
    def __init__(self, jobs: list[DeliveryJobRecord]) -> None:
        self.jobs = jobs
        self.completed: list[int] = []
        self.failed: list[dict[str, object]] = []

    async def claim_next_job(self) -> DeliveryJobRecord | None:
        if not self.jobs:
            return None
        return self.jobs.pop(0)

    async def mark_complete(self, job_id: int) -> None:
        self.completed.append(job_id)

    async def mark_failed(self, job_id: int, *, error: str, next_attempt_at: datetime) -> None:
        self.failed.append({"job_id": job_id, "error": error, "next_attempt_at": next_attempt_at})


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
