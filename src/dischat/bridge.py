from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx

from dischat.i18n import translate
from dischat.matrix.client import MatrixMessage, MatrixSendResult
from dischat.security.audit import AuditEntry
from dischat.security.permissions import can_post_from_chat, detect_platform
from dischat.storage.repositories import (
    ChatAccount,
    DeliveryMessageRecord,
    RoomLinkRecord,
    TargetType,
)


@dataclass(slots=True, frozen=True)
class BridgeResult:
    posted: bool
    discourse_username: str | None = None
    discourse_post_id: int | None = None
    error_message: str | None = None
    matrix_response: MatrixSendResult | None = None


class DiscourseReplyResult(Protocol):
    topic_id: int
    post_id: int


class DiscourseReplyWriter(Protocol):
    async def get_post(self, post_id: int) -> dict[str, object]: ...

    async def create_reply(
        self,
        *,
        topic_id: int,
        raw: str,
        reply_to_post_number: int | None = None,
        api_username: str | None = None,
    ) -> DiscourseReplyResult: ...


class DiscourseTopicReader(Protocol):
    async def get_topic(self, topic_id: int) -> dict[str, object]: ...


class ChatAccountsRepo(Protocol):
    async def ensure_account(
        self, *, mxid: str, platform: str, response_locale: str
    ) -> ChatAccount: ...


class RoomLinksRepo(Protocol):
    async def get_by_room_id(self, matrix_room_id: str) -> RoomLinkRecord | None: ...


class DeliveryMessagesRepo(Protocol):
    async def get_by_matrix_event(
        self,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
    ) -> DeliveryMessageRecord | None: ...

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
    ) -> DeliveryMessageRecord: ...


class AuditLogsRepo(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


class MatrixNoticeClient(Protocol):
    async def send_notice(self, room_id: str, body: str) -> MatrixSendResult: ...


def relay_username_for_platform(*, platform: str, matrix: str, telegram: str, discord: str) -> str:
    if platform == "telegram":
        return telegram
    if platform == "discord":
        return discord
    return matrix


async def handle_matrix_reply(
    *,
    message: MatrixMessage,
    discourse_client: DiscourseReplyWriter,
    matrix_client: MatrixNoticeClient,
    chat_accounts: ChatAccountsRepo,
    room_links: RoomLinksRepo,
    delivery_messages: DeliveryMessagesRepo,
    audit_logs: AuditLogsRepo,
    relay_matrix_username: str,
    relay_telegram_username: str,
    relay_discord_username: str,
) -> BridgeResult:
    if message.parent_event_id is None:
        return BridgeResult(posted=False)

    parent = await delivery_messages.get_by_matrix_event(
        matrix_room_id=message.room_id,
        matrix_event_id=message.parent_event_id,
    )
    if parent is None:
        return BridgeResult(posted=False)

    account = await chat_accounts.ensure_account(
        mxid=message.sender,
        platform=detect_platform(message.sender),
        response_locale="ar",
    )
    room_link = await room_links.get_by_room_id(message.room_id)
    permission = can_post_from_chat(
        is_dm=room_link is None,
        has_parent_bridge_message=True,
        is_paired=account.discourse_username is not None and account.revoked_at is None,
        room_allows_relay=room_link.allow_relay if room_link is not None else False,
    )

    if permission.decision == "reject":
        response = await matrix_client.send_notice(
            message.room_id,
            translate("posting.requires_pairing", account.response_locale),
        )
        return BridgeResult(posted=False, error_message=permission.reason, matrix_response=response)
    if permission.decision == "ignore":
        return BridgeResult(posted=False)

    discourse_username = account.discourse_username
    if permission.decision == "relay":
        discourse_username = relay_username_for_platform(
            platform=account.platform,
            matrix=relay_matrix_username,
            telegram=relay_telegram_username,
            discord=relay_discord_username,
        )
    assert discourse_username is not None
    try:
        parent_post = await discourse_client.get_post(parent.discourse_post_id)
    except httpx.HTTPError:
        parent_post = {}
    reply_to_post_number = parent_post.get("post_number")
    if not isinstance(reply_to_post_number, int):
        if hasattr(discourse_client, "get_topic"):
            topic_payload = await cast("DiscourseTopicReader", discourse_client).get_topic(
                parent.discourse_topic_id
            )
            post_stream = topic_payload.get("post_stream")
            topic_posts: list[dict[str, Any]] = []
            if isinstance(post_stream, dict):
                post_stream_dict = cast("dict[str, Any]", post_stream)
                posts = post_stream_dict.get("posts")
                if isinstance(posts, list):
                    topic_posts = [post for post in posts if isinstance(post, dict)]
            for topic_post in topic_posts:
                if topic_post.get("id") == parent.discourse_post_id:
                    reply_to_post_number = topic_post.get("post_number")
                    break
    if not isinstance(reply_to_post_number, int):
        response = await matrix_client.send_notice(
            message.room_id,
            translate("posting.requires_pairing", account.response_locale),
        )
        return BridgeResult(
            posted=False, error_message="missing_parent_post_number", matrix_response=response
        )

    write_result = await discourse_client.create_reply(
        topic_id=parent.discourse_topic_id,
        raw=message.body,
        reply_to_post_number=reply_to_post_number,
        api_username=discourse_username,
    )
    await delivery_messages.create_mapping(
        discourse_topic_id=write_result.topic_id,
        discourse_post_id=write_result.post_id,
        matrix_room_id=message.room_id,
        matrix_event_id=message.event_id,
        target_type="room",
        target_mxid=None,
        parent_delivery_message_id=parent.id,
    )
    await audit_logs.record(
        AuditEntry(
            action="create_discourse_reply",
            mxid=message.sender,
            platform=account.platform,
            discourse_username_used=discourse_username,
            topic_id=write_result.topic_id,
            post_id=write_result.post_id,
            matrix_room_id=message.room_id,
            matrix_event_id=message.event_id,
            success=True,
        )
    )
    return BridgeResult(
        posted=True,
        discourse_username=discourse_username,
        discourse_post_id=write_result.post_id,
    )
