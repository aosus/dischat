"""Recovery behaviour for stranded delivery jobs (issue #10).

Covers:
- an exception during delivery moves the job to a retryable failed state,
- the drain loop keeps going after an exception,
- at-least-once reconciliation skips sends whose outcome was already persisted.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from dischat.jobs.workers import deliver_job as _deliver_job
from dischat.main import drain_delivery_jobs
from dischat.matrix.client import MatrixSendResult
from dischat.storage.repositories import DeliveryJobRecord, DeliveryMessageRecord, TargetType


class FakeAuditLogs:
    def __init__(self) -> None:
        self._next_id = 1

    async def record(self, entry) -> int:
        audit_id = self._next_id
        self._next_id += 1
        return audit_id

    async def update_outcome(self, audit_log_id: int, **kwargs) -> None:
        return None


async def deliver_job(**kwargs):
    kwargs.setdefault("audit_logs", FakeAuditLogs())
    return await _deliver_job(**kwargs)


@dataclass(slots=True)
class StoredEvent:
    discourse_topic_id: int
    discourse_post_id: int
    raw_payload_json: dict[str, Any]


class FakeDiscourseEvents:
    def __init__(self) -> None:
        self.by_id: dict[int, StoredEvent] = {}

    async def get_by_id(self, event_id: int) -> StoredEvent | None:
        return self.by_id.get(event_id)


class FakeChatAccounts:
    async def get_by_mxid(self, mxid: str) -> None:
        return None


class FakeRoomLinks:
    async def get_by_room_id(self, matrix_room_id: str) -> None:
        return None


@dataclass(slots=True)
class FakeMatrixClient:
    """Matrix client whose sends can be made to raise per room."""

    fail_rooms: set[str] = field(default_factory=set)

    texts: list[tuple[str, str]] = field(default_factory=list)
    dms: list[tuple[str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)
    tx_ids: list[str | None] = field(default_factory=list)
    dm_room_ids: list[str | None] = field(default_factory=list)
    resolved_rooms: list[str] = field(default_factory=list)
    device_id: str | None = "DEVICE-TEST"
    # Room "resolved" when a job has no pin yet; tests can rotate this to prove
    # the pinned room wins on retries.
    unpinned_dm_room: str = "!dm:test"

    async def send_text(
        self, room_id: str, body: str, *, formatted=None, tx_id: str | None = None
    ) -> MatrixSendResult:
        if room_id in self.fail_rooms:
            raise RuntimeError("homeserver unreachable")
        self.texts.append((room_id, body))
        self.tx_ids.append(tx_id)
        return MatrixSendResult(event_id=f"$text-{len(self.texts)}", room_id=room_id)

    async def send_notice(self, room_id: str, body: str) -> MatrixSendResult:
        return MatrixSendResult(event_id="$notice", room_id=room_id)

    async def send_reply(
        self,
        room_id: str,
        body: str,
        parent_event_id: str,
        *,
        formatted=None,
        tx_id: str | None = None,
    ) -> MatrixSendResult:
        if room_id in self.fail_rooms:
            raise RuntimeError("homeserver unreachable")
        self.replies.append((room_id, body, parent_event_id))
        self.tx_ids.append(tx_id)
        return MatrixSendResult(event_id=f"$reply-{len(self.replies)}", room_id=room_id)

    async def resolve_dm_room(self, mxid: str) -> str:
        # A job with no persisted pin "resolves" a room deterministically;
        # tests rotate unpinned_dm_room to prove the persisted pin wins.
        self.resolved_rooms.append(mxid)
        return self.unpinned_dm_room

    async def send_dm(
        self,
        room_id: str,
        body: str,
        *,
        formatted=None,
        tx_id: str | None = None,
    ) -> MatrixSendResult:
        self.dms.append((room_id, body))
        self.tx_ids.append(tx_id)
        self.dm_room_ids.append(room_id)
        return MatrixSendResult(event_id=f"$dm-{len(self.dms)}", room_id=room_id)


@dataclass(slots=True)
class MappingRecord:
    id: int
    discourse_topic_id: int
    discourse_post_id: int
    matrix_room_id: str
    matrix_event_id: str
    target_type: TargetType
    target_mxid: str | None
    parent_delivery_message_id: int | None

    def as_record(self) -> DeliveryMessageRecord:
        return DeliveryMessageRecord(
            id=self.id,
            discourse_topic_id=self.discourse_topic_id,
            discourse_post_id=self.discourse_post_id,
            matrix_room_id=self.matrix_room_id,
            matrix_event_id=self.matrix_event_id,
            target_type=self.target_type,
            target_mxid=self.target_mxid,
            parent_delivery_message_id=self.parent_delivery_message_id,
        )


class FakeDeliveryMessages:
    """In-memory mapping store mirroring DeliveryMessageRepository semantics."""

    def __init__(self) -> None:
        self.by_discourse_post_room: dict[tuple[int, str], MappingRecord] = {}
        self.by_discourse_post: dict[int, list[MappingRecord]] = {}
        self.created: list[dict[str, Any]] = []
        # When True, create_mapping raises — simulating a DB outage in the
        # post-send/pre-persist window.
        self.fail_create: bool = False

    async def get_by_discourse_post_and_room(
        self, *, discourse_post_id: int, matrix_room_id: str
    ) -> DeliveryMessageRecord | None:
        record = self.by_discourse_post_room.get((discourse_post_id, matrix_room_id))
        return record.as_record() if record is not None else None

    async def list_by_discourse_post(
        self, *, discourse_post_id: int
    ) -> list[DeliveryMessageRecord]:
        return [record.as_record() for record in self.by_discourse_post.get(discourse_post_id, [])]

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
        if self.fail_create:
            raise RuntimeError("db unavailable")
        record = MappingRecord(
            id=len(self.created) + 1,
            discourse_topic_id=discourse_topic_id,
            discourse_post_id=discourse_post_id,
            matrix_room_id=matrix_room_id,
            matrix_event_id=matrix_event_id,
            target_type=target_type,
            target_mxid=target_mxid,
            parent_delivery_message_id=parent_delivery_message_id,
        )
        self.created.append({"matrix_event_id": matrix_event_id})
        self.by_discourse_post_room[(discourse_post_id, matrix_room_id)] = record
        self.by_discourse_post.setdefault(discourse_post_id, []).append(record)
        return record.as_record()


class FakeDeliveryJobs:
    def __init__(self, jobs: list[DeliveryJobRecord]) -> None:
        self.jobs = jobs
        self.completed: list[int] = []
        self.failed: list[dict[str, Any]] = []
        self.tx_ids: dict[int, str] = {}
        self.device_ids: dict[int, str] = {}
        self.dm_rooms: dict[int, str | None] = {}

    async def claim_next_job(self, *, lease_seconds: int = 120) -> DeliveryJobRecord | None:
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

    async def ensure_matrix_tx_id(self, job_id: int) -> str:
        if job_id not in self.tx_ids:
            self.tx_ids[job_id] = f"$tx-{job_id}-{len(self.tx_ids)}"
        return self.tx_ids[job_id]

    async def ensure_matrix_device_id(self, job_id: int, *, device_id: str) -> str:
        self.device_ids.setdefault(job_id, device_id)
        return self.device_ids[job_id]

    async def get_matrix_dm_room_id(self, job_id: int) -> str | None:
        return self.dm_rooms.get(job_id)

    async def pin_matrix_dm_room(self, job_id: int, *, room_id: str) -> str:
        self.dm_rooms.setdefault(job_id, room_id)
        return self.dm_rooms[job_id] or room_id


def _make_job(
    job_id: int,
    *,
    attempts: int = 0,
    matrix_room_id: str = "!room:test",
) -> DeliveryJobRecord:
    return DeliveryJobRecord(
        id=job_id,
        event_id=job_id,
        target_type="room",
        target_mxid=None,
        matrix_room_id=matrix_room_id,
        status="running",
        attempts=attempts,
        next_attempt_at=datetime.now(UTC),
        last_error=None,
    )


def _events_with(*post_ids: int) -> FakeDiscourseEvents:
    source = FakeDiscourseEvents()
    for offset, post_id in enumerate(post_ids):
        source.by_id[offset + 1] = StoredEvent(
            discourse_topic_id=10 + offset,
            discourse_post_id=post_id,
            raw_payload_json={"raw": f"post {post_id}"},
        )
    return source


def _make_context(
    jobs_repo: FakeDeliveryJobs,
    matrix_client: FakeMatrixClient,
    events: FakeDiscourseEvents,
    messages: FakeDeliveryMessages,
) -> SimpleNamespace:
    return SimpleNamespace(
        delivery_jobs=jobs_repo,
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix_client,
        audit_logs=FakeAuditLogs(),
    )


async def test_exception_during_delivery_moves_job_to_retryable_failed_state() -> None:
    """A raising deliver_job must leave the job 'failed' with error text and backoff."""
    jobs_repo = FakeDeliveryJobs([_make_job(1)])
    matrix = FakeMatrixClient(fail_rooms={"!room:test"})
    messages = FakeDeliveryMessages()
    context = _make_context(jobs_repo, matrix, _events_with(100), messages)

    delivered = await drain_delivery_jobs(context)

    assert delivered == 0
    assert jobs_repo.completed == []
    assert len(jobs_repo.failed) == 1
    failure = jobs_repo.failed[0]
    assert failure["job_id"] == 1
    assert "RuntimeError" in str(failure["error"])
    # Backoff must schedule the retry in the future.
    assert isinstance(failure["next_attempt_at"], datetime)
    assert failure["next_attempt_at"] > datetime.now(UTC)


async def test_drain_delivery_jobs_keeps_going_after_an_exception() -> None:
    """A raising job must not abort the drain loop; later jobs still deliver."""
    # Job 1 targets the failing room, job 2 a healthy one.
    jobs_repo = FakeDeliveryJobs(
        [
            _make_job(1, matrix_room_id="!broken:test"),
            _make_job(2, matrix_room_id="!healthy:test"),
        ]
    )
    matrix = FakeMatrixClient(fail_rooms={"!broken:test"})
    messages = FakeDeliveryMessages()
    context = _make_context(jobs_repo, matrix, _events_with(100, 101), messages)

    delivered = await drain_delivery_jobs(context)

    assert delivered == 1
    assert [failure["job_id"] for failure in jobs_repo.failed] == [1]
    assert jobs_repo.completed == [2]


async def test_deliver_job_skips_resend_when_mapping_already_persisted() -> None:
    """Send succeeded earlier, persistence of the outcome was lost: no duplicate."""
    matrix = FakeMatrixClient()
    messages = FakeDeliveryMessages()
    messages.by_discourse_post_room[(100, "!room:test")] = MappingRecord(
        id=7,
        discourse_topic_id=10,
        discourse_post_id=100,
        matrix_room_id="!room:test",
        matrix_event_id="$earlier-attempt",
        target_type="room",
        target_mxid=None,
        parent_delivery_message_id=None,
    )

    result = await deliver_job(
        job=_make_job(1),
        discourse_events=_events_with(100),
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
    )

    assert result.complete is True
    assert matrix.texts == []  # nothing re-sent
    assert messages.created == []


async def test_deliver_job_sends_normally_when_no_prior_mapping_exists() -> None:
    matrix = FakeMatrixClient()
    messages = FakeDeliveryMessages()

    result = await deliver_job(
        job=_make_job(1),
        discourse_events=_events_with(100),
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
    )

    assert result.complete is True
    assert len(matrix.texts) == 1
    assert len(messages.created) == 1


async def test_deliver_job_uses_durable_tx_id_across_post_send_persist_failure() -> None:
    """Regression for the review's blocker: send succeeds, create_mapping fails.

    The retry after recovery must reuse the SAME durable transaction id that was
    stamped before the first Matrix write, so the homeserver deduplicates the
    send instead of posting a duplicate message — no reliance on the mapping
    (which never persisted) for idempotency.
    """
    matrix = FakeMatrixClient()
    messages = FakeDeliveryMessages()
    jobs_repo = FakeDeliveryJobs([])
    events = _events_with(100)

    # Attempt 1: Matrix send succeeds, persisting the mapping fails (DB outage).
    messages.fail_create = True
    first = await deliver_job(
        job=_make_job(1),
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
        delivery_jobs=jobs_repo,
    )
    assert first.complete is False
    assert first.error == "mapping_persistence_failed"
    assert len(matrix.texts) == 1
    first_tx_id = matrix.tx_ids[0]
    assert first_tx_id is not None

    # Attempt 2 (after lease expiry + reclaim): same durable tx id, and the
    # homeserver dedupes so no second message goes out.
    messages.fail_create = False
    second = await deliver_job(
        job=_make_job(1, attempts=1),
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
        delivery_jobs=jobs_repo,
    )
    assert second.complete is True
    # The retry MUST reuse the exact tx id from attempt 1.
    assert matrix.tx_ids == [first_tx_id, first_tx_id]
    assert jobs_repo.tx_ids == {1: first_tx_id}
    # Mapping eventually persisted exactly once.
    assert len(messages.created) == 1


async def test_deliver_job_dm_reuses_durable_tx_id_after_persist_failure() -> None:
    """Same post-send/pre-persist window, DM flavour."""
    matrix = FakeMatrixClient()
    messages = FakeDeliveryMessages()
    jobs_repo = FakeDeliveryJobs([])
    events = _events_with(100)

    job = DeliveryJobRecord(
        id=2,
        event_id=1,
        target_type="dm",
        target_mxid="@alice:test",
        matrix_room_id=None,
        status="running",
        attempts=0,
        next_attempt_at=datetime.now(UTC),
        last_error=None,
    )

    messages.fail_create = True
    first = await deliver_job(
        job=job,
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
        delivery_jobs=jobs_repo,
    )
    assert first.complete is False
    assert first.error == "mapping_persistence_failed"
    assert len(matrix.dms) == 1
    first_tx_id = matrix.tx_ids[0]
    assert first_tx_id is not None

    messages.fail_create = False
    second = await deliver_job(
        job=job,
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
        delivery_jobs=jobs_repo,
    )
    assert second.complete is True
    assert matrix.tx_ids == [first_tx_id, first_tx_id]
    assert len(messages.created) == 1


async def test_deliver_job_works_without_delivery_jobs_repo() -> None:
    """Legacy call sites that do not pass a jobs repo still deliver fine."""
    matrix = FakeMatrixClient()
    messages = FakeDeliveryMessages()

    result = await deliver_job(
        job=_make_job(1),
        discourse_events=_events_with(100),
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
    )

    assert result.complete is True
    assert len(matrix.texts) == 1
    assert matrix.tx_ids == [None]
    assert len(messages.created) == 1


async def test_deliver_job_stamps_device_id_and_reuses_tx_id_after_restart() -> None:
    """Retry after restart must use the SAME persisted tx id on the SAME device.

    Matrix dedupes by (device, transaction id): a password re-login without a
    stable device id mints a NEW device, invalidating the persisted tx id. The
    worker stamps the client's device id on the job (so restarts re-login as
    the same device) and reuses the persisted tx id on the retry.
    """
    matrix = FakeMatrixClient()
    messages = FakeDeliveryMessages()
    jobs_repo = FakeDeliveryJobs([])
    events = _events_with(100)

    # Attempt 1 (before "restart"): send ok, mapping persistence fails.
    messages.fail_create = True
    first = await deliver_job(
        job=_make_job(1),
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
        delivery_jobs=jobs_repo,
    )
    assert first.error == "mapping_persistence_failed"
    first_tx_id = matrix.tx_ids[0]
    assert first_tx_id is not None
    # The client's stable device id was stamped on the job for restart safety.
    assert jobs_repo.device_ids == {1: matrix.device_id}

    # Attempt 2 (simulated restart — fresh client instance, same device id):
    # the retry reuses the SAME persisted tx id, valid on the same device.
    restarted_matrix = FakeMatrixClient(device_id=matrix.device_id)
    messages.fail_create = False
    second = await deliver_job(
        job=_make_job(1, attempts=1),
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=restarted_matrix,
        delivery_jobs=jobs_repo,
    )
    assert second.complete is True
    assert restarted_matrix.tx_ids == [first_tx_id]
    # Restart still stamps the same (stable) device id on the job.
    assert jobs_repo.device_ids == {1: matrix.device_id}
    assert len(messages.created) == 1


async def test_deliver_job_dm_retry_is_pinned_to_the_same_room() -> None:
    """DM retry must target the SAME room: tx ids are endpoint-scoped.

    A retry that re-resolves the DM room could pick (or create) a different
    room; the same tx id sent to a different /rooms/{roomId}/send endpoint
    cannot deduplicate. The first resolved room is persisted on the job and
    every later attempt is pinned to it.
    """
    matrix = FakeMatrixClient()
    messages = FakeDeliveryMessages()
    jobs_repo = FakeDeliveryJobs([])
    events = _events_with(100)

    job = DeliveryJobRecord(
        id=3,
        event_id=1,
        target_type="dm",
        target_mxid="@alice:test",
        matrix_room_id=None,
        status="running",
        attempts=0,
        next_attempt_at=datetime.now(UTC),
        last_error=None,
    )

    # Attempt 1: unpinned — resolves a room, which is persisted on the job.
    # Use the post-send/pre-persist window (mapping persistence fails) so the
    # retry actually re-sends instead of being short-circuited by reconciliation.
    messages.fail_create = True
    first = await deliver_job(
        job=job,
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
        delivery_jobs=jobs_repo,
    )
    assert first.error == "mapping_persistence_failed"
    first_room = matrix.dm_room_ids[0]
    assert first_room is not None
    assert jobs_repo.dm_rooms == {3: first_room}

    # Attempt 2 (retry after lease expiry): the resolved room is pinned, so the
    # send goes to exactly the SAME endpoint even though the fake would
    # otherwise "resolve" a different room for an unpinned call.
    matrix.dm_room_ids.clear()
    matrix.unpinned_dm_room = "!dm:other"
    messages.fail_create = False
    second = await deliver_job(
        job=job,
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
        delivery_jobs=jobs_repo,
    )
    assert second.complete is True
    # The retry used the pinned room, NOT the rotating re-resolution.
    assert matrix.dm_room_ids == [first_room]
    assert jobs_repo.dm_rooms == {3: first_room}


async def test_deliver_job_dm_pin_is_persisted_before_the_matrix_send() -> None:
    """Regression: the DM room pin must be durable BEFORE the Matrix write.

    Previously the pin was written after send_dm() returned. If the process
    died in between (send accepted by the homeserver, pin never committed),
    the restarted worker re-ran room resolution and could pick a DIFFERENT
    room — the same durable tx id then hit a different
    /rooms/{roomId}/send endpoint, where it cannot deduplicate, and a
    duplicate message went out. The pin must therefore be committed before
    the send, and the send must target the pinned room.
    """
    matrix = FakeMatrixClient()
    messages = FakeDeliveryMessages()
    jobs_repo = FakeDeliveryJobs([])
    events = _events_with(100)

    job = DeliveryJobRecord(
        id=4,
        event_id=1,
        target_type="dm",
        target_mxid="@alice:test",
        matrix_room_id=None,
        status="running",
        attempts=0,
        next_attempt_at=datetime.now(UTC),
        last_error=None,
    )

    # Attempt 1: resolve + pin, then the send is ACCEPTED by the homeserver
    # and the process dies before ANY post-send DB write. Model the crash by
    # aborting inside send_dm — no mapping, no completion, nothing persisted
    # after the pin except the room id itself.
    class CrashingAfterAccept(FakeMatrixClient):
        async def send_dm(
            self, room_id: str, body: str, *, formatted=None, tx_id=None
        ) -> MatrixSendResult:
            raise RuntimeError("process died after homeserver accepted the event")

    matrix = CrashingAfterAccept()
    with pytest.raises(RuntimeError):
        await deliver_job(
            job=job,
            discourse_events=events,
            delivery_messages=messages,
            chat_accounts=FakeChatAccounts(),
            room_links=FakeRoomLinks(),
            matrix_client=matrix,
            delivery_jobs=jobs_repo,
        )

    # The pin was already committed before the send: no crash window remains.
    first_room = jobs_repo.dm_rooms.get(4)
    assert first_room is not None
    assert matrix.dm_room_ids == []

    # Attempt 2 (restart): the resolver would "resolve" a DIFFERENT room for
    # an unpinned job, but the persisted pin forces the retry to the SAME
    # endpoint, where the durable tx id deduplicates.
    matrix2 = FakeMatrixClient()
    matrix2.unpinned_dm_room = "!dm:other-after-restart"
    second = await deliver_job(
        job=job,
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix2,
        delivery_jobs=jobs_repo,
    )
    assert second.complete is True
    assert matrix2.dm_room_ids == [first_room]
    assert matrix2.resolved_rooms == []  # never re-resolved
    assert jobs_repo.dm_rooms == {4: first_room}
    assert len(messages.created) == 1


async def test_deliver_job_refuses_send_on_device_mismatch() -> None:
    """Regression: a job stamped for device A must not be sent from device B.

    Matrix deduplicates tx ids per (device, transaction id). If the service
    restarts with a different MATRIX_DEVICE_ID (or a replacement access token
    mints a new device), the job's persisted tx id would not deduplicate on
    the new device. The worker must fail closed instead of silently sending.
    """
    matrix = FakeMatrixClient(device_id="DEVICE-A")
    messages = FakeDeliveryMessages()
    jobs_repo = FakeDeliveryJobs([])
    events = _events_with(100)

    # First attempt stamps DEVICE-A on the job (mapping persistence fails so
    # the job stays retryable).
    messages.fail_create = True
    first = await deliver_job(
        job=_make_job(1),
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=matrix,
        delivery_jobs=jobs_repo,
    )
    assert first.error == "mapping_persistence_failed"
    assert jobs_repo.device_ids == {1: "DEVICE-A"}

    # Restart with a DIFFERENT device id: the send must be refused.
    messages.fail_create = False
    restarted = FakeMatrixClient(device_id="DEVICE-B")
    second = await deliver_job(
        job=_make_job(1, attempts=1),
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=restarted,
        delivery_jobs=jobs_repo,
    )
    assert second.complete is False
    assert second.error == "matrix_device_mismatch"
    # Fail closed: nothing was sent and no mapping was created.
    assert restarted.texts == []
    assert len(messages.created) == 0
    # The original stamp is preserved — the job still belongs to DEVICE-A.
    assert jobs_repo.device_ids == {1: "DEVICE-A"}

    # Same-device restart still works normally.
    same_device = FakeMatrixClient(device_id="DEVICE-A")
    third = await deliver_job(
        job=_make_job(1, attempts=1),
        discourse_events=events,
        delivery_messages=messages,
        chat_accounts=FakeChatAccounts(),
        room_links=FakeRoomLinks(),
        matrix_client=same_device,
        delivery_jobs=jobs_repo,
    )
    assert third.complete is True
    assert len(same_device.texts) == 1
