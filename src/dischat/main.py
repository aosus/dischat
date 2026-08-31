from __future__ import annotations

import asyncio
import logging

from dischat.config import load_settings
from dischat.discourse.sync import PollerState, poll_once
from dischat.healthcheck import write_heartbeat
from dischat.jobs.workers import deliver_job
from dischat.logging import configure_logging
from dischat.matrix.handler import process_sync_messages
from dischat.runtime import build_context
from dischat.security.audit import failure_reason
from dischat.service import backoff_delay
from dischat.storage.repositories import DEFAULT_JOB_LEASE_SECONDS
from dischat.subscriptions.bootstrap import (
    sync_categories_from_discourse,
    sync_room_links_from_file,
)

logger = logging.getLogger(__name__)


def _job_lease_seconds(context) -> int:
    """Lease duration for claimed jobs, configurable via DELIVERY_JOB_LEASE_SECONDS."""
    settings = getattr(context, "settings", None)
    configured = getattr(settings, "delivery_job_lease_seconds", DEFAULT_JOB_LEASE_SECONDS)
    return int(configured)


async def drain_delivery_jobs(context) -> int:
    delivered = 0
    lease_seconds = _job_lease_seconds(context)
    while True:
        job = await context.delivery_jobs.claim_next_job(lease_seconds=lease_seconds)
        if job is None:
            return delivered
        heartbeat = asyncio.create_task(
            _renew_job_lease(context, job, lease_seconds),
            name=f"delivery-lease-{job.id}",
        )
        try:
            result = await deliver_job(
                job=job,
                discourse_events=context.discourse_events,
                delivery_messages=context.delivery_messages,
                chat_accounts=context.chat_accounts,
                room_links=context.room_links,
                matrix_client=context.matrix_client,
                delivery_jobs=context.delivery_jobs,
                audit_logs=getattr(context, "audit_logs", None),
                categories=getattr(context, "categories", None),
                live_e2e_category_id=getattr(
                    getattr(context, "settings", None), "discourse_test_category_id", None
                ),
            )
        except asyncio.CancelledError:
            # Shutdown: leave the lease to expire so another worker reclaims the job.
            raise
        except Exception as exc:
            # A raising delivery must never strand the job in 'running': move it to a
            # retryable 'failed' state with the error text and a backoff. If persisting
            # the failure itself fails, the job keeps its (still-expiring) lease and is
            # reclaimed after restart — see docs/operations.md. mark_failed is fenced
            # on the claim token: if our lease expired and the job was reclaimed, the
            # stale update is ignored.
            logger.warning("Delivery job %s raised: %s", job.id, exc)
            await context.delivery_jobs.mark_failed(
                job.id,
                claim_token=job.claim_token or "",
                error=failure_reason(exc),
                next_attempt_at=backoff_delay(job.attempts),
            )
            continue
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        if result.complete:
            fenced = await context.delivery_jobs.mark_complete(
                job.id, claim_token=job.claim_token or ""
            )
            if fenced:
                delivered += 1
            else:
                logger.info(
                    "Delivery job %s completed but its claim was lost; "
                    "leaving the newer claim in charge",
                    job.id,
                )
            continue
        logger.info("Delivery job %s failed: %s", job.id, result.error)
        fenced = await context.delivery_jobs.mark_failed(
            job.id,
            claim_token=job.claim_token or "",
            error=result.error or "unknown_error",
            next_attempt_at=backoff_delay(job.attempts),
        )
        if not fenced:
            logger.info(
                "Delivery job %s failed but its claim was lost; leaving the newer claim in charge",
                job.id,
            )


async def _renew_job_lease(context, job, lease_seconds: int) -> None:
    """Keep a live delivery claim from expiring during slow external I/O."""
    interval = max(1.0, lease_seconds / 3)
    while True:
        await asyncio.sleep(interval)
        renewed = await context.delivery_jobs.renew_lease(
            job.id,
            claim_token=job.claim_token or "",
            lease_seconds=lease_seconds,
        )
        if renewed is None:
            logger.warning("Delivery job %s lost its lease during processing", job.id)
            return


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
    sync_response = await context.matrix_client.sync_once(
        since=sync_since,
        timeout_ms=0 if sync_since is None else settings.poll_interval_seconds * 1000,
    )
    await context.matrix_client.accept_invites(sync_response)
    matrix_batch_result = await process_sync_messages(
        matrix_client=context.matrix_client,
        service=context.service,
        discourse_client=context.discourse_client,
        chat_accounts=context.chat_accounts,
        room_links=context.room_links,
        delivery_messages=context.delivery_messages,
        audit_logs=context.audit_logs,
        event_state=getattr(context, "matrix_state", None),
        relay_matrix_username=settings.discourse_relay_matrix_username,
        relay_telegram_username=settings.discourse_relay_telegram_username,
        relay_discord_username=settings.discourse_relay_discord_username,
        live_e2e_category_id=settings.discourse_test_category_id,
        sync_response=sync_response,
    )
    # Older adapters/test doubles returned None; only an explicit False means
    # the batch contains a live-lease/deferred event and cannot be checkpointed.
    matrix_batch_terminal = matrix_batch_result is not False

    logger = logging.getLogger(__name__)
    # Fail closed: when the pre-poll visibility revalidation failed, do not poll Discourse
    # at all — the stored public flags cannot be trusted until a refresh revalidates them.
    # `refresh_category_visibility` above retries on every iteration and clears the flag
    # on success, so polling resumes automatically. Delivery of already-enqueued jobs
    # continues below so the outbound side keeps draining.
    # Revalidate immediately before the privileged admin-key post poll. Doing
    # this before Matrix long-polling leaves a confidentiality window equal to
    # the sync timeout plus command processing time.
    await refresh_category_visibility(context=context, settings=settings, poll_state=poll_state)
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
    if context_has_matrix_state(context) and poll_state.last_seen_post_id is not None:
        set_discourse_cursor = getattr(
            context.matrix_state, "set_discourse_last_seen_post_id", None
        )
        if set_discourse_cursor is not None:
            await set_discourse_cursor(poll_state.last_seen_post_id)
    if processed:
        logger.info("Processed %s Discourse events", processed)

    delivered = await drain_delivery_jobs(context)
    if delivered:
        logger.info("Delivered %s Matrix jobs", delivered)
    next_batch = getattr(sync_response, "next_batch", None)
    matrix_checkpointed = False
    if matrix_batch_terminal and isinstance(next_batch, str) and context_has_matrix_state(context):
        # Commit the continuation token before retention. If this write fails,
        # no replay fence is pruned from under the still-old durable token.
        await context.matrix_state.set_sync_next_batch(next_batch)
        matrix_checkpointed = True

    # Ledger retention: confirmed ('processed') markers past the window are
    # dead weight — the fence only needs them to outlive the /sync replay
    # horizon. Pruning every iteration keeps matrix_event_state bounded with
    # traffic; claimed/owned/written rows are never touched (removing one
    # could re-open an external write). Failures here must not fail the
    # iteration: retention is housekeeping, not a correctness step.
    if matrix_checkpointed:
        try:
            await context.matrix_state.prune_processed_events(
                older_than_days=settings.matrix_event_retention_days
            )
        except Exception:  # pragma: no cover - defensive: retention is best-effort
            logger.warning("matrix_event_state retention prune failed", exc_info=True)
    if matrix_batch_terminal and isinstance(next_batch, str):
        return next_batch
    return sync_since


def context_has_matrix_state(context) -> bool:
    return getattr(context, "matrix_state", None) is not None


async def run() -> None:
    settings = load_settings()
    settings.validate_runtime_requirements()
    logger = logging.getLogger(__name__)
    logger.info("Dischat service configuration loaded from %s", settings.config_file)
    context = await build_context(settings)
    try:
        await context.matrix_client.login()
        write_heartbeat()

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

        poll_state = PollerState(
            last_seen_post_id=await context.matrix_state.get_discourse_last_seen_post_id()
        )
        sync_since: str | None = await context.matrix_state.get_sync_next_batch()
        if sync_since is not None:
            logger.info("Resuming Matrix sync from persisted token %s", sync_since)
        while True:
            sync_since = await run_iteration(
                context=context,
                settings=settings,
                poll_state=poll_state,
                sync_since=sync_since,
            )
            write_heartbeat()
    finally:
        await context.close()


def main() -> None:
    configure_logging()
    asyncio.run(run())
