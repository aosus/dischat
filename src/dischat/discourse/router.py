from __future__ import annotations

from dischat.discourse.models import DiscourseEvent
from dischat.storage.repositories import (
    ChatAccountRepository,
    DeliveryJobRepository,
    DeliveryMessageRepository,
    RoomLinkRepository,
    UserWatchRepository,
)


async def route_event(
    *,
    event_id: int,
    discourse_event: DiscourseEvent,
    category_slug: str | None,
    category_id: int | None,
    room_links: RoomLinkRepository,
    chat_accounts: ChatAccountRepository,
    user_watches: UserWatchRepository,
    delivery_messages: DeliveryMessageRepository,
    delivery_jobs: DeliveryJobRepository,
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
