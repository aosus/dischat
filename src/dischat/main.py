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

logger = logging.getLogger(__name__)


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


async def refresh_category_visibility(*, context, settings, poll_state: PollerState) -> bool:
    # Security gate: the stored categories rows are the visibility source of truth for the
    # poller, so they are REVALIDATED BEFORE EVERY PRODUCTION POLL — not on a timer. A
    # category that an admin makes read-restricted (or deletes) is therefore caught by the
    # very next poll; a newly public category opens up just as fast. There is no cadence
    # knob to widen this window: any interval between snapshot and poll is a window in
    # which the stored `is_public` flag is an unverified claim. If the revalidation fails
    # — the category listing failing while the admin-authenticated post feed still works
    # is exactly the dangerous case — the last-known snapshot no longer proves anything
    # is still public, so this FAILS CLOSED: the snapshot is marked stale and polling is
    # suspended until a refresh succeeds. `run_iteration` retries on every subsequent
    # iteration; Matrix sync/commands and delivery of already-enqueued jobs are
    # unaffected. On success the snapshot is revalidated and the stale flag clears.
    try:
        discourse_categories = await context.discourse_client.list_categories()
        category_lookup = await sync_categories_from_discourse(
            categories_repository=context.categories,
            discourse_categories=discourse_categories,
            live_e2e_category_id=getattr(settings, "discourse_test_category_id", None),
        )
        # Re-materialize the file-configured room links so explicitly linked rooms pick up
        # categories that were unknown at startup (same idempotent pass as bootstrap).
        await sync_room_links_from_file(
            room_links_repository=context.room_links,
            file_config=context.file_config,
            category_lookup=category_lookup,
        )
    except Exception:
        logger.exception(
            "Category visibility refresh failed; snapshot is stale and polling is "
            "suspended (fail closed) until visibility is revalidated"
        )
        poll_state.visibility_stale = True
        return False
    poll_state.visibility_stale = False
    return True


async def run_iteration(
    *, context, settings, poll_state: PollerState, sync_since: str | None
) -> str | None:
    await refresh_category_visibility(context=context, settings=settings, poll_state=poll_state)
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
    # Fail closed: when the pre-poll visibility revalidation failed, do not poll Discourse
    # at all — the stored public flags cannot be trusted until a refresh revalidates them.
    # `refresh_category_visibility` above retries on every iteration and clears the flag
    # on success, so polling resumes automatically. Delivery of already-enqueued jobs
    # continues below so the outbound side keeps draining.
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
        visibility_stale=poll_state.visibility_stale,
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
