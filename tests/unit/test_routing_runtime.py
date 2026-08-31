from dataclasses import dataclass
from typing import Any

from dischat.bridge import handle_matrix_reply
from dischat.discourse.models import DiscourseEvent
from dischat.discourse.router import route_event
from dischat.discourse.sync import PollerState, poll_once
from dischat.jobs.workers import deliver_job
from dischat.matrix.client import MatrixMessage, MatrixSendResult
from dischat.security.audit import STATUS_FAILED, STATUS_SUCCESS, AuditEntry
from dischat.storage.repositories import (
    ChatAccount,
    DeliveryJobRecord,
    DeliveryMessageRecord,
    RoomLinkRecord,
    TargetType,
)


@dataclass(slots=True)
class FakeDiscourseWriteResult:
    post_id: int = 10
    topic_id: int = 20
    raw: str = "reply"
    post_number: int | None = 2


class FakeDiscourseClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.latest_posts: list[dict[str, object]] = []
        self.topics: dict[int, dict[str, object]] = {}
        self.posts: dict[int, dict[str, object]] = {}
        self.category_topics: list[dict[str, object]] = []

    async def list_category_latest_posts(
        self, *, category_slug: str, category_id: int
    ) -> list[dict[str, object]]:
        return self.category_topics

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
        return FakeDiscourseWriteResult(post_id=99, topic_id=topic_id, raw=raw)

    async def list_latest_posts(self, *, before: int | None = None) -> list[dict[str, object]]:
        return self.latest_posts

    async def get_topic(self, topic_id: int) -> dict[str, object]:
        return self.topics[topic_id]

    async def get_post(self, post_id: int) -> dict[str, object]:
        return self.posts[post_id]


class FakeMatrixClient:
    device_id: str | None = "DEVICE-ROUTING-TEST"

    def __init__(self) -> None:
        self.texts: list[tuple[str, str, dict[str, str] | None]] = []
        self.notices: list[tuple[str, str]] = []
        self.dms: list[tuple[str, str]] = []
        self.replies: list[tuple[str, str, str, dict[str, str] | None]] = []
        # Room "resolved" for unpinned DM sends; tests can rotate this.
        self.next_dm_room: str = "!dm:test"
        self.resolved_rooms: list[str] = []

    async def send_text(
        self,
        room_id: str,
        body: str,
        *,
        formatted: dict[str, str] | None = None,
        tx_id: str | None = None,
    ) -> MatrixSendResult:
        self.texts.append((room_id, body, formatted))
        return MatrixSendResult(event_id="$text", room_id=room_id)

    async def send_notice(self, room_id: str, body: str) -> MatrixSendResult:
        self.notices.append((room_id, body))
        return MatrixSendResult(event_id="$notice", room_id=room_id)

    async def resolve_dm_room(self, mxid: str) -> str:
        self.resolved_rooms.append(mxid)
        return self.next_dm_room

    async def send_dm(
        self,
        room_id: str,
        body: str,
        *,
        formatted: dict[str, str] | None = None,
        tx_id: str | None = None,
    ) -> MatrixSendResult:
        self.dms.append((room_id, body))
        return MatrixSendResult(event_id="$dm", room_id=room_id)

    async def send_reply(
        self,
        room_id: str,
        body: str,
        parent_event_id: str,
        *,
        formatted: dict[str, str] | None = None,
        tx_id: str | None = None,
    ) -> MatrixSendResult:
        self.replies.append((room_id, body, parent_event_id, formatted))
        return MatrixSendResult(event_id="$reply", room_id=room_id)


class FakeChatAccounts:
    def __init__(self, discourse_username: str | None = None, *, locale: str = "ar") -> None:
        self.discourse_username = discourse_username
        self.locale = locale
        self.by_username: dict[str, list[ChatAccount]] = {}
        self.by_mxid: dict[str, ChatAccount] = {}

    async def ensure_account(
        self, *, mxid: str, platform: str, response_locale: str
    ) -> ChatAccount:
        account = ChatAccount(
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
        self.by_mxid[mxid] = account
        return account

    async def list_by_discourse_username(self, discourse_username: str) -> list[ChatAccount]:
        return self.by_username.get(discourse_username, [])

    async def get_by_mxid(self, mxid: str) -> ChatAccount | None:
        return self.by_mxid.get(mxid)


class FakeRoomLinks:
    def __init__(self, room_link: RoomLinkRecord | None = None) -> None:
        self.room_link = room_link
        self.by_category: dict[str, list[RoomLinkRecord]] = {}
        self.by_room: dict[str, RoomLinkRecord] = {}
        self.matching_calls: list[dict[str, object]] = []
        if room_link is not None:
            self.by_room[room_link.matrix_room_id] = room_link

    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord | None:
        return self.by_room.get(matrix_room_id, self.room_link)

    async def list_links_matching_category(
        self,
        category_slug: str,
        *,
        category_id: int | None = None,
        include_non_public: bool = False,
    ) -> list[RoomLinkRecord]:
        self.matching_calls.append(
            {"category_slug": category_slug, "include_non_public": include_non_public}
        )
        return self.by_category.get(category_slug, [])


class FakeDeliveryMessages:
    def __init__(self) -> None:
        self.by_matrix_event: dict[tuple[str, str], DeliveryMessageRecord] = {}
        self.by_discourse_post: dict[int, list[DeliveryMessageRecord]] = {}
        self.by_discourse_post_room: dict[tuple[int, str], DeliveryMessageRecord] = {}
        self.created: list[dict[str, object]] = []

    async def get_by_matrix_event(
        self, *, matrix_room_id: str, matrix_event_id: str
    ) -> DeliveryMessageRecord | None:
        return self.by_matrix_event.get((matrix_room_id, matrix_event_id))

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
        self.created.append(
            {
                "discourse_topic_id": discourse_topic_id,
                "discourse_post_id": discourse_post_id,
                "matrix_room_id": matrix_room_id,
                "matrix_event_id": matrix_event_id,
                "target_type": target_type,
                "target_mxid": target_mxid,
                "parent_delivery_message_id": parent_delivery_message_id,
            }
        )
        record = DeliveryMessageRecord(
            id=len(self.created),
            discourse_topic_id=discourse_topic_id,
            discourse_post_id=discourse_post_id,
            matrix_room_id=matrix_room_id,
            matrix_event_id=matrix_event_id,
            target_type=target_type,
            target_mxid=target_mxid,
            parent_delivery_message_id=parent_delivery_message_id,
        )
        self.by_matrix_event[(matrix_room_id, matrix_event_id)] = record
        self.by_discourse_post.setdefault(discourse_post_id, []).append(record)
        self.by_discourse_post_room[(discourse_post_id, matrix_room_id)] = record
        return record

    async def list_by_discourse_post(
        self, *, discourse_post_id: int
    ) -> list[DeliveryMessageRecord]:
        return self.by_discourse_post.get(discourse_post_id, [])

    async def get_by_discourse_post_and_room(
        self, *, discourse_post_id: int, matrix_room_id: str
    ) -> DeliveryMessageRecord | None:
        return self.by_discourse_post_room.get((discourse_post_id, matrix_room_id))


class FakeAuditLogs:
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
        old = self.entries[audit_log_id - 1]
        self.entries[audit_log_id - 1] = AuditEntry(
            action=old.action,
            discourse_username_used=old.discourse_username_used,
            mxid=old.mxid,
            platform=old.platform,
            discourse_user_id_used=old.discourse_user_id_used,
            topic_id=old.topic_id,
            post_id=post_id,
            matrix_room_id=matrix_room_id or old.matrix_room_id,
            matrix_event_id=matrix_event_id or old.matrix_event_id,
            success=success,
            error_message=error_message,
            status=STATUS_SUCCESS if success else STATUS_FAILED,
        )


class FakeUserWatches:
    def __init__(self) -> None:
        self.mxids_by_category: dict[int, list[str]] = {}
        self.watch_calls: list[dict[str, object]] = []

    async def list_mxids_for_category(
        self, *, category_id: int, include_non_public: bool = False
    ) -> list[str]:
        self.watch_calls.append(
            {"category_id": category_id, "include_non_public": include_non_public}
        )
        return self.mxids_by_category.get(category_id, [])


class FakeDeliveryJobs:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

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


class FakeCategories:
    def __init__(self) -> None:
        self.categories: dict[int, Any] = {}

    async def get_by_discourse_category_id(self, discourse_category_id: int):
        return self.categories.get(discourse_category_id)


class FakeDiscourseEvents:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.event = None
        self.by_id: dict[int, Any] = {}

    async def create_event_if_missing(self, **kwargs):
        self.created.append(kwargs)
        event = type("StoredEvent", (), {"id": 1})()
        return event

    async def get_by_id(self, event_id: int):
        return self.by_id.get(event_id)


async def test_handle_matrix_reply_posts_as_paired_user_with_parent_mapping() -> None:
    discourse = FakeDiscourseClient()
    discourse.posts[30] = {"post_number": 7}
    matrix = FakeMatrixClient()
    audit = FakeAuditLogs()
    delivery_messages = FakeDeliveryMessages()
    delivery_messages.by_matrix_event[("!room:test", "$parent")] = DeliveryMessageRecord(
        id=1,
        discourse_topic_id=20,
        discourse_post_id=30,
        matrix_room_id="!room:test",
        matrix_event_id="$parent",
        target_type="room",
        target_mxid=None,
        parent_delivery_message_id=None,
    )

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
        delivery_messages=delivery_messages,
        audit_logs=audit,
        relay_matrix_username="MatrixRelayUser",
        relay_telegram_username="TelegramRelayUser",
        relay_discord_username="DiscordRelayUser",
    )

    assert result.posted is True
    assert discourse.calls == [
        {
            "topic_id": 20,
            "raw": "hello discourse",
            "reply_to_post_number": 7,
            "api_username": "alice",
        }
    ]
    assert delivery_messages.created[0]["parent_delivery_message_id"] == 1
    assert audit.entries[0].discourse_username_used == "alice"


async def test_route_event_enqueues_watch_dms_and_threaded_room_replies() -> None:
    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=False,
        allow_relay=False,
        full_content=True,
        enabled=True,
        category_slugs=("support",),
    )
    room_links = FakeRoomLinks(room_link)
    room_links.by_category["support"] = [room_link]
    user_watches = FakeUserWatches()
    user_watches.mxids_by_category[10] = ["@watcher:aosus.org"]
    delivery_messages = FakeDeliveryMessages()
    delivery_messages.by_discourse_post[30] = [
        DeliveryMessageRecord(
            id=2,
            discourse_topic_id=20,
            discourse_post_id=30,
            matrix_room_id="!room:test",
            matrix_event_id="$parent",
            target_type="room",
            target_mxid=None,
            parent_delivery_message_id=None,
        )
    ]
    jobs = FakeDeliveryJobs()

    await route_event(
        event_id=5,
        discourse_event=DiscourseEvent(
            discourse_topic_id=20,
            discourse_post_id=31,
            reply_to_post_number=2,
            event_type="new_topic",
            category_id=10,
            author_username="alice",
            target_discourse_username=None,
            raw_payload_json={"reply_to_discourse_post_id": 30},
        ),
        category_slug="support",
        category_id=10,
        room_links=room_links,
        chat_accounts=FakeChatAccounts(),
        user_watches=user_watches,
        delivery_messages=delivery_messages,
        delivery_jobs=jobs,
    )

    assert jobs.enqueued == [
        {
            "event_id": 5,
            "target_type": "room",
            "target_mxid": None,
            "matrix_room_id": "!room:test",
        },
        {
            "event_id": 5,
            "target_type": "dm",
            "target_mxid": "@watcher:aosus.org",
            "matrix_room_id": None,
        },
        {
            "event_id": 5,
            "target_type": "room",
            "target_mxid": None,
            "matrix_room_id": "!room:test",
        },
    ]


async def test_route_event_honors_notification_preferences() -> None:
    jobs = FakeDeliveryJobs()
    chat_accounts = FakeChatAccounts()
    chat_accounts.by_username["bob"] = [
        ChatAccount(
            id=1,
            mxid="@bob:aosus.org",
            platform="matrix",
            discourse_user_id=1,
            discourse_username="bob",
            paired_at=None,
            revoked_at=None,
            notify_on_direct_replies=False,
            notify_on_mentions=True,
            response_locale="ar",
        )
    ]

    await route_event(
        event_id=6,
        discourse_event=DiscourseEvent(
            discourse_topic_id=20,
            discourse_post_id=31,
            reply_to_post_number=2,
            event_type="direct_reply",
            category_id=10,
            author_username="alice",
            target_discourse_username="bob",
            raw_payload_json={},
        ),
        category_slug="support",
        category_id=10,
        room_links=FakeRoomLinks(),
        chat_accounts=chat_accounts,
        user_watches=FakeUserWatches(),
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
    )

    assert jobs.enqueued == []


async def test_poll_once_resolves_parent_discourse_post_id_before_routing() -> None:
    discourse = FakeDiscourseClient()
    discourse.latest_posts = [
        {
            "id": 31,
            "topic_id": 20,
            "category_id": 10,
            "username": "alice",
            "raw": "reply",
            "reply_to_post_number": 2,
        }
    ]
    discourse.topics[20] = {
        "post_stream": {
            "posts": [
                {"id": 30, "post_number": 2},
            ]
        }
    }
    categories = FakeCategories()
    categories.categories[10] = type(
        "Category", (), {"id": 1, "slug": "support", "is_public": True, "enabled": True}
    )()
    discourse_events = FakeDiscourseEvents()

    processed = await poll_once(
        client=discourse,
        state=PollerState(),
        categories=categories,
        discourse_events=discourse_events,
        room_links=FakeRoomLinks(),
        chat_accounts=FakeChatAccounts(),
        user_watches=FakeUserWatches(),
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=FakeDeliveryJobs(),
    )

    assert processed == 1
    assert discourse_events.created[0]["raw_payload_json"]["reply_to_discourse_post_id"] == 30


async def test_poll_once_backfills_cooked_html_from_topic_payload() -> None:
    discourse = FakeDiscourseClient()
    discourse.latest_posts = [
        {
            "id": 31,
            "topic_id": 20,
            "category_id": None,
            "username": "alice",
            "raw": "reply",
        }
    ]
    discourse.topics[20] = {
        "category_id": 10,
        "title": "Support topic",
        "post_stream": {
            "posts": [
                {"id": 31, "post_number": 1, "cooked": "<p><strong>Hello</strong> world</p>"},
            ]
        },
    }
    categories = FakeCategories()
    categories.categories[10] = type(
        "Category", (), {"id": 1, "slug": "support", "is_public": True, "enabled": True}
    )()
    discourse_events = FakeDiscourseEvents()

    processed = await poll_once(
        client=discourse,
        state=PollerState(),
        categories=categories,
        discourse_events=discourse_events,
        room_links=FakeRoomLinks(),
        chat_accounts=FakeChatAccounts(),
        user_watches=FakeUserWatches(),
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=FakeDeliveryJobs(),
    )

    assert processed == 1
    assert discourse_events.created[0]["raw_payload_json"]["topic_title"] == "Support topic"
    assert (
        discourse_events.created[0]["raw_payload_json"]["cooked"]
        == "<p><strong>Hello</strong> world</p>"
    )


async def test_deliver_job_uses_excerpt_and_thread_reply_for_room_jobs() -> None:
    matrix = FakeMatrixClient()
    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=False,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("support",),
    )
    room_links = FakeRoomLinks(room_link)
    discourse_events = FakeDiscourseEvents()
    discourse_events.by_id[1] = type(
        "Event",
        (),
        {
            "discourse_topic_id": 20,
            "discourse_post_id": 31,
            "raw_payload_json": {
                "topic_title": "Support topic",
                "raw": "word " * 100,
                "reply_to_discourse_post_id": 30,
            },
        },
    )()
    delivery_messages = FakeDeliveryMessages()
    delivery_messages.by_discourse_post_room[(30, "!room:test")] = DeliveryMessageRecord(
        id=2,
        discourse_topic_id=20,
        discourse_post_id=30,
        matrix_room_id="!room:test",
        matrix_event_id="$parent",
        target_type="room",
        target_mxid=None,
        parent_delivery_message_id=None,
    )

    result = await deliver_job(
        job=DeliveryJobRecord(
            id=1,
            event_id=1,
            target_type="room",
            target_mxid=None,
            matrix_room_id="!room:test",
            status="pending",
            attempts=0,
            next_attempt_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            last_error=None,
        ),
        discourse_events=discourse_events,
        delivery_messages=delivery_messages,
        chat_accounts=FakeChatAccounts(),
        room_links=room_links,
        matrix_client=matrix,
        audit_logs=FakeAuditLogs(),
    )

    assert result.complete is True
    assert matrix.replies
    assert matrix.notices == []
    assert matrix.texts == []
    assert matrix.replies[0][1].startswith("# Support topic\n\n")
    assert matrix.replies[0][1].endswith("…")
    assert matrix.replies[0][3] is not None
    assert matrix.replies[0][3]["formatted_body"].startswith(
        "<p><strong>Support topic</strong></p><p>"
    )
    assert delivery_messages.created[0]["parent_delivery_message_id"] == 2


async def test_deliver_job_formats_new_topic_with_title_for_room_jobs() -> None:
    matrix = FakeMatrixClient()
    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=False,
        allow_relay=False,
        full_content=True,
        enabled=True,
        category_slugs=("support",),
    )
    room_links = FakeRoomLinks(room_link)
    discourse_events = FakeDiscourseEvents()
    discourse_events.by_id[1] = type(
        "Event",
        (),
        {
            "discourse_topic_id": 20,
            "discourse_post_id": 31,
            "raw_payload_json": {
                "topic_title": "Support topic",
                "raw": "Detailed body",
            },
        },
    )()
    delivery_messages = FakeDeliveryMessages()

    result = await deliver_job(
        job=DeliveryJobRecord(
            id=1,
            event_id=1,
            target_type="room",
            target_mxid=None,
            matrix_room_id="!room:test",
            status="pending",
            attempts=0,
            next_attempt_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            last_error=None,
        ),
        discourse_events=discourse_events,
        delivery_messages=delivery_messages,
        chat_accounts=FakeChatAccounts(),
        room_links=room_links,
        matrix_client=matrix,
        audit_logs=FakeAuditLogs(),
    )

    assert result.complete is True
    assert matrix.texts == [
        (
            "!room:test",
            "# Support topic\n\nDetailed body",
            {
                "format": "org.matrix.custom.html",
                "formatted_body": "<h1>Support topic</h1><p>Detailed body</p>",
            },
        )
    ]
    assert matrix.notices == []
    assert matrix.replies == []


async def _poll_single_post(
    discourse: FakeDiscourseClient,
    categories: FakeCategories,
    *,
    room_links: FakeRoomLinks | None = None,
    user_watches: FakeUserWatches | None = None,
    live_e2e_category_id: int | None = None,
) -> tuple[FakeDiscourseEvents, FakeDeliveryJobs, FakeRoomLinks, FakeUserWatches]:
    discourse_events = FakeDiscourseEvents()
    jobs = FakeDeliveryJobs()
    if room_links is None:
        room_links = FakeRoomLinks()
    if user_watches is None:
        user_watches = FakeUserWatches()
    await poll_once(
        client=discourse,
        state=PollerState(),
        categories=categories,
        discourse_events=discourse_events,
        room_links=room_links,
        chat_accounts=FakeChatAccounts(),
        user_watches=user_watches,
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
        live_e2e_category_id=live_e2e_category_id,
    )
    return discourse_events, jobs, room_links, user_watches


def _category(discourse_category_id: int, *, is_public: bool, enabled: bool = True):
    return type(
        "Category",
        (),
        {
            "id": 1,
            "slug": "support",
            "name": "Support",
            "is_public": is_public,
            "enabled": enabled,
        },
    )()


def _private_post() -> dict[str, object]:
    return {
        "id": 31,
        "topic_id": 20,
        "category_id": 99,
        "username": "alice",
        "raw": "secret",
    }


async def test_poll_skips_unknown_category_before_creating_event_or_job() -> None:
    discourse = FakeDiscourseClient()
    discourse.latest_posts = [_private_post()]
    categories = FakeCategories()  # category 99 has no bootstrap record

    events, jobs, room_links, watches = await _poll_single_post(discourse, categories)

    assert events.created == []
    assert jobs.enqueued == []
    assert room_links.matching_calls == []
    assert watches.watch_calls == []


async def test_poll_skips_non_public_category_before_creating_event_or_job() -> None:
    discourse = FakeDiscourseClient()
    discourse.latest_posts = [_private_post()]
    categories = FakeCategories()
    # Admin API key sees the restricted post; the bootstrap record says it is not public.
    categories.categories[99] = _category(99, is_public=False)
    categories.categories[10] = _category(10, is_public=True)
    # A watcher and a linked room exist for the private slug to prove they are never used.
    watches = FakeUserWatches()
    watches.mxids_by_category[1] = ["@watcher:aosus.org"]
    room_links = FakeRoomLinks()
    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=True,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("support",),
    )
    room_links.by_category["support"] = [room_link]

    discourse_events = FakeDiscourseEvents()
    jobs = FakeDeliveryJobs()
    processed = await poll_once(
        client=discourse,
        state=PollerState(),
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
    assert room_links.matching_calls == []
    assert watches.watch_calls == []


async def test_poll_skips_disabled_category_before_creating_event_or_job() -> None:
    discourse = FakeDiscourseClient()
    discourse.latest_posts = [_private_post()]
    categories = FakeCategories()
    categories.categories[99] = _category(99, is_public=True, enabled=False)

    events, jobs, room_links, watches = await _poll_single_post(discourse, categories)

    assert events.created == []
    assert jobs.enqueued == []
    assert room_links.matching_calls == []
    assert watches.watch_calls == []


async def test_poll_routes_public_category_normally() -> None:
    discourse = FakeDiscourseClient()
    post = dict(_private_post(), category_id=10)
    discourse.latest_posts = [post]
    categories = FakeCategories()
    categories.categories[10] = _category(10, is_public=True)
    room_links = FakeRoomLinks()
    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=True,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("support",),
    )
    room_links.by_category["support"] = [room_link]
    watches = FakeUserWatches()
    watches.mxids_by_category[1] = ["@watcher:aosus.org"]

    events, jobs, room_links_used, watches_used = await _poll_single_post(
        discourse, categories, room_links=room_links, user_watches=watches
    )

    assert events.created != []
    assert jobs.enqueued == [
        {
            "event_id": 1,
            "target_type": "room",
            "target_mxid": None,
            "matrix_room_id": "!room:test",
        },
        {
            "event_id": 1,
            "target_type": "dm",
            "target_mxid": "@watcher:aosus.org",
            "matrix_room_id": None,
        },
    ]
    assert room_links_used.matching_calls == [
        {"category_slug": "support", "include_non_public": False}
    ]
    assert watches_used.watch_calls == [{"category_id": 1, "include_non_public": False}]


async def test_poll_skips_category_that_turns_non_public_between_polls() -> None:
    # Regression: an admin restricts the category while the bridge is running. The stored
    # row is refreshed and the poller must stop bridging it even though earlier posts in
    # the same run were delivered — no event, no delivery job, no routing calls.
    discourse = FakeDiscourseClient()
    categories = FakeCategories()
    categories.categories[99] = _category(99, is_public=True)
    room_links = FakeRoomLinks()
    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=True,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("support",),
    )
    room_links.by_category["support"] = [room_link]
    watches = FakeUserWatches()
    watches.mxids_by_category[1] = ["@watcher:aosus.org"]

    discourse.latest_posts = [dict(_private_post(), id=31)]
    discourse_events = FakeDiscourseEvents()
    jobs = FakeDeliveryJobs()
    state = PollerState()

    first_processed = await poll_once(
        client=discourse,
        state=state,
        categories=categories,
        discourse_events=discourse_events,
        room_links=room_links,
        chat_accounts=FakeChatAccounts(),
        user_watches=watches,
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
    )
    assert first_processed == 1
    assert len(jobs.enqueued) == 2

    # Admin makes the category read-restricted while Dischat is running.
    categories.categories[99] = _category(99, is_public=False)
    discourse.latest_posts = [dict(_private_post(), id=32)]

    second_processed = await poll_once(
        client=discourse,
        state=state,
        categories=categories,
        discourse_events=discourse_events,
        room_links=room_links,
        chat_accounts=FakeChatAccounts(),
        user_watches=watches,
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
    )

    assert second_processed == 0
    assert len(discourse_events.created) == 1
    assert len(jobs.enqueued) == 2
    assert room_links.matching_calls == [{"category_slug": "support", "include_non_public": False}]
    assert watches.watch_calls == [{"category_id": 1, "include_non_public": False}]


async def test_poll_skips_category_removed_between_polls() -> None:
    # A category deleted (or newly unknown) after the bootstrap snapshot must fail closed.
    discourse = FakeDiscourseClient()
    categories = FakeCategories()
    categories.categories[99] = _category(99, is_public=True)
    discourse.latest_posts = [dict(_private_post(), id=41)]
    discourse_events = FakeDiscourseEvents()
    jobs = FakeDeliveryJobs()
    state = PollerState()

    assert (
        await poll_once(
            client=discourse,
            state=state,
            categories=categories,
            discourse_events=discourse_events,
            room_links=FakeRoomLinks(),
            chat_accounts=FakeChatAccounts(),
            user_watches=FakeUserWatches(),
            delivery_messages=FakeDeliveryMessages(),
            delivery_jobs=jobs,
        )
        == 1
    )

    del categories.categories[99]
    discourse.latest_posts = [dict(_private_post(), id=42)]

    processed = await poll_once(
        client=discourse,
        state=state,
        categories=categories,
        discourse_events=discourse_events,
        room_links=FakeRoomLinks(),
        chat_accounts=FakeChatAccounts(),
        user_watches=FakeUserWatches(),
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
    )

    assert processed == 0
    assert len(discourse_events.created) == 1
    assert len(jobs.enqueued) == 0


async def test_live_e2e_mode_allows_the_test_category_despite_non_public_record() -> None:
    discourse = FakeDiscourseClient()
    discourse.category_topics = [{"id": 20}]
    discourse.topics[20] = {
        "category_id": 56,
        "title": "Live E2E topic",
        "post_stream": {"posts": [_private_post()]},
    }
    discourse.latest_posts = []
    for post in discourse.topics[20]["post_stream"]["posts"]:
        post["topic_id"] = 20
    categories = FakeCategories()
    # The live-E2E category is read-restricted on Discourse but explicitly bootstrapped.
    categories.categories[56] = _category(56, is_public=False)
    room_links = FakeRoomLinks()
    room_link = RoomLinkRecord(
        id=1,
        matrix_room_id="!room:test",
        include_all_public_categories=False,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=("support",),
    )
    room_links.by_category["support"] = [room_link]
    watches = FakeUserWatches()
    watches.mxids_by_category[1] = ["@watcher:aosus.org"]

    discourse_events = FakeDiscourseEvents()
    jobs = FakeDeliveryJobs()
    processed = await poll_once(
        client=discourse,
        state=PollerState(),
        categories=categories,
        discourse_events=discourse_events,
        room_links=room_links,
        chat_accounts=FakeChatAccounts(),
        user_watches=watches,
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
        live_e2e_category_id=56,
    )

    assert processed == 1
    assert discourse_events.created != []
    assert jobs.enqueued == [
        {
            "event_id": 1,
            "target_type": "room",
            "target_mxid": None,
            "matrix_room_id": "!room:test",
        },
        {
            "event_id": 1,
            "target_type": "dm",
            "target_mxid": "@watcher:aosus.org",
            "matrix_room_id": None,
        },
    ]
    assert room_links.matching_calls == [{"category_slug": "support", "include_non_public": True}]
    assert watches.watch_calls == [{"category_id": 1, "include_non_public": True}]


async def test_live_e2e_bypass_never_fans_private_category_out_to_all_public_watchers_and_rooms() -> (
    None
):
    # Regression: the live-E2E escape hatch authorizes only the explicitly configured
    # category path. A room linked via include_all_public_categories and a user with an
    # all_public_categories watch must NOT receive the private test category, even though
    # they would receive every genuinely public category.
    discourse = FakeDiscourseClient()
    discourse.category_topics = [{"id": 20}]
    discourse.topics[20] = {
        "category_id": 56,
        "title": "Live E2E topic",
        "post_stream": {"posts": [_private_post()]},
    }
    discourse.latest_posts = []
    for post in discourse.topics[20]["post_stream"]["posts"]:
        post["topic_id"] = 20
    categories = FakeCategories()
    categories.categories[56] = _category(56, is_public=False)
    # The private test category has no explicit room link, only an all-public room.
    room_links = FakeRoomLinks()
    all_public_room = RoomLinkRecord(
        id=2,
        matrix_room_id="!all-public:test",
        include_all_public_categories=True,
        allow_relay=False,
        full_content=False,
        enabled=True,
        category_slugs=(),
    )
    room_links.by_category["all"] = [all_public_room]
    # And no explicit category watch — only an all_public_categories watch.
    watches = FakeUserWatches()
    watches.mxids_by_category[999] = ["@all:aosus.org"]

    discourse_events = FakeDiscourseEvents()
    jobs = FakeDeliveryJobs()
    processed = await poll_once(
        client=discourse,
        state=PollerState(),
        categories=categories,
        discourse_events=discourse_events,
        room_links=room_links,
        chat_accounts=FakeChatAccounts(),
        user_watches=watches,
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
        live_e2e_category_id=56,
    )

    # The test category's own event may still be processed (that is the point of the
    # escape hatch), but NOTHING may be enqueued: the router still queries the category
    # path with the escape hatch, but there is no explicitly linked room and no explicit
    # watch — and critically the all-public room/watch never match it.
    assert processed == 1
    assert discourse_events.created != []
    assert jobs.enqueued == []
    assert room_links.matching_calls == [{"category_slug": "support", "include_non_public": True}]
    assert watches.watch_calls == [{"category_id": 1, "include_non_public": True}]


async def test_route_event_defaults_never_reactivate_non_public_matching() -> None:
    jobs = FakeDeliveryJobs()

    await route_event(
        event_id=7,
        discourse_event=DiscourseEvent(
            discourse_topic_id=20,
            discourse_post_id=31,
            reply_to_post_number=None,
            event_type="new_topic",
            category_id=1,
            author_username="alice",
            target_discourse_username=None,
            raw_payload_json={},
        ),
        category_slug="support",
        category_id=1,
        room_links=FakeRoomLinks(),
        chat_accounts=FakeChatAccounts(),
        user_watches=FakeUserWatches(),
        delivery_messages=FakeDeliveryMessages(),
        delivery_jobs=jobs,
    )

    assert jobs.enqueued == []


async def test_non_public_category_gets_no_exception_outside_live_e2e_mode() -> None:
    # The same discourse category id that live-E2E would allow must stay blocked in
    # production mode: the exception exists only when live_e2e_category_id is configured.
    discourse = FakeDiscourseClient()
    discourse.latest_posts = [dict(_private_post(), category_id=56)]
    categories = FakeCategories()
    categories.categories[56] = _category(56, is_public=False)

    events, jobs, room_links, watches = await _poll_single_post(discourse, categories)

    assert events.created == []
    assert jobs.enqueued == []
    assert room_links.matching_calls == []
    assert watches.watch_calls == []
