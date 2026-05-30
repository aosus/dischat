from __future__ import annotations

from dischat.discourse.models import DiscourseEvent
from dischat.storage.repositories import (
    ChatAccountRepository,
    DeliveryJobRepository,
    RoomLinkRepository,
)


async def route_event(
    *,
    event_id: int,
    discourse_event: DiscourseEvent,
    category_slug: str | None,
    room_links: RoomLinkRepository,
    chat_accounts: ChatAccountRepository,
    delivery_jobs: DeliveryJobRepository,
) -> None:
    if category_slug is not None and discourse_event.event_type == 'new_topic':
        for room_link in await room_links.list_links_matching_category(category_slug):
            await delivery_jobs.enqueue(
                event_id=event_id,
                target_type='room',
                target_mxid=None,
                matrix_room_id=room_link.matrix_room_id,
            )
    if discourse_event.target_discourse_username is not None:
        for account in await chat_accounts.list_by_discourse_username(
            discourse_event.target_discourse_username
        ):
            await delivery_jobs.enqueue(
                event_id=event_id,
                target_type='dm',
                target_mxid=account.mxid,
                matrix_room_id=None,
            )
