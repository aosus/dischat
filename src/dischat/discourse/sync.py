from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast

from dischat.discourse.models import DiscourseEvent
from dischat.discourse.polling import normalize_post_event
from dischat.discourse.router import (
    ChatAccountsRepo,
    DeliveryJobsRepo,
    DeliveryMessagesRepo,
    RoomLinksRepo,
    UserWatchesRepo,
    route_event,
)

logger = logging.getLogger(__name__)


class DiscourseClientLike(Protocol):
    async def list_latest_posts(self, *, before: int | None = None) -> list[dict[str, object]]: ...

    async def get_topic(self, topic_id: int) -> dict[str, object]: ...


class CategoryFeedClient(Protocol):
    async def list_category_latest_posts(
        self, *, category_slug: str, category_id: int
    ) -> list[dict[str, object]]: ...


class CategoryRef(Protocol):
    id: int
    slug: str
    is_public: bool
    enabled: bool


class CategoriesRepo(Protocol):
    async def get_by_discourse_category_id(
        self, discourse_category_id: int
    ) -> CategoryRef | None: ...


class StoredEventRef(Protocol):
    id: int


class DiscourseEventsRepo(Protocol):
    async def create_event_if_missing(
        self,
        *,
        discourse_topic_id: int,
        discourse_post_id: int,
        event_type: str,
        category_id: int | None,
        author_username: str,
        target_discourse_username: str | None,
        raw_payload_json: dict[str, Any],
    ) -> StoredEventRef: ...


@dataclass(slots=True)
class PollerState:
    last_seen_post_id: int | None = None
    # Set when the pre-poll visibility revalidation failed: the stored snapshot can no
    # longer be trusted as proof that a category is public, so polling must not evaluate
    # posts. Visibility itself is revalidated before every poll; this flag only records
    # the failed-attempt state until the next attempt succeeds.
    visibility_stale: bool = False


def _topic_posts(topic_payload: dict[str, object]) -> list[dict[str, object]]:
    post_stream = topic_payload.get("post_stream")
    if not isinstance(post_stream, dict):
        return []
    post_stream_dict = cast("dict[str, object]", post_stream)
    posts = post_stream_dict.get("posts")
    if not isinstance(posts, list):
        return []
    return [cast("dict[str, object]", post) for post in posts if isinstance(post, dict)]


async def _list_unseen_latest_posts(
    client: DiscourseClientLike, *, last_seen_post_id: int | None
) -> list[dict[str, object]]:
    """Page backwards until the durable cursor is reached.

    On a brand-new deployment there is deliberately no historical cursor, so
    only the current latest page is considered. After that first successful
    scan, every restart can backfill however many pages arrived while the
    service was offline.
    """
    page = await client.list_latest_posts(before=None)
    pages = [page]
    if last_seen_post_id is None:
        return page

    while page:
        oldest_id = min(int(cast("int | str", post["id"])) for post in page)
        if oldest_id <= last_seen_post_id:
            break
        page = await client.list_latest_posts(before=oldest_id)
        if page:
            next_oldest_id = min(int(cast("int | str", post["id"])) for post in page)
            if next_oldest_id >= oldest_id:
                raise RuntimeError(
                    "Discourse latest-post pagination did not advance before the cursor"
                )
        pages.append(page)

    deduplicated: dict[int, dict[str, object]] = {}
    for fetched_page in pages:
        for post in fetched_page:
            post_id = int(cast("int | str", post["id"]))
            deduplicated[post_id] = post
    return list(deduplicated.values())


async def poll_once(
    *,
    client: DiscourseClientLike,
    state: PollerState,
    categories: CategoriesRepo,
    discourse_events: DiscourseEventsRepo,
    room_links: RoomLinksRepo,
    chat_accounts: ChatAccountsRepo,
    user_watches: UserWatchesRepo,
    delivery_messages: DeliveryMessagesRepo,
    delivery_jobs: DeliveryJobsRepo,
    live_e2e_category_id: int | None = None,
    visibility_stale: bool = False,
) -> int:
    # Fail-closed gate (polling boundary): when the pre-poll visibility revalidation
    # failed, the stored `is_public`/`enabled` flags no longer prove a category is still
    # public (an admin may have restricted it since the last successful refresh, while
    # the admin-key post feed keeps working). Rather than route on possibly outdated
    # visibility, skip the entire poll — nothing is read as "skipped content",
    # `last_seen_post_id` does not advance, and the same posts are re-evaluated against a
    # fresh snapshot once visibility is revalidated. `run_iteration` retries the
    # revalidation on every iteration while this flag is set. The live-E2E category is
    # exempt: it is already isolated to live-E2E mode by its exact ID and never consults
    # this snapshot.
    if (visibility_stale or state.visibility_stale) and live_e2e_category_id is None:
        logger.warning(
            "Skipping Discourse poll: category visibility snapshot is stale "
            "(refresh failed); no content will be routed until visibility is revalidated"
        )
        return 0
    if live_e2e_category_id is not None:
        live_category = await categories.get_by_discourse_category_id(live_e2e_category_id)
        if live_category is None:
            posts: list[dict[str, object]] = []
        else:
            category_topics = await cast("CategoryFeedClient", client).list_category_latest_posts(
                category_slug=live_category.slug,
                category_id=live_e2e_category_id,
            )
            posts = []
            for topic in category_topics:
                topic_id = int(cast("int | str", topic["id"]))
                topic_payload = await client.get_topic(topic_id)
                posts.extend(
                    dict(
                        topic_post,
                        category_id=topic_payload.get("category_id"),
                        topic_title=topic_payload.get("title"),
                    )
                    for topic_post in _topic_posts(topic_payload)
                )
    else:
        posts = await _list_unseen_latest_posts(client, last_seen_post_id=state.last_seen_post_id)
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
                    for topic_post in _topic_posts(topic_payload):
                        if topic_post.get("id") == post_payload.get("id"):
                            cooked = topic_post.get("cooked")
                            if isinstance(cooked, str):
                                post_payload["cooked"] = cooked
                            break
        # Category gate (polling boundary): resolve the post's category against the bootstrap
        # record *before* any event or delivery job is created. The Discourse client runs with
        # an admin API key, so what the API returns must never be trusted here; only the stored
        # `is_public`/`enabled` flags decide visibility. Unknown, disabled, and non-public
        # categories are skipped entirely. The one exception is the explicit live-E2E category,
        # isolated to live-E2E mode by its exact discourse category ID.
        raw_category_id = post_payload.get("category_id")
        discourse_category_id: int | None = None
        if isinstance(raw_category_id, int | str):
            discourse_category_id = int(raw_category_id)
        include_non_public_category = False
        if discourse_category_id is None:
            state.last_seen_post_id = post_id
            continue
        if live_e2e_category_id is not None:
            # The live feed is a strict, single-category test fixture. Never
            # route an unexpected category returned by that endpoint.
            if discourse_category_id != live_e2e_category_id:
                state.last_seen_post_id = post_id
                continue
            include_non_public_category = True
        else:
            category_record = await categories.get_by_discourse_category_id(discourse_category_id)
            if (
                category_record is None
                or not category_record.enabled
                or not category_record.is_public
            ):
                # A successfully revalidated snapshot proves this post is not
                # eligible. Advance the scan cursor so content that was private
                # when observed cannot be retroactively leaked if its category
                # becomes public later.
                state.last_seen_post_id = post_id
                continue
        discourse_event: DiscourseEvent = normalize_post_event(post_payload)
        if discourse_event.reply_to_post_number is not None:
            topic_payload = await client.get_topic(discourse_event.discourse_topic_id)
            for topic_post in _topic_posts(topic_payload):
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
            include_non_public_category=include_non_public_category,
        )
        state.last_seen_post_id = post_id
        processed += 1
    return processed
