from __future__ import annotations

import asyncio
import logging

from dischat.config import load_settings
from dischat.discourse.sync import PollerState, poll_once
from dischat.jobs.workers import deliver_job
from dischat.logging import configure_logging
from dischat.matrix.handler import process_sync_messages
from dischat.runtime import build_context
from dischat.service import backoff_delay
from dischat.subscriptions.bootstrap import (
    sync_categories_from_discourse,
    sync_room_links_from_file,
)


async def drain_delivery_jobs(context) -> int:
    delivered = 0
    while True:
        job = await context.delivery_jobs.claim_next_job()
        if job is None:
            return delivered
        result = await deliver_job(
            job=job,
            discourse_events=context.discourse_events,
            delivery_messages=context.delivery_messages,
            chat_accounts=context.chat_accounts,
            room_links=context.room_links,
            matrix_client=context.matrix_client,
        )
        if result.complete:
            await context.delivery_jobs.mark_complete(job.id)
            delivered += 1
            continue
        await context.delivery_jobs.mark_failed(
            job.id,
            error=result.error or "unknown_error",
            next_attempt_at=backoff_delay(job.attempts),
        )


async def run_iteration(
    *, context, settings, poll_state: PollerState, sync_since: str | None
) -> str | None:
    sync_response = await context.matrix_client.sync_once(
        since=sync_since,
        timeout_ms=0 if sync_since is None else settings.poll_interval_seconds * 1000,
    )
    await context.matrix_client.accept_invites(sync_response)
    await process_sync_messages(
        matrix_client=context.matrix_client,
        service=context.service,
        discourse_client=context.discourse_client,
        chat_accounts=context.chat_accounts,
        room_links=context.room_links,
        delivery_messages=context.delivery_messages,
        audit_logs=context.audit_logs,
        relay_matrix_username=settings.discourse_relay_matrix_username,
        relay_telegram_username=settings.discourse_relay_telegram_username,
        relay_discord_username=settings.discourse_relay_discord_username,
        live_e2e_category_id=settings.discourse_test_category_id,
        sync_response=sync_response,
    )

    logger = logging.getLogger(__name__)
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
    )
    if processed:
        logger.info("Processed %s Discourse events", processed)

    delivered = await drain_delivery_jobs(context)
    if delivered:
        logger.info("Delivered %s Matrix jobs", delivered)
    next_batch = getattr(sync_response, "next_batch", None)
    return next_batch if isinstance(next_batch, str) else sync_since


async def run() -> None:
    settings = load_settings()
    settings.validate_runtime_requirements()
    logger = logging.getLogger(__name__)
    logger.info("Dischat service configuration loaded from %s", settings.config_file)
    context = await build_context(settings)
    try:
        await context.matrix_client.login()

        discourse_categories = await context.discourse_client.list_categories()
        category_lookup = await sync_categories_from_discourse(
            categories_repository=context.categories,
            discourse_categories=discourse_categories,
            live_e2e_category_id=settings.discourse_test_category_id,
        )
        await sync_room_links_from_file(
            room_links_repository=context.room_links,
            file_config=context.file_config,
            category_lookup=category_lookup,
        )

        poll_state = PollerState()
        sync_since: str | None = None
        while True:
            sync_since = await run_iteration(
                context=context,
                settings=settings,
                poll_state=poll_state,
                sync_since=sync_since,
            )
    finally:
        await context.close()


def main() -> None:
    configure_logging()
    asyncio.run(run())
