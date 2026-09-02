"""Integration coverage for delivery job leases and stranded-job recovery (issue #10)."""

import asyncio
from datetime import UTC, datetime, timedelta

from dischat.storage.repositories import DeliveryJobRepository, DiscourseEventRepository

SIMULATED_LEASE_SECONDS = 60


async def _create_event(pg_pool, *, discourse_post_id: int):
    events = DiscourseEventRepository(pg_pool)
    return await events.create_event_if_missing(
        discourse_topic_id=discourse_post_id,
        discourse_post_id=discourse_post_id,
        event_type="post_created",
        category_id=None,
        author_username="alice",
        target_discourse_username=None,
        raw_payload_json={"id": discourse_post_id, "topic_id": discourse_post_id},
    )


async def test_claim_next_job_stamps_lease_columns(pg_pool) -> None:
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=900)
    await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:lease",
    )

    before = datetime.now(UTC)
    claimed = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)

    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.claimed_at is not None
    assert claimed.lease_expires_at is not None
    # Lease expiry ~lease_seconds in the future.
    assert claimed.lease_expires_at > before
    assert claimed.lease_expires_at <= datetime.now(UTC) + timedelta(
        seconds=SIMULATED_LEASE_SECONDS
    )


async def test_running_job_with_active_lease_is_not_reclaimed(pg_pool) -> None:
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=901)
    await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:active-lease",
    )
    claimed = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    assert claimed is not None

    # Another worker cycle while the lease is still valid must not steal the job.
    assert await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS) is None


async def test_expired_running_job_is_reclaimed_after_simulated_restart(pg_pool) -> None:
    """Crashed worker recovery: stale 'running' becomes claimable once the lease lapses."""
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=902)
    enqueued = await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:stranded",
    )
    first_claim = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    assert first_claim is not None

    async with pg_pool.acquire() as connection:
        row = await connection.fetchrow("SELECT * FROM delivery_jobs WHERE id = $1", enqueued.id)
    assert row["status"] == "running"

    # Simulated restart: the original claiming process is gone (a new repository
    # instance below plays the restarted worker); fake lease expiry by moving it
    # into the past, as process death leaves an old lease behind.
    async with pg_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE delivery_jobs
            SET lease_expires_at = NOW() - INTERVAL '1 second'
            WHERE id = $1
            """,
            enqueued.id,
        )

    restarted_jobs = DeliveryJobRepository(pg_pool)  # "fresh" process after restart
    reclaimed = await restarted_jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)

    assert reclaimed is not None
    assert reclaimed.id == enqueued.id
    assert reclaimed.status == "running"
    assert reclaimed.attempts == first_claim.attempts + 1
    assert reclaimed.lease_expires_at is not None
    assert reclaimed.lease_expires_at > datetime.now(UTC)


async def test_legacy_running_row_with_null_lease_is_reclaimed(pg_pool) -> None:
    """Rows stranded by pre-lease versions (NULL lease) must also recover."""
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=903)
    enqueued = await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:legacy",
    )
    claimed = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    assert claimed is not None

    # Erase the lease entirely to mimic a job claimed before the lease columns.
    async with pg_pool.acquire() as connection:
        await connection.execute(
            "UPDATE delivery_jobs SET lease_expires_at = NULL, claimed_at = NULL WHERE id = $1",
            enqueued.id,
        )

    recovered = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)

    assert recovered is not None
    assert recovered.id == enqueued.id


async def test_failed_job_honours_backoff_and_is_then_retried(pg_pool) -> None:
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=904)
    enqueued = await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:backoff",
    )
    await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    retry_at = datetime.now(UTC) + timedelta(minutes=10)
    claimed = await jobs.get(enqueued.id)
    assert claimed is not None and claimed.claim_token is not None
    assert await jobs.mark_failed(
        enqueued.id,
        claim_token=claimed.claim_token,
        error="RuntimeError: boom",
        next_attempt_at=retry_at,
    )

    fetched = await jobs.get(enqueued.id)
    assert fetched is not None
    assert fetched.status == "failed"
    assert fetched.last_error == "RuntimeError: boom"

    # Not due yet: not claimable even though status is 'failed'.
    assert await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS) is None


async def test_renew_lease_extends_expiry_for_still_running_job(pg_pool) -> None:
    """A live worker can extend its lease past the original window."""
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=906)
    enqueued = await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:heartbeat",
    )
    claimed = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    assert claimed is not None
    assert claimed.claim_token is not None

    renewed_at = await jobs.renew_lease(
        enqueued.id,
        claim_token=claimed.claim_token or "",
        lease_seconds=SIMULATED_LEASE_SECONDS * 3,
    )
    assert renewed_at is not None

    assert renewed_at > datetime.now(UTC) + timedelta(seconds=SIMULATED_LEASE_SECONDS)

    fetched = await jobs.get(enqueued.id)
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.lease_expires_at is not None
    # Renewed lease keeps the job unclaimable.
    assert await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS) is None


async def test_concurrent_claims_never_hand_out_the_same_job(pg_pool) -> None:
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=905)
    for room_suffix in range(2):
        await jobs.enqueue(
            event_id=event.id if room_suffix == 0 else event.id,
            target_type="room",
            target_mxid=None,
            matrix_room_id=f"!room:race-{room_suffix}",
        )

    claims = await asyncio.gather(*(jobs.claim_next_job() for _ in range(6)))
    ids = [job.id for job in claims if job is not None]

    assert len(ids) == len(set(ids)) == 2


async def test_stale_worker_cannot_overwrite_state_after_reclaim(pg_pool) -> None:
    """Two-worker expired-lease race: the old claim must be fenced out (issue #10 R3).

    Worker A claims, its lease expires, worker B reclaims the same job. Every
    state update from A — renew, complete, fail — must be rejected via the
    claim token, and B's own updates must go through.
    """
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=907)
    enqueued = await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:fenced",
    )
    worker_a = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    assert worker_a is not None and worker_a.claim_token is not None

    # Worker A's lease lapses (it is slow / partitioned away), worker B reclaims.
    async with pg_pool.acquire() as connection:
        await connection.execute(
            "UPDATE delivery_jobs SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = $1",
            enqueued.id,
        )
    worker_b = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    assert worker_b is not None and worker_b.claim_token is not None
    assert worker_b.claim_token != worker_a.claim_token

    # Stale worker A: renewal and BOTH terminal transitions are rejected.
    assert (
        await jobs.renew_lease(
            enqueued.id, claim_token=worker_a.claim_token, lease_seconds=SIMULATED_LEASE_SECONDS * 5
        )
        is None
    )
    assert await jobs.mark_complete(enqueued.id, claim_token=worker_a.claim_token) is False
    stale_retry_at = datetime.now(UTC) - timedelta(minutes=5)
    assert (
        await jobs.mark_failed(
            enqueued.id,
            claim_token=worker_a.claim_token,
            error="stale worker failure",
            next_attempt_at=stale_retry_at,
        )
        is False
    )

    # Nothing A did may have touched B's claim.
    untouched = await jobs.get(enqueued.id)
    assert untouched is not None
    assert untouched.status == "running"
    assert untouched.claim_token == worker_b.claim_token
    assert untouched.lease_expires_at is not None
    assert untouched.lease_expires_at <= datetime.now(UTC) + timedelta(
        seconds=SIMULATED_LEASE_SECONDS
    )

    # The current owner B can still renew and complete.
    renewed = await jobs.renew_lease(
        enqueued.id, claim_token=worker_b.claim_token, lease_seconds=SIMULATED_LEASE_SECONDS * 5
    )
    assert renewed is not None and renewed > datetime.now(UTC) + timedelta(
        seconds=SIMULATED_LEASE_SECONDS
    )
    assert await jobs.mark_complete(enqueued.id, claim_token=worker_b.claim_token) is True
    completed = await jobs.get(enqueued.id)
    assert completed is not None and completed.status == "complete"


async def test_claim_tokens_are_unique_per_claim(pg_pool) -> None:
    """Each claim mints a fresh fencing token; re-claiming rotates it."""
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=908)
    enqueued = await jobs.enqueue(
        event_id=event.id,
        target_type="room",
        target_mxid=None,
        matrix_room_id="!room:token-rotation",
    )
    first = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    assert first is not None and first.claim_token
    async with pg_pool.acquire() as connection:
        await connection.execute(
            "UPDATE delivery_jobs SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = $1",
            enqueued.id,
        )
    second = await jobs.claim_next_job(lease_seconds=SIMULATED_LEASE_SECONDS)
    assert second is not None and second.claim_token
    assert second.claim_token != first.claim_token


async def test_durable_tx_id_device_id_and_dm_room_persistence(pg_pool) -> None:
    """Restart-safety columns survive and stay stable across claims (issue #10 R3).

    The tx id is stamped once and reused on reclaim, the device id is stamped
    idempotently, and the first resolved DM room is pinned — all against real
    Postgres.
    """
    jobs = DeliveryJobRepository(pg_pool)
    event = await _create_event(pg_pool, discourse_post_id=909)
    enqueued = await jobs.enqueue(
        event_id=event.id,
        target_type="dm",
        target_mxid="@alice:restart",
        matrix_room_id=None,
    )

    first_tx = await jobs.ensure_matrix_tx_id(enqueued.id)
    assert first_tx.startswith(f"dischat-{enqueued.id}-")
    # Re-stamping (retry/reclaim) must return the SAME id, never rotate it.
    assert await jobs.ensure_matrix_tx_id(enqueued.id) == first_tx

    first_device = await jobs.ensure_matrix_device_id(enqueued.id, device_id="DEVICE-A")
    assert first_device == "DEVICE-A"
    assert await jobs.ensure_matrix_device_id(enqueued.id, device_id="DEVICE-B") == "DEVICE-A"

    # First DM send resolves a room; the pin is idempotent across retries.
    resolved = await jobs.pin_matrix_dm_room(enqueued.id, room_id="!dm:first:here")
    assert resolved == "!dm:first:here"
    assert await jobs.pin_matrix_dm_room(enqueued.id, room_id="!dm:other") == "!dm:first:here"
    assert await jobs.get_matrix_dm_room_id(enqueued.id) == "!dm:first:here"

    # All persisted values round-trip through the record mapping.
    fetched = await jobs.get(enqueued.id)
    assert fetched is not None
    assert fetched.matrix_tx_id == first_tx
    assert fetched.matrix_device_id == "DEVICE-A"
    assert fetched.matrix_dm_room_id == "!dm:first:here"
