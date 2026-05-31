from __future__ import annotations

from typing import Protocol

from dischat.discourse.models import DiscourseEvent
from dischat.storage.repositories import (
    ChatAccount,
    DeliveryMessageRecord,
    RoomLinkRecord,
    TargetType,
)


class RoomLinksRepo(Protocol):
    async def list_links_matching_category(self, category_slug: str) -> list[RoomLinkRecord]: ...


class ChatAccountsRepo(Protocol):
    async def list_by_discourse_username(self, discourse_username: str) -> list[ChatAccount]: ...


class UserWatchesRepo(Protocol):
    async def list_mxids_for_category(self, *, category_id: int) -> list[str]: ...


class DeliveryMessagesRepo(Protocol):
    async def list_by_discourse_post(
        self, *, discourse_post_id: int
    ) -> list[DeliveryMessageRecord]: ...


class DeliveryJobsRepo(Protocol):
    async def enqueue(
        self,
        *,
        event_id: int,
        target_type: TargetType,
        target_mxid: str | None,
        matrix_room_id: str | None,
    ) -> None: ...


async def route_event(
    *,
    event_id: int,
    discourse_event: DiscourseEvent,
    category_slug: str | None,
    category_id: int | None,
    room_links: RoomLinksRepo,
    chat_accounts: ChatAccountsRepo,
    user_watches: UserWatchesRepo,
    delivery_messages: DeliveryMessagesRepo,
    delivery_jobs: DeliveryJobsRepo,
) -> None:
    if category_slug is not None and discourse_event.event_type == "new_topic":
        for room_link in await room_links.list_links_matching_category(category_slug):
            await delivery_jobs.enqueue(
                event_id=event_id,
                target_type="room",
                target_mxid=None,
                matrix_room_id=room_link.matrix_room_id,
            )
        if category_id is not None:
            for mxid in await user_watches.list_mxids_for_category(category_id=category_id):
                await delivery_jobs.enqueue(
                    event_id=event_id,
                    target_type="dm",
                    target_mxid=mxid,
                    matrix_room_id=None,
                )
    reply_to_post_id = discourse_event.raw_payload_json.get("reply_to_discourse_post_id")
    if isinstance(reply_to_post_id, int):
        mappings = await delivery_messages.list_by_discourse_post(
            discourse_post_id=reply_to_post_id
        )
        for mapping in mappings:
            await delivery_jobs.enqueue(
                event_id=event_id,
                target_type="room",
                target_mxid=None,
                matrix_room_id=mapping.matrix_room_id,
            )
        if mappings:
            return
    if discourse_event.target_discourse_username is not None:
        for account in await chat_accounts.list_by_discourse_username(
            discourse_event.target_discourse_username
        ):
            if (
                discourse_event.event_type == "direct_reply"
                and not account.notify_on_direct_replies
            ):
                continue
            if discourse_event.event_type == "mention" and not account.notify_on_mentions:
                continue
            await delivery_jobs.enqueue(
                event_id=event_id,
                target_type="dm",
                target_mxid=account.mxid,
                matrix_room_id=None,
            )
