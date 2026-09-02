from __future__ import annotations

import typing
from dataclasses import dataclass, field
from typing import Any

import pytest

from dischat.bridge import handle_matrix_reply
from dischat.jobs.workers import deliver_job
from dischat.matrix.client import MatrixMessage, MatrixSendResult
from dischat.matrix.handler import process_sync_messages
from dischat.security.audit import (
    ACTION_DISCOURSE_REPLY,
    ACTION_DM_DELIVERY,
    ACTION_PAIRING_PM,
    ACTION_ROOM_DELIVERY,
    ACTION_SEND_MATRIX_NOTICE,
    LIVE_WRITE_PATHS,
    STATUS_FAILED,
    STATUS_SUCCESS,
    AuditEntry,
    MissingAuditIdError,
    MissingAuditLoggerError,
)
from dischat.storage.repositories import (
    ChatAccount,
    DeliveryJobRecord,
    DeliveryMessageRecord,
    RoomLinkRecord,
    TargetType,
)


class RecordingAuditLogs:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._next_id = 1

    async def record(self, entry: AuditEntry) -> int:
        entry_id = self._next_id
        self._next_id += 1
        self.entries.append(entry)
        return entry_id

    async def update_outcome(
        self,
        audit_log_id: int,
        *,
        success: bool,
        error_message: str | None,
        post_id: int | None = None,
        matrix_event_id: str | None = None,
        matrix_room_id: str | None = None,
    ) -> None:
        self.entries[audit_log_id - 1] = AuditEntry(
            action=self.entries[audit_log_id - 1].action,
            discourse_username_used=self.entries[audit_log_id - 1].discourse_username_used,
            mxid=self.entries[audit_log_id - 1].mxid,
            platform=self.entries[audit_log_id - 1].platform,
            discourse_user_id_used=self.entries[audit_log_id - 1].discourse_user_id_used,
            topic_id=self.entries[audit_log_id - 1].topic_id,
            post_id=post_id,
            matrix_room_id=matrix_room_id or self.entries[audit_log_id - 1].matrix_room_id,
            matrix_event_id=matrix_event_id or self.entries[audit_log_id - 1].matrix_event_id,
            success=success,
            error_message=error_message,
            status=STATUS_SUCCESS if success else STATUS_FAILED,
        )

    def single(self) -> AuditEntry:
        assert len(self.entries) == 1
        return self.entries[0]


@dataclass(slots=True)
class StubCommandResponse:
    body: str = "Pairing code sent."
    pairing_code_to_deliver: str | None = None
    pairing_target_username: str | None = None


class StubService:
    def __init__(self, response: StubCommandResponse | None) -> None:
        self._response = response

    async def handle_message(self, **kwargs: Any) -> StubCommandResponse | None:
        return self._response


class StubDiscourse:
    def __init__(self, *, fail_private_message: bool = False, fail_reply: bool = False) -> None:
        self.private_messages: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.fail_private_message = fail_private_message
        self.fail_reply = fail_reply

    async def create_private_message(
        self, *, target_username: str, title: str, raw: str
    ) -> dict[str, int]:
        if self.fail_private_message:
            raise RuntimeError("Discourse rejected the pairing PM")
        self.private_messages.append(
            {"target_username": target_username, "title": title, "raw_length": len(raw)}
        )
        return {"post_id": 1, "topic_id": 2}

    async def get_post(self, post_id: int) -> dict[str, object]:
        return {"post_number": 2}

    async def create_reply(self, *, topic_id: int, raw: str, **kwargs: Any) -> Any:
        if self.fail_reply:
            raise RuntimeError("Discourse rejected the reply")
        self.replies.append({"topic_id": topic_id, "raw": raw})
        return SimpleWriteResult(post_id=99, topic_id=topic_id)


@dataclass(slots=True)
class SimpleWriteResult:
    post_id: int
    topic_id: int


class StubMatrixClient:
    user_id = "@dischat-bot:aosus.org"

    def __init__(self, messages: list[MatrixMessage] | None = None) -> None:
        self._messages = messages or []
        self.notices: list[tuple[str, str]] = []

    def extract_messages(self, sync_response: Any) -> list[MatrixMessage]:
        return self._messages

    async def send_notice(
        self, room_id: str, body: str, *, tx_id: str | None = None
    ) -> MatrixSendResult:
        self.notices.append((room_id, body))
        return MatrixSendResult(event_id="$notice", room_id=room_id)


def make_sync_message(sender: str = "@alice:aosus.org") -> MatrixMessage:
    return MatrixMessage(
        event_id="$event",
        room_id="!room:test",
        sender=sender,
        body="/pair target_user",
        parent_event_id=None,
    )


async def test_pairing_pm_success_records_audit_entry() -> None:
    audit = RecordingAuditLogs()
    discourse = StubDiscourse()
    matrix = StubMatrixClient([make_sync_message()])
    service = StubService(
        StubCommandResponse(
            body="code sent",
            pairing_code_to_deliver="123456",
            pairing_target_username="target_user",
        )
    )

    await process_sync_messages(
        matrix_client=matrix,
        service=service,
        discourse_client=discourse,
        chat_accounts=None,
        room_links=None,
        delivery_messages=None,
        audit_logs=audit,
        relay_matrix_username="relay_matrix",
        relay_telegram_username="relay_telegram",
        relay_discord_username="relay_discord",
        live_e2e_category_id=None,
        sync_response=None,
    )

    entry, notice_entry = audit.entries
    assert entry.action == ACTION_PAIRING_PM
    assert entry.success is True
    assert entry.discourse_username_used == "target_user"
    assert entry.mxid == "@alice:aosus.org"
    assert entry.platform == "matrix"
    assert entry.error_message is None
    # No secret material: neither the raw pairing code nor its hash.
    assert "123456" not in repr(entry)
    # The command-response notice is itself an audited Matrix write.
    assert notice_entry.action == ACTION_SEND_MATRIX_NOTICE
    assert notice_entry.success is True
    assert notice_entry.matrix_room_id == "!room:test"
    # The successful external write's event id is persisted for correlation.
    assert notice_entry.matrix_event_id == "$notice"


async def test_pairing_pm_failure_records_failed_audit_entry_and_reraises() -> None:
    audit = RecordingAuditLogs()
    discourse = StubDiscourse(fail_private_message=True)
    matrix = StubMatrixClient([make_sync_message()])
    service = StubService(
        StubCommandResponse(
            body="code sent",
            pairing_code_to_deliver="123456",
            pairing_target_username="target_user",
        )
    )

    with pytest.raises(RuntimeError):
        await process_sync_messages(
            matrix_client=matrix,
            service=service,
            discourse_client=discourse,
            chat_accounts=None,
            room_links=None,
            delivery_messages=None,
            audit_logs=audit,
            relay_matrix_username="relay_matrix",
            relay_telegram_username="relay_telegram",
            relay_discord_username="relay_discord",
            live_e2e_category_id=None,
            sync_response=None,
        )

    entry = audit.single()
    assert entry.action == ACTION_PAIRING_PM
    assert entry.success is False
    assert entry.error_message is not None
    assert entry.error_message == "RuntimeError"
    assert entry.discourse_username_used == "target_user"


async def test_discourse_reply_failure_records_failed_audit_entry_and_reraises() -> None:
    audit = RecordingAuditLogs()
    discourse = StubDiscourse(fail_reply=True)

    with pytest.raises(RuntimeError):
        await handle_matrix_reply(
            message=MatrixMessage(
                event_id="$event",
                room_id="!room:test",
                sender="@alice:aosus.org",
                body="hello discourse",
                parent_event_id="$parent",
            ),
            discourse_client=discourse,
            matrix_client=StubReplyMatrix(),
            chat_accounts=StubBridgeAccounts("alice"),
            room_links=StubRoomLinks(),
            delivery_messages=StubBridgeDeliveryMessages(),
            audit_logs=audit,
            relay_matrix_username="relay_matrix",
            relay_telegram_username="relay_telegram",
            relay_discord_username="relay_discord",
        )

    entry = audit.single()
    assert entry.action == ACTION_DISCOURSE_REPLY
    assert entry.success is False
    assert entry.error_message == "RuntimeError"
    assert entry.topic_id == 20
    assert entry.post_id is None
    assert entry.mxid == "@alice:aosus.org"


async def test_discourse_reply_success_still_audits_with_post_ids() -> None:
    audit = RecordingAuditLogs()

    result = await handle_matrix_reply(
        message=MatrixMessage(
            event_id="$event",
            room_id="!room:test",
            sender="@alice:aosus.org",
            body="hello discourse",
            parent_event_id="$parent",
        ),
        discourse_client=StubDiscourse(),
        matrix_client=StubReplyMatrix(),
        chat_accounts=StubBridgeAccounts("alice"),
        room_links=StubRoomLinks(),
        delivery_messages=StubBridgeDeliveryMessages(),
        audit_logs=audit,
        relay_matrix_username="relay_matrix",
        relay_telegram_username="relay_telegram",
        relay_discord_username="relay_discord",
    )

    assert result.posted is True
    entry = audit.single()
    assert entry.action == ACTION_DISCOURSE_REPLY
    assert entry.success is True
    assert entry.post_id == 99
    assert entry.topic_id == 20


@dataclass(slots=True)
class StubAccount:
    platform: str = "matrix"
    discourse_username: str | None = None
    revoked_at: object | None = None
    response_locale: str = "ar"


class StubBridgeAccounts:
    def __init__(self, username: str | None) -> None:
        self._username = username

    async def ensure_account(self, *, mxid: str, platform: str, response_locale: str):
        return StubAccount(platform=platform, discourse_username=self._username)


class StubRoomLinks:
    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord:
        return RoomLinkRecord(
            id=1,
            matrix_room_id=matrix_room_id,
            include_all_public_categories=False,
            allow_relay=False,
            full_content=True,
            enabled=True,
            category_slugs=("support",),
        )


class StubBridgeDeliveryMessages:
    def __init__(self, *, fail_create_mapping: bool = False) -> None:
        self.fail_create_mapping = fail_create_mapping

    async def get_by_matrix_event(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> DeliveryMessageRecord | None:
        if matrix_event_id != "$parent":
            return None
        return DeliveryMessageRecord(
            id=1,
            discourse_topic_id=20,
            discourse_post_id=30,
            matrix_room_id=matrix_room_id,
            matrix_event_id=matrix_event_id,
            target_type="room",
            target_mxid=None,
            parent_delivery_message_id=None,
        )

    async def create_mapping(self, **kwargs: Any) -> DeliveryMessageRecord:
        if self.fail_create_mapping:
            raise RuntimeError("delivery mapping insert failed")
        return DeliveryMessageRecord(
            id=2,
            discourse_topic_id=kwargs["discourse_topic_id"],
            discourse_post_id=kwargs["discourse_post_id"],
            matrix_room_id=kwargs["matrix_room_id"],
            matrix_event_id=kwargs["matrix_event_id"],
            target_type=kwargs["target_type"],
            target_mxid=kwargs["target_mxid"],
            parent_delivery_message_id=kwargs["parent_delivery_message_id"],
        )


class StubReplyMatrix:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    async def send_notice(
        self, room_id: str, body: str, *, tx_id: str | None = None
    ) -> MatrixSendResult:
        self.notices.append((room_id, body))
        return MatrixSendResult(event_id="$notice", room_id=room_id)


def make_delivery_job(target_type: TargetType) -> DeliveryJobRecord:
    return DeliveryJobRecord(
        id=1,
        event_id=1,
        target_type=target_type,
        target_mxid="@bob:aosus.org" if target_type == "dm" else None,
        matrix_room_id="!room:test" if target_type == "room" else None,
        status="pending",
        attempts=0,
        next_attempt_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        last_error=None,
    )


@dataclass(slots=True)
class StubEvent:
    discourse_topic_id: int = 20
    discourse_post_id: int = 31
    raw_payload_json: dict[str, Any] = field(default_factory=dict)


class StubEventsRepo:
    def __init__(self, event: StubEvent | None) -> None:
        self._event = event

    async def get_by_id(self, event_id: int) -> StubEvent | None:
        return self._event


class StubWorkerMessages:
    def __init__(self, *, fail_create_mapping: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.parent_for_room: dict[tuple[int, str], DeliveryMessageRecord] = {}
        self.fail_create_mapping = fail_create_mapping

    async def get_by_discourse_post_and_room(
        self, *, discourse_post_id: int, matrix_room_id: str
    ) -> DeliveryMessageRecord | None:
        return self.parent_for_room.get((discourse_post_id, matrix_room_id))

    async def list_by_discourse_post(
        self, *, discourse_post_id: int
    ) -> list[DeliveryMessageRecord]:
        return [
            record
            for record in self.parent_for_room.values()
            if record.discourse_post_id == discourse_post_id
        ]

    async def create_mapping(self, **kwargs: Any) -> DeliveryMessageRecord:
        if self.fail_create_mapping:
            raise RuntimeError("delivery mapping insert failed")
        self.created.append(kwargs)
        return DeliveryMessageRecord(id=5, **kwargs)


class StubWorkerAccounts:
    async def get_by_mxid(self, mxid: str):
        return StubAccount(response_locale="en")


class StubWorkerRoomLinks:
    def __init__(self, full_content: bool = False) -> None:
        self._full_content = full_content

    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord:
        return RoomLinkRecord(
            id=1,
            matrix_room_id=matrix_room_id,
            include_all_public_categories=False,
            allow_relay=False,
            full_content=self._full_content,
            enabled=True,
            category_slugs=("support",),
        )


class StubWorkerMatrix:
    def __init__(
        self,
        *,
        fail_send_text: bool = False,
        fail_send_dm: bool = False,
        dm_result_room_id: str | None = "!dm:bob",
    ) -> None:
        self.texts: list[tuple[str, str]] = []
        self.dms: list[tuple[str, str]] = []
        self.fail_send_text = fail_send_text
        self.fail_send_dm = fail_send_dm
        self.dm_result_room_id = dm_result_room_id

    async def send_notice(
        self, room_id: str, body: str, *, tx_id: str | None = None
    ) -> MatrixSendResult:
        return MatrixSendResult(event_id="$stub_notice", room_id=room_id)

    async def send_reply(
        self, room_id: str, body: str, parent_event_id: str, *, formatted=None
    ) -> MatrixSendResult:
        if self.fail_send_text:
            raise RuntimeError("Matrix homeserver refused the reply")
        self.texts.append((room_id, body))
        return MatrixSendResult(event_id="$reply1", room_id=room_id)

    async def send_text(self, room_id: str, body: str, *, formatted=None) -> MatrixSendResult:
        if self.fail_send_text:
            raise RuntimeError("Matrix homeserver refused the send")
        self.texts.append((room_id, body))
        return MatrixSendResult(event_id="$text1", room_id=room_id)

    async def resolve_dm_room(self, mxid: str) -> str:
        return self.dm_result_room_id or "!dm:resolved"

    async def send_dm(self, room_id: str, body: str, *, formatted=None) -> MatrixSendResult:
        if self.fail_send_dm:
            raise RuntimeError("Matrix homeserver refused the DM")
        self.dms.append((room_id, body))
        return MatrixSendResult(event_id="$dm1", room_id=self.dm_result_room_id)


ROOM_EVENT = StubEvent(raw_payload_json={"raw": "Post body", "topic_title": "Title"})


async def test_room_delivery_success_records_audit_entry() -> None:
    audit = RecordingAuditLogs()
    matrix = StubWorkerMatrix()

    result = await deliver_job(
        job=make_delivery_job("room"),
        discourse_events=StubEventsRepo(ROOM_EVENT),
        delivery_messages=StubWorkerMessages(),
        chat_accounts=StubWorkerAccounts(),
        room_links=StubWorkerRoomLinks(),
        matrix_client=matrix,
        audit_logs=audit,
    )

    assert result.complete is True
    entry = audit.single()
    assert entry.action == ACTION_ROOM_DELIVERY
    assert entry.success is True
    assert entry.matrix_room_id == "!room:test"
    assert entry.matrix_event_id == "$text1"
    assert entry.topic_id == 20
    assert entry.post_id == 31


async def test_room_delivery_failure_records_failed_audit_entry_and_reraises() -> None:
    audit = RecordingAuditLogs()
    matrix = StubWorkerMatrix(fail_send_text=True)

    with pytest.raises(RuntimeError):
        await deliver_job(
            job=make_delivery_job("room"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=StubWorkerAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=matrix,
            audit_logs=audit,
        )

    entry = audit.single()
    assert entry.action == ACTION_ROOM_DELIVERY
    assert entry.success is False
    assert entry.error_message == "RuntimeError"
    assert entry.matrix_event_id is None
    assert entry.topic_id == 20


async def test_dm_delivery_success_records_audit_entry() -> None:
    audit = RecordingAuditLogs()
    matrix = StubWorkerMatrix()

    result = await deliver_job(
        job=make_delivery_job("dm"),
        discourse_events=StubEventsRepo(ROOM_EVENT),
        delivery_messages=StubWorkerMessages(),
        chat_accounts=StubWorkerAccounts(),
        room_links=StubWorkerRoomLinks(),
        matrix_client=matrix,
        audit_logs=audit,
    )

    assert result.complete is True
    entry = audit.single()
    assert entry.action == ACTION_DM_DELIVERY
    assert entry.success is True
    assert entry.mxid == "@bob:aosus.org"
    assert entry.matrix_event_id == "$dm1"
    assert entry.matrix_room_id == "!dm:bob"


async def test_dm_delivery_failure_records_failed_audit_entry_and_reraises() -> None:
    audit = RecordingAuditLogs()
    matrix = StubWorkerMatrix(fail_send_dm=True)

    with pytest.raises(RuntimeError):
        await deliver_job(
            job=make_delivery_job("dm"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=StubWorkerAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=matrix,
            audit_logs=audit,
        )

    entry = audit.single()
    assert entry.action == ACTION_DM_DELIVERY
    assert entry.success is False
    assert entry.error_message == "RuntimeError"


async def test_missing_event_records_failed_audit_entry() -> None:
    audit = RecordingAuditLogs()

    result = await deliver_job(
        job=make_delivery_job("room"),
        discourse_events=StubEventsRepo(None),
        delivery_messages=StubWorkerMessages(),
        chat_accounts=StubWorkerAccounts(),
        room_links=StubWorkerRoomLinks(),
        matrix_client=StubWorkerMatrix(),
        audit_logs=audit,
    )

    assert result.complete is False
    assert result.error == "missing_discourse_event"
    entry = audit.single()
    assert entry.action == ACTION_ROOM_DELIVERY
    assert entry.success is False
    assert entry.error_message == "missing_discourse_event"


async def test_unsupported_target_records_failed_audit_entry() -> None:
    audit = RecordingAuditLogs()

    result = await deliver_job(
        job=make_delivery_job(typing.cast(TargetType, "broadcast")),
        discourse_events=StubEventsRepo(ROOM_EVENT),
        delivery_messages=StubWorkerMessages(),
        chat_accounts=StubWorkerAccounts(),
        room_links=StubWorkerRoomLinks(),
        matrix_client=StubWorkerMatrix(),
        audit_logs=audit,
    )

    assert result.complete is False
    entry = audit.single()
    assert entry.action == ACTION_DM_DELIVERY
    assert entry.success is False
    assert entry.error_message == "unsupported_delivery_target"


async def test_live_write_paths_registry_covers_expected_actions() -> None:
    actions = {action for action, _platform in LIVE_WRITE_PATHS}
    assert actions == {
        ACTION_PAIRING_PM,
        ACTION_DISCOURSE_REPLY,
        ACTION_ROOM_DELIVERY,
        ACTION_DM_DELIVERY,
        ACTION_SEND_MATRIX_NOTICE,
    }


async def test_audit_logger_required_flag_enforces_coverage() -> None:
    audit_logs = None

    with pytest.raises(MissingAuditLoggerError):
        await _require_wrapper(audit_logs)


async def test_discourse_reply_success_with_mapping_failure_still_audited() -> None:
    """External write succeeded but create_mapping failed: the audit row must
    still exist and record the successful Discourse write."""
    audit = RecordingAuditLogs()
    discourse = StubDiscourse()

    with pytest.raises(RuntimeError, match="delivery mapping insert failed"):
        await handle_matrix_reply(
            message=MatrixMessage(
                event_id="$event",
                room_id="!room:test",
                sender="@alice:aosus.org",
                body="hello discourse",
                parent_event_id="$parent",
            ),
            discourse_client=discourse,
            matrix_client=StubReplyMatrix(),
            chat_accounts=StubBridgeAccounts("alice"),
            room_links=StubRoomLinks(),
            delivery_messages=StubBridgeDeliveryMessages(fail_create_mapping=True),
            audit_logs=audit,
            relay_matrix_username="relay_matrix",
            relay_telegram_username="relay_telegram",
            relay_discord_username="relay_discord",
        )

    entry = audit.single()
    assert entry.action == ACTION_DISCOURSE_REPLY
    # The write DID happen: audit says success even though the mapping failed.
    assert entry.success is True
    assert entry.post_id == 99
    assert entry.topic_id == 20
    assert entry.error_message is None


async def test_room_delivery_success_with_mapping_failure_still_audited() -> None:
    """Matrix send succeeded, mapping insert failed: audit row still resolves
    to success for the write that did happen."""
    audit = RecordingAuditLogs()
    matrix = StubWorkerMatrix()

    result = await deliver_job(
        job=make_delivery_job("room"),
        discourse_events=StubEventsRepo(ROOM_EVENT),
        delivery_messages=StubWorkerMessages(fail_create_mapping=True),
        chat_accounts=StubWorkerAccounts(),
        room_links=StubWorkerRoomLinks(),
        matrix_client=matrix,
        audit_logs=audit,
    )
    assert result.complete is False
    assert result.error == "mapping_persistence_failed"

    entry = audit.single()
    assert entry.action == ACTION_ROOM_DELIVERY
    assert entry.success is True
    assert entry.matrix_event_id == "$text1"
    assert entry.topic_id == 20
    assert entry.post_id == 31


async def test_dm_delivery_success_with_mapping_failure_still_audited() -> None:
    audit = RecordingAuditLogs()
    matrix = StubWorkerMatrix()

    result = await deliver_job(
        job=make_delivery_job("dm"),
        discourse_events=StubEventsRepo(ROOM_EVENT),
        delivery_messages=StubWorkerMessages(fail_create_mapping=True),
        chat_accounts=StubWorkerAccounts(),
        room_links=StubWorkerRoomLinks(),
        matrix_client=matrix,
        audit_logs=audit,
    )
    assert result.complete is False
    assert result.error == "mapping_persistence_failed"

    entry = audit.single()
    assert entry.action == ACTION_DM_DELIVERY
    assert entry.success is True
    assert entry.mxid == "@bob:aosus.org"
    assert entry.matrix_event_id == "$dm1"
    assert entry.matrix_room_id == "!dm:bob"


async def test_attempt_row_is_pending_before_external_write() -> None:
    """The audit row is created BEFORE the write, so a crash between the
    external write and the outcome update still leaves a 'pending' row
    proving the write was attempted/performed."""

    class SpyingAuditLogs(RecordingAuditLogs):
        def __init__(self) -> None:
            super().__init__()
            self.attempt_entries: list[AuditEntry] = []

        async def record(self, entry: AuditEntry) -> int:
            self.attempt_entries.append(entry)
            return await super().record(entry)

    class FailingMappingMessages(StubWorkerMessages):
        async def create_mapping(self, **kwargs: Any) -> DeliveryMessageRecord:
            raise RuntimeError("delivery mapping insert failed")

    audit = SpyingAuditLogs()

    class BoundaryMatrix(StubWorkerMatrix):
        async def send_text(self, room_id: str, body: str, *, formatted=None):
            assert len(audit.attempt_entries) == 1
            pending = audit.attempt_entries[0]
            assert pending.action == ACTION_ROOM_DELIVERY
            assert pending.status == "pending"
            assert pending.success is None
            assert pending.matrix_event_id is None
            return await super().send_text(room_id, body, formatted=formatted)

    matrix = BoundaryMatrix()
    result = await deliver_job(
        job=make_delivery_job("room"),
        discourse_events=StubEventsRepo(ROOM_EVENT),
        delivery_messages=FailingMappingMessages(),
        chat_accounts=StubWorkerAccounts(),
        room_links=StubWorkerRoomLinks(),
        matrix_client=matrix,
        audit_logs=audit,
    )
    assert result.complete is False
    assert result.error == "mapping_persistence_failed"

    # The attempt row was recorded before matrix_client.send_text was called.
    assert audit.attempt_entries, "no attempt row was recorded before the external write"
    assert audit.attempt_entries[0].action == ACTION_ROOM_DELIVERY
    assert audit.attempt_entries[0].matrix_event_id is None
    # A pending attempt row must not claim success: the outcome is unknown.
    assert audit.attempt_entries[0].status == "pending"
    assert audit.attempt_entries[0].success is None


async def test_worker_send_paths_fail_closed_without_audit_logger() -> None:
    """Room/DM delivery is a live write path: with audit_logs=None the worker
    must refuse to send (MissingAuditLoggerError) instead of proceeding
    unaudited, matching the bridge/notice paths."""

    room_matrix = StubWorkerMatrix()
    with pytest.raises(MissingAuditLoggerError):
        await deliver_job(
            job=make_delivery_job("room"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=StubWorkerAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=room_matrix,
            audit_logs=None,
        )
    assert room_matrix.texts == []
    assert room_matrix.dms == []

    dm_matrix = StubWorkerMatrix()
    with pytest.raises(MissingAuditLoggerError):
        await deliver_job(
            job=make_delivery_job("dm"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=StubWorkerAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=dm_matrix,
            audit_logs=None,
        )
    assert dm_matrix.dms == []
    assert dm_matrix.texts == []


async def test_worker_send_paths_fail_closed_when_audit_id_missing() -> None:
    """coderabbit round-4: a live attempt whose audit row cannot be resolved
    to an id (record() returns None) must fail closed BEFORE the external
    write: the outcome could never be recorded for that write."""

    class NoIdAuditLogs(RecordingAuditLogs):
        async def record(self, entry: AuditEntry) -> Any:  # noqa: ANN401
            await super().record(entry)
            return None

    room_matrix = StubWorkerMatrix()
    with pytest.raises(MissingAuditIdError):
        await deliver_job(
            job=make_delivery_job("room"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=StubWorkerAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=room_matrix,
            audit_logs=NoIdAuditLogs(),
        )
    assert room_matrix.texts == []
    assert room_matrix.dms == []

    dm_matrix = StubWorkerMatrix()
    with pytest.raises(MissingAuditIdError):
        await deliver_job(
            job=make_delivery_job("dm"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=StubWorkerAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=dm_matrix,
            audit_logs=NoIdAuditLogs(),
        )
    assert dm_matrix.dms == []
    assert dm_matrix.texts == []


async def test_bridge_reply_fails_closed_when_audit_id_missing() -> None:
    """Same fail-closed contract for the bridge Discourse reply path."""

    class NoIdAuditLogs(RecordingAuditLogs):
        async def record(self, entry: AuditEntry) -> Any:  # noqa: ANN401
            await super().record(entry)
            return None

    audit = NoIdAuditLogs()
    discourse = StubDiscourse()

    with pytest.raises(MissingAuditIdError):
        await handle_matrix_reply(
            message=MatrixMessage(
                event_id="$event",
                room_id="!room:test",
                sender="@alice:aosus.org",
                body="hello discourse",
                parent_event_id="$parent",
            ),
            discourse_client=discourse,
            matrix_client=StubReplyMatrix(),
            chat_accounts=StubBridgeAccounts("alice"),
            room_links=StubRoomLinks(),
            delivery_messages=StubBridgeDeliveryMessages(),
            audit_logs=audit,
            relay_matrix_username="relay_matrix",
            relay_telegram_username="relay_telegram",
            relay_discord_username="relay_discord",
        )

    assert discourse.replies == []


async def test_command_notice_fails_closed_when_audit_id_missing() -> None:
    """Same fail-closed contract for the command-response notice path."""

    class NoIdAuditLogs(RecordingAuditLogs):
        async def record(self, entry: AuditEntry) -> Any:  # noqa: ANN401
            await super().record(entry)
            return None

    matrix = StubMatrixClient([make_sync_message()])

    with pytest.raises(MissingAuditIdError):
        await process_sync_messages(
            matrix_client=matrix,
            service=StubService(StubCommandResponse(body="Watching #general.")),
            discourse_client=StubDiscourse(),
            chat_accounts=None,
            room_links=None,
            delivery_messages=None,
            audit_logs=NoIdAuditLogs(),
            relay_matrix_username="relay_matrix",
            relay_telegram_username="relay_telegram",
            relay_discord_username="relay_discord",
            live_e2e_category_id=None,
            sync_response=None,
        )

    assert matrix.notices == []


async def test_crash_before_outcome_update_leaves_unresolved_not_success() -> None:
    """Simulates a process crash after the external write but before
    update_audit_outcome: the durable audit row must remain status='pending'
    with success=None so operator queries on the boolean cannot report the
    unresolved attempt as a successful write."""

    class CrashBeforeOutcomeAuditLogs(RecordingAuditLogs):
        async def update_outcome(
            self,
            audit_log_id: int,
            *,
            success: bool,
            error_message: str | None,
            post_id: int | None = None,
            matrix_event_id: str | None = None,
            matrix_room_id: str | None = None,
        ) -> None:
            raise RuntimeError("simulated crash before outcome update")

    audit = CrashBeforeOutcomeAuditLogs()
    matrix = StubWorkerMatrix()
    with pytest.raises(RuntimeError, match="simulated crash before outcome update"):
        await deliver_job(
            job=make_delivery_job("room"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=StubWorkerAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=matrix,
            audit_logs=audit,
        )

    # The Matrix send DID happen...
    assert len(matrix.texts) == 1
    # ...but the durable row is unresolved: pending and NOT success=True.
    entry = audit.single()
    assert entry.action == ACTION_ROOM_DELIVERY
    assert entry.status == "pending"
    assert entry.success is None
    assert entry.matrix_event_id is None


class AttemptSpyAuditLogs(RecordingAuditLogs):
    """RecordingAuditLogs that also snapshots every attempt row at record()."""

    def __init__(self) -> None:
        super().__init__()
        self.attempt_entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> int:
        self.attempt_entries.append(entry)
        return await super().record(entry)


async def test_room_delivery_presend_failure_creates_no_pending_attempt_row() -> None:
    """Required by round-3 review: a repository dependency raising BEFORE
    send_text/send_reply must not leave an unresolved pending external-write
    attempt. The attempt row is created only after all local preparation, so
    a failing room-link lookup happens before any audit row exists."""

    class ExplodingRoomLinks(StubWorkerRoomLinks):
        async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord:
            raise RuntimeError("room link lookup failed")

    audit = AttemptSpyAuditLogs()
    matrix = StubWorkerMatrix()

    with pytest.raises(RuntimeError, match="room link lookup failed"):
        await deliver_job(
            job=make_delivery_job("room"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=StubWorkerAccounts(),
            room_links=ExplodingRoomLinks(),
            matrix_client=matrix,
            audit_logs=audit,
        )

    # No Matrix write happened and no audit row exists at all: nothing is
    # left pending with success=NULL as if a write were in flight.
    assert matrix.texts == []
    assert matrix.dms == []
    assert audit.entries == []
    assert audit.attempt_entries == []


async def test_room_delivery_parent_mapping_failure_creates_no_pending_attempt_row() -> None:
    """Same contract for the parent-mapping lookup pre-send dependency."""

    class ExplodingParentLookup(StubWorkerMessages):
        async def get_by_discourse_post_and_room(
            self, *, discourse_post_id: int, matrix_room_id: str
        ) -> DeliveryMessageRecord | None:
            raise RuntimeError("parent mapping lookup failed")

    reply_event = StubEvent(
        raw_payload_json={
            "raw": "Reply body",
            "topic_title": "Title",
            "reply_to_discourse_post_id": 30,
        }
    )
    audit = AttemptSpyAuditLogs()
    matrix = StubWorkerMatrix()

    with pytest.raises(RuntimeError, match="parent mapping lookup failed"):
        await deliver_job(
            job=make_delivery_job("room"),
            discourse_events=StubEventsRepo(reply_event),
            delivery_messages=ExplodingParentLookup(),
            chat_accounts=StubWorkerAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=matrix,
            audit_logs=audit,
        )

    assert matrix.texts == []
    assert matrix.dms == []
    assert audit.entries == []
    assert audit.attempt_entries == []


async def test_dm_delivery_presend_failure_creates_no_pending_attempt_row() -> None:
    """Same contract for the DM account lookup pre-send dependency."""

    class ExplodingAccounts:
        async def get_by_mxid(self, mxid: str) -> ChatAccount | None:
            raise RuntimeError("account lookup failed")

    audit = AttemptSpyAuditLogs()
    matrix = StubWorkerMatrix()

    with pytest.raises(RuntimeError, match="account lookup failed"):
        await deliver_job(
            job=make_delivery_job("dm"),
            discourse_events=StubEventsRepo(ROOM_EVENT),
            delivery_messages=StubWorkerMessages(),
            chat_accounts=ExplodingAccounts(),
            room_links=StubWorkerRoomLinks(),
            matrix_client=matrix,
            audit_logs=audit,
        )

    # No DM was sent and no unresolved attempt row remains.
    assert matrix.dms == []
    assert matrix.texts == []
    assert audit.entries == []
    assert audit.attempt_entries == []


async def test_command_response_notice_is_audited() -> None:
    """A plain command response (no pairing PM) still sends a Matrix notice,
    which is a live external write and must be audited."""
    audit = RecordingAuditLogs()
    matrix = StubMatrixClient([make_sync_message()])

    await process_sync_messages(
        matrix_client=matrix,
        service=StubService(StubCommandResponse(body="Watching #general.")),
        discourse_client=StubDiscourse(),
        chat_accounts=None,
        room_links=None,
        delivery_messages=None,
        audit_logs=audit,
        relay_matrix_username="relay_matrix",
        relay_telegram_username="relay_telegram",
        relay_discord_username="relay_discord",
        live_e2e_category_id=None,
        sync_response=None,
    )

    assert len(matrix.notices) == 1
    entry = audit.single()
    assert entry.action == ACTION_SEND_MATRIX_NOTICE
    assert entry.success is True
    assert entry.mxid == "@alice:aosus.org"
    assert entry.platform == "matrix"
    assert entry.matrix_room_id == "!room:test"


async def test_reject_notice_is_audited() -> None:
    """handle_matrix_reply's permission-rejection notice is audited."""
    audit = RecordingAuditLogs()
    matrix = StubReplyMatrix()

    result = await handle_matrix_reply(
        message=MatrixMessage(
            event_id="$event",
            room_id="!room:test",
            sender="@unpaired:aosus.org",
            body="hello discourse",
            parent_event_id="$parent",
        ),
        discourse_client=StubDiscourse(),
        matrix_client=matrix,
        chat_accounts=StubBridgeAccounts(None),
        room_links=StubRoomLinks(),
        delivery_messages=StubBridgeDeliveryMessages(),
        audit_logs=audit,
        relay_matrix_username="relay_matrix",
        relay_telegram_username="relay_telegram",
        relay_discord_username="relay_discord",
    )

    assert result.posted is False
    assert len(matrix.notices) == 1
    entry = audit.single()
    assert entry.action == ACTION_SEND_MATRIX_NOTICE
    assert entry.success is True
    assert entry.mxid == "@unpaired:aosus.org"
    # Rejection notices persist the delivered Matrix event id too.
    assert entry.matrix_event_id == "$notice"


async def _require_wrapper(audit_logs) -> None:
    from dischat.security.audit import record_audit_entry

    await record_audit_entry(
        audit_logs,
        AuditEntry(action="x", discourse_username_used="y", success=True),
        require_logger=True,
    )
