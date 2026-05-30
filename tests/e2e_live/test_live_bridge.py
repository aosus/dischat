from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from dischat.bridge import handle_matrix_reply
from dischat.config import FileConfig, RoomLinkConfig, load_settings
from dischat.discourse.sync import PollerState, poll_once
from dischat.main import drain_delivery_jobs
from dischat.matrix.client import MatrixMessage, NioMatrixClient
from dischat.runtime import build_context
from dischat.subscriptions.bootstrap import (
    sync_categories_from_discourse,
    sync_room_links_from_file,
)
from dischat.testing.live import (
    assert_live_test_category,
    require_live_env,
    wait_for_non_none,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_E2E") != "1",
    reason="Live E2E is disabled unless RUN_LIVE_E2E=1.",
)


@dataclass(slots=True, frozen=True)
class LiveE2EEnv:
    discourse_test_pairing_username: str
    matrix_test_user_a_username: str
    matrix_test_user_a_access_token: str | None
    matrix_test_user_a_password: str
    matrix_test_room_id: str


def load_live_env() -> LiveE2EEnv:
    return LiveE2EEnv(
        discourse_test_pairing_username=require_live_env("DISCOURSE_TEST_PAIRING_USERNAME"),
        matrix_test_user_a_username=require_live_env("MATRIX_TEST_USER_A_USERNAME"),
        matrix_test_user_a_access_token=os.getenv("MATRIX_TEST_USER_A_ACCESS_TOKEN"),
        matrix_test_user_a_password=require_live_env("MATRIX_TEST_USER_A_PASSWORD"),
        matrix_test_room_id=require_live_env("MATRIX_TEST_ROOM_ID"),
    )


def render_live_post_body(post: dict[str, Any]) -> str:
    raw = post.get("raw")
    if isinstance(raw, str) and raw.strip():
        return raw
    cooked = post.get("cooked")
    if not isinstance(cooked, str) or not cooked.strip():
        return ""
    text = re.sub(r"<br\s*/?>", "\n", cooked)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    return text.strip()


async def truncate_runtime_tables(pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            """
            TRUNCATE TABLE
                audit_logs,
                delivery_messages,
                delivery_jobs,
                discourse_events,
                room_link_categories,
                room_links,
                user_watches,
                pairing_sessions,
                chat_accounts,
                categories
            RESTART IDENTITY CASCADE
            """
        )


async def prepare_live_context(*, room_id: str):
    settings = load_settings()
    settings.validate_runtime_requirements()
    assert settings.discourse_test_category_id is not None
    assert_live_test_category(settings.discourse_test_category_id, 56)

    context = await build_context(settings)
    try:
        await context.matrix_client.login()
        await truncate_runtime_tables(context.pool)

        discourse_categories = await context.discourse_client.list_categories()
        category_lookup = await sync_categories_from_discourse(
            categories_repository=context.categories,
            discourse_categories=discourse_categories,
            live_e2e_category_id=settings.discourse_test_category_id,
        )
        live_probe = await context.discourse_client.get_topic(5328)
        live_category = {
            "id": settings.discourse_test_category_id,
            "slug": str(
                live_probe["category_slug"] if "category_slug" in live_probe else "testing"
            ),
        }
        if live_category["slug"] not in category_lookup:
            await context.categories.upsert_category(
                discourse_category_id=settings.discourse_test_category_id,
                slug=str(live_category["slug"]),
                name=str(live_category["slug"]),
                is_public=False,
                enabled=True,
            )
            live_category_record = await context.categories.get_by_discourse_category_id(
                settings.discourse_test_category_id
            )
            assert live_category_record is not None
            category_lookup[live_category_record.slug] = live_category_record.id
        await sync_room_links_from_file(
            room_links_repository=context.room_links,
            file_config=FileConfig(
                rooms={
                    room_id: RoomLinkConfig(
                        categories=[str(live_category["slug"])],
                        allow_relay=True,
                        full_content=True,
                    )
                }
            ),
            category_lookup=category_lookup,
        )
        baseline_posts = []
        category_topics = await context.discourse_client.list_category_latest_posts(
            category_slug=str(live_category["slug"]),
            category_id=settings.discourse_test_category_id,
        )
        for topic in category_topics:
            topic_payload = await context.discourse_client.get_topic(int(topic["id"]))
            baseline_posts.extend(
                dict(topic_post, category_id=topic_payload.get("category_id"))
                for topic_post in topic_payload.get("post_stream", {}).get("posts", [])
            )
        poll_state = PollerState(
            last_seen_post_id=max((int(str(post["id"])) for post in baseline_posts), default=0)
        )
        live_category_record = await context.categories.get_by_discourse_category_id(
            settings.discourse_test_category_id
        )
        assert live_category_record is not None
        return context, settings, poll_state, live_category, live_category_record
    except Exception:
        await context.close()
        raise


async def wait_for_discourse_reply(*, client, topic_id: int, raw: str, username: str):
    async def poll_reply() -> dict[str, Any] | None:
        topic = await client.get_topic(topic_id)
        for post in topic.get("post_stream", {}).get("posts", []):
            if render_live_post_body(post) == raw and post.get("username") == username:
                return dict(post)
        return None

    return await wait_for_non_none(poll_reply, timeout=60.0, interval=2.0)


@pytest.mark.asyncio
async def test_live_room_relay_roundtrip() -> None:
    env = load_live_env()
    (
        context,
        settings,
        poll_state,
        _live_category,
        _live_category_record,
    ) = await prepare_live_context(room_id=env.matrix_test_room_id)
    user_client = NioMatrixClient(
        homeserver_url=settings.matrix_homeserver_url,
        user_id=env.matrix_test_user_a_username,
        access_token=env.matrix_test_user_a_access_token,
        password=env.matrix_test_user_a_password,
    )

    try:
        await user_client.login()
        await context.chat_accounts.ensure_account(
            mxid=env.matrix_test_user_a_username,
            platform="matrix",
            response_locale="en",
        )
        await context.chat_accounts.pair_account(
            mxid=env.matrix_test_user_a_username,
            discourse_username=env.discourse_test_pairing_username,
        )
        await context.matrix_client.invite_user(
            env.matrix_test_room_id,
            env.matrix_test_user_a_username,
        )
        await user_client.join_room(env.matrix_test_room_id)

        marker = uuid4().hex[:12]
        topic_title = f"Live relay room {marker}"
        topic_body = f"live relay room body {marker}"
        topic = await context.discourse_client.create_topic(
            title=topic_title,
            raw=topic_body,
            category_id=settings.discourse_test_category_id,
        )

        processed = await poll_once(
            client=context.discourse_client,
            state=poll_state,
            categories=context.categories,
            discourse_events=context.discourse_events,
            room_links=context.room_links,
            chat_accounts=context.chat_accounts,
            user_watches=context.user_watches,
            delivery_messages=context.delivery_messages,
            delivery_jobs=context.delivery_jobs,
            live_e2e_category_id=settings.discourse_test_category_id,
        )
        assert processed >= 1
        delivered = await drain_delivery_jobs(context)
        assert delivered >= 1

        room_mapping = await context.delivery_messages.get_by_discourse_post_and_room(
            discourse_post_id=topic.post_id,
            matrix_room_id=env.matrix_test_room_id,
        )
        assert room_mapping is not None
        bridged_event = await context.matrix_client.get_event(
            room_id=env.matrix_test_room_id,
            event_id=room_mapping.matrix_event_id,
        )
        assert marker in str(bridged_event.get("content", {}).get("body", ""))

        reply_body = f"live relay response {marker}"
        reply_send = await user_client.send_reply(
            env.matrix_test_room_id,
            reply_body,
            room_mapping.matrix_event_id,
        )
        reply_event = await context.matrix_client.get_event(
            room_id=env.matrix_test_room_id,
            event_id=reply_send.event_id,
        )
        await handle_matrix_reply(
            message=MatrixMessage(
                event_id=reply_send.event_id,
                room_id=env.matrix_test_room_id,
                sender=env.matrix_test_user_a_username,
                body=str(reply_event.get("content", {}).get("body", "")),
                parent_event_id=(
                    reply_event.get("content", {})
                    .get("m.relates_to", {})
                    .get("m.in_reply_to", {})
                    .get("event_id")
                ),
            ),
            discourse_client=context.discourse_client,
            matrix_client=context.matrix_client,
            chat_accounts=context.chat_accounts,
            room_links=context.room_links,
            delivery_messages=context.delivery_messages,
            audit_logs=context.audit_logs,
            relay_matrix_username=settings.discourse_relay_matrix_username,
            relay_telegram_username=settings.discourse_relay_telegram_username,
            relay_discord_username=settings.discourse_relay_discord_username,
        )

        bridged_reply = await wait_for_discourse_reply(
            client=context.discourse_client,
            topic_id=topic.topic_id,
            raw=reply_body,
            username=env.discourse_test_pairing_username,
        )
        assert render_live_post_body(bridged_reply) == reply_body
    finally:
        await user_client.close()
        await context.close()


@pytest.mark.asyncio
async def test_live_category_watch_delivers_dm() -> None:
    env = load_live_env()
    (
        context,
        settings,
        poll_state,
        _live_category,
        live_category_record,
    ) = await prepare_live_context(room_id=env.matrix_test_room_id)

    try:
        await context.chat_accounts.ensure_account(
            mxid=env.matrix_test_user_a_username,
            platform="matrix",
            response_locale="en",
        )
        await context.user_watches.add_category_watch(
            mxid=env.matrix_test_user_a_username,
            category_id=live_category_record.id,
        )

        marker = uuid4().hex[:12]
        topic_body = f"live watch dm body {marker} and enough text for discourse"
        topic = await context.discourse_client.create_topic(
            title=f"Live watch dm {marker}",
            raw=topic_body,
            category_id=settings.discourse_test_category_id,
        )

        processed = await poll_once(
            client=context.discourse_client,
            state=poll_state,
            categories=context.categories,
            discourse_events=context.discourse_events,
            room_links=context.room_links,
            chat_accounts=context.chat_accounts,
            user_watches=context.user_watches,
            delivery_messages=context.delivery_messages,
            delivery_jobs=context.delivery_jobs,
            live_e2e_category_id=settings.discourse_test_category_id,
        )
        assert processed >= 1
        delivered = await drain_delivery_jobs(context)
        assert delivered >= 2

        mappings = await context.delivery_messages.list_by_discourse_post(
            discourse_post_id=topic.post_id,
        )
        dm_mapping = next(
            mapping
            for mapping in mappings
            if mapping.target_type == "dm"
            and mapping.target_mxid == env.matrix_test_user_a_username
        )
        dm_event = await context.matrix_client.get_event(
            room_id=dm_mapping.matrix_room_id,
            event_id=dm_mapping.matrix_event_id,
        )
        assert dm_mapping.matrix_room_id != env.matrix_test_room_id
        assert marker in str(dm_event.get("content", {}).get("body", ""))
    finally:
        await context.close()
