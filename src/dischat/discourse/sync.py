from __future__ import annotations

from dataclasses import dataclass

from dischat.discourse.client import DiscourseClient
from dischat.discourse.models import DiscourseEvent
from dischat.discourse.polling import normalize_post_event
from dischat.discourse.router import route_event
from dischat.storage.repositories import (
    CategoryRepository,
    ChatAccountRepository,
    DeliveryJobRepository,
    DiscourseEventRepository,
    RoomLinkRepository,
)


@dataclass(slots=True)
class PollerState:
    last_seen_post_id: int | None = None


async def poll_once(
    *,
    client: DiscourseClient,
    state: PollerState,
    categories: CategoryRepository,
    discourse_events: DiscourseEventRepository,
    room_links: RoomLinkRepository,
    chat_accounts: ChatAccountRepository,
    delivery_jobs: DeliveryJobRepository,
) -> int:
    posts = await client.list_latest_posts(before=None)
    posts = sorted(posts, key=lambda item: int(item['id']))
    processed = 0
    for post_payload in posts:
        post_id = int(post_payload['id'])
        if state.last_seen_post_id is not None and post_id <= state.last_seen_post_id:
            continue
        discourse_event: DiscourseEvent = normalize_post_event(post_payload)
        stored = await discourse_events.create_event_if_missing(
            discourse_topic_id=discourse_event.discourse_topic_id,
            discourse_post_id=discourse_event.discourse_post_id,
            event_type=discourse_event.event_type,
            category_id=discourse_event.category_id,
            author_username=discourse_event.author_username,
            target_discourse_username=discourse_event.target_discourse_username,
            raw_payload_json=discourse_event.raw_payload_json,
        )
        category_slug = None
        if discourse_event.category_id is not None:
            category = await categories.get_by_discourse_category_id(discourse_event.category_id)
            if category is not None:
                category_slug = category.slug
        await route_event(
            event_id=stored.id,
            discourse_event=discourse_event,
            category_slug=category_slug,
            room_links=room_links,
            chat_accounts=chat_accounts,
            delivery_jobs=delivery_jobs,
        )
        state.last_seen_post_id = post_id
        processed += 1
    return processed
