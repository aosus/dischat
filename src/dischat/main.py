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


async def run() -> None:
    settings = load_settings()
    settings.validate_runtime_requirements()
    logger = logging.getLogger(__name__)
    logger.info("Dischat service configuration loaded from %s", settings.config_file)
    context = await build_context(settings)
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
    sync_response = await context.matrix_client.sync_once()
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

    processed = await poll_once(
        client=context.discourse_client,
        state=poll_state,
        categories=context.categories,
        discourse_events=context.discourse_events,
        room_links=context.room_links,
        chat_accounts=context.chat_accounts,
        delivery_jobs=context.delivery_jobs,
    )
    logger.info("Processed %s Discourse events", processed)

    while True:
        job = await context.delivery_jobs.claim_next_job()
        if job is None:
            break
        result = await deliver_job(
            job=job,
            discourse_events=context.discourse_events,
            delivery_messages=context.delivery_messages,
            chat_accounts=context.chat_accounts,
            matrix_client=context.matrix_client,
        )
        if result.complete:
            await context.delivery_jobs.mark_complete(job.id)
        else:
            await context.delivery_jobs.mark_failed(
                job.id,
                error=result.error or "unknown_error",
                next_attempt_at=backoff_delay(job.attempts),
            )


def main() -> None:
    configure_logging()
    asyncio.run(run())
