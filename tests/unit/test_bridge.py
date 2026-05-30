from dataclasses import dataclass

from dischat.bridge import handle_matrix_reply
from dischat.matrix.client import MatrixMessage, MatrixSendResult
from dischat.security.audit import AuditEntry
from dischat.storage.repositories import ChatAccount, DeliveryMessageRecord, RoomLinkRecord


@dataclass(slots=True)
class FakeDiscourseWriteResult:
    post_id: int = 10
    topic_id: int = 20
    raw: str = "reply"
    post_number: int | None = 2


class FakeDiscourseClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def create_reply(self, *, topic_id: int, raw: str):
        self.calls.append((topic_id, raw))
        return FakeDiscourseWriteResult(post_id=99, topic_id=topic_id, raw=raw)


class FakeMatrixClient:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []
        self.dms: list[tuple[str, str]] = []
        self.replies: list[tuple[str, str, str]] = []

    async def send_notice(self, room_id: str, body: str) -> MatrixSendResult:
        self.notices.append((room_id, body))
        return MatrixSendResult(event_id="$notice", room_id=room_id)

    async def send_dm(self, mxid: str, body: str) -> MatrixSendResult:
        self.dms.append((mxid, body))
        return MatrixSendResult(event_id="$dm", room_id="!dm:test")

    async def send_reply(self, room_id: str, body: str, parent_event_id: str) -> MatrixSendResult:
        self.replies.append((room_id, body, parent_event_id))
        return MatrixSendResult(event_id="$reply", room_id=room_id)


class FakeChatAccounts:
    def __init__(self, discourse_username: str | None) -> None:
        self.discourse_username = discourse_username

    async def ensure_account(
        self, *, mxid: str, platform: str, response_locale: str
    ) -> ChatAccount:
        return ChatAccount(
            id=1,
            mxid=mxid,
            platform=platform,
            discourse_user_id=None,
            discourse_username=self.discourse_username,
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


class FakeDeliveryMessages:
    async def get_by_matrix_event(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> DeliveryMessageRecord | None:
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


class FakeAuditLogs:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


async def test_handle_matrix_reply_posts_as_paired_user() -> None:
    discourse = FakeDiscourseClient()
    matrix = FakeMatrixClient()
    audit = FakeAuditLogs()

    result = await handle_matrix_reply(
        message=MatrixMessage(
            event_id="$event",
            room_id="!room:test",
            sender="@alice:aosus.org",
            body="hello discourse",
            parent_event_id="$parent",
        ),
        discourse_client=discourse,
        matrix_client=matrix,
        chat_accounts=FakeChatAccounts("alice"),
        room_links=FakeRoomLinks(
            RoomLinkRecord(
                id=1,
                matrix_room_id="!room:test",
                include_all_public_categories=False,
                allow_relay=False,
                full_content=True,
                enabled=True,
                category_slugs=("support",),
            )
        ),
        delivery_messages=FakeDeliveryMessages(),
        audit_logs=audit,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    assert result.posted is True
    assert result.discourse_username == "alice"
    assert discourse.calls == [(20, "hello discourse")]
    assert audit.entries[0].discourse_username_used == "alice"


async def test_handle_matrix_reply_uses_relay_when_allowed() -> None:
    discourse = FakeDiscourseClient()
    matrix = FakeMatrixClient()

    result = await handle_matrix_reply(
        message=MatrixMessage(
            event_id="$event",
            room_id="!room:test",
            sender="@telegram_123:aosus.org",
            body="relay me",
            parent_event_id="$parent",
        ),
        discourse_client=discourse,
        matrix_client=matrix,
        chat_accounts=FakeChatAccounts(None),
        room_links=FakeRoomLinks(
            RoomLinkRecord(
                id=1,
                matrix_room_id="!room:test",
                include_all_public_categories=False,
                allow_relay=True,
                full_content=True,
                enabled=True,
                category_slugs=("support",),
            )
        ),
        delivery_messages=FakeDeliveryMessages(),
        audit_logs=FakeAuditLogs(),
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    assert result.posted is True
    assert result.discourse_username == "TelegramRelayUser"


async def test_handle_matrix_reply_rejects_unpaired_user_when_relay_disabled() -> None:
    discourse = FakeDiscourseClient()
    matrix = FakeMatrixClient()

    result = await handle_matrix_reply(
        message=MatrixMessage(
            event_id="$event",
            room_id="!room:test",
            sender="@bob:aosus.org",
            body="blocked",
            parent_event_id="$parent",
        ),
        discourse_client=discourse,
        matrix_client=matrix,
        chat_accounts=FakeChatAccounts(None),
        room_links=FakeRoomLinks(
            RoomLinkRecord(
                id=1,
                matrix_room_id="!room:test",
                include_all_public_categories=False,
                allow_relay=False,
                full_content=True,
                enabled=True,
                category_slugs=("support",),
            )
        ),
        delivery_messages=FakeDeliveryMessages(),
        audit_logs=FakeAuditLogs(),
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    assert result.posted is False
    assert result.error_message == "room_requires_pairing"
    assert matrix.notices
    assert discourse.calls == []
