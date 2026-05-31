from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from dischat.discourse.client import DiscourseClient
from dischat.discourse.models import DiscourseEvent
from dischat.discourse.polling import normalize_post_event
from dischat.discourse.router import route_event
from dischat.storage.repositories import (
    CategoryRepository,
    ChatAccountRepository,
    DeliveryJobRepository,
    DeliveryMessageRepository,
    DiscourseEventRepository,
    RoomLinkRepository,
    UserWatchRepository,
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
    user_watches: UserWatchRepository,
    delivery_messages: DeliveryMessageRepository,
    delivery_jobs: DeliveryJobRepository,
    live_e2e_category_id: int | None = None,
) -> int:
    if live_e2e_category_id is not None:
        live_category = await categories.get_by_discourse_category_id(live_e2e_category_id)
        if live_category is None:
            posts: list[dict[str, object]] = []
        else:
            category_topics = await client.list_category_latest_posts(
                category_slug=live_category.slug,
                category_id=live_e2e_category_id,
            )
            posts = []
            for topic in category_topics:
                topic_id = int(topic["id"])
                topic_payload = await client.get_topic(topic_id)
                posts.extend(
                    dict(
                        topic_post,
                        category_id=topic_payload.get("category_id"),
                        topic_title=topic_payload.get("title"),
                    )
                    for topic_post in topic_payload.get("post_stream", {}).get("posts", [])
                )
    else:
        posts = await client.list_latest_posts(before=None)
    posts = sorted(posts, key=lambda item: int(item["id"]))
    processed = 0
    for post_payload in posts:
        post_id = int(cast("int | str", post_payload["id"]))
        if state.last_seen_post_id is not None and post_id <= state.last_seen_post_id:
            continue
        if post_payload.get("category_id") is None:
            topic_payload = await client.get_topic(int(cast("int | str", post_payload["topic_id"])))
            category_id = topic_payload.get("category_id")
            if category_id is not None:
                post_payload = dict(post_payload)
                post_payload["category_id"] = category_id
                post_payload["topic_title"] = topic_payload.get("title")
                if post_payload.get("cooked") is None:
                    for topic_post in topic_payload.get("post_stream", {}).get("posts", []):
                        if topic_post.get("id") == post_payload.get("id"):
                            cooked = topic_post.get("cooked")
                            if isinstance(cooked, str):
                                post_payload["cooked"] = cooked
                            break
        discourse_event: DiscourseEvent = normalize_post_event(post_payload)
        if discourse_event.reply_to_post_number is not None:
            topic_payload = await client.get_topic(discourse_event.discourse_topic_id)
            for topic_post in topic_payload.get("post_stream", {}).get("posts", []):
                if topic_post.get("post_number") == discourse_event.reply_to_post_number:
                    discourse_event.raw_payload_json["reply_to_discourse_post_id"] = topic_post[
                        "id"
                    ]
                    break
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
        watch_category_id = None
        if discourse_event.category_id is not None:
            category = await categories.get_by_discourse_category_id(discourse_event.category_id)
            if category is not None:
                category_slug = category.slug
                category_id = getattr(category, "id", None)
                if isinstance(category_id, int):
                    watch_category_id = category_id
        await route_event(
            event_id=stored.id,
            discourse_event=discourse_event,
            category_slug=category_slug,
            category_id=watch_category_id,
            room_links=room_links,
            chat_accounts=chat_accounts,
            user_watches=user_watches,
            delivery_messages=delivery_messages,
            delivery_jobs=delivery_jobs,
        )
        state.last_seen_post_id = post_id
        processed += 1
    return processed
