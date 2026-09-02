import asyncio
from datetime import UTC, datetime, timedelta

from dischat.storage.repositories import PairingRateLimitRepository


async def test_concurrent_issuance_reservations_never_exceed_cap(pg_pool) -> None:
    limits = PairingRateLimitRepository(pg_pool)
    now = datetime.now(UTC)
    maximum = 3

    results = await asyncio.gather(
        *[
            limits.reserve_issuance(
                mxid="@parallel:aosus.org",
                discourse_username="target",
                now=now,
                window=timedelta(hours=1),
                max_issuances=maximum,
            )
            for _ in range(maximum + 2)
        ]
    )

    assert sum(retry_at is None for retry_at in results) == maximum
    assert sum(retry_at is not None for retry_at in results) == 2


async def test_atomic_reservation_respects_active_cooldown(pg_pool) -> None:
    limits = PairingRateLimitRepository(pg_pool)
    now = datetime.now(UTC)
    cooldown_until = now + timedelta(minutes=15)
    await limits.apply_cooldown(
        mxid="@cooldown:aosus.org",
        discourse_username=None,
        cooldown_until=cooldown_until,
        now=now,
    )

    retry_at = await limits.reserve_issuance(
        mxid="@cooldown:aosus.org",
        discourse_username="target",
        now=now,
        window=timedelta(hours=1),
        max_issuances=3,
    )

    assert retry_at == cooldown_until


async def test_reservation_prunes_stale_rate_limit_and_event_rows(pg_pool) -> None:
    limits = PairingRateLimitRepository(pg_pool)
    now = datetime.now(UTC)
    window = timedelta(hours=1)
    stale = now - window - timedelta(seconds=1)
    async with pg_pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO pairing_rate_limits
                (mxid, discourse_username, updated_at, window_started_at)
            VALUES ('@stale:aosus.org', 'old-target', $1, $1)
            """,
            stale,
        )
        await connection.execute(
            """
            INSERT INTO pairing_issuance_events (mxid, discourse_username, issued_at)
            VALUES ('@stale:aosus.org', 'old-target', $1)
            """,
            stale,
        )

    assert (
        await limits.reserve_issuance(
            mxid="@fresh:aosus.org",
            discourse_username="target",
            now=now,
            window=window,
            max_issuances=3,
        )
        is None
    )

    async with pg_pool.acquire() as connection:
        stale_limits = await connection.fetchval(
            "SELECT COUNT(*) FROM pairing_rate_limits WHERE mxid = '@stale:aosus.org'"
        )
        stale_events = await connection.fetchval(
            "SELECT COUNT(*) FROM pairing_issuance_events WHERE mxid = '@stale:aosus.org'"
        )
    assert stale_limits == 0
    assert stale_events == 0


async def test_issuance_counter_persists_across_rows_and_rolls_window(pg_pool) -> None:
    limits = PairingRateLimitRepository(pg_pool)
    now = datetime.now(UTC)

    first = await limits.record_issuance(
        mxid="@alice:aosus.org",
        discourse_username="bob",
        now=now,
        window=timedelta(hours=1),
    )
    second = await limits.record_issuance(
        mxid="@alice:aosus.org",
        discourse_username="bob",
        now=now + timedelta(seconds=1),
        window=timedelta(hours=1),
    )

    assert first.issuance_count == 1
    assert second.issuance_count == 2
    assert second.window_started_at == first.window_started_at

    # A different target username has its own bucket (scoping is case-sensitive
    # at the storage layer; the service lowercases the requested username before
    # recording, so /pair BoB and /pair bob share a bucket).
    other = await limits.get_state(mxid="@alice:aosus.org", discourse_username="carol")
    assert other is None


async def test_failed_attempts_accumulate_persistently(pg_pool) -> None:
    limits = PairingRateLimitRepository(pg_pool)
    now = datetime.now(UTC)

    state = None
    for _ in range(5):
        state = await limits.record_failure(
            mxid="@alice:aosus.org", discourse_username=None, now=now
        )
    assert state is not None

    assert state.failure_count == 5
    fetched = await limits.get_state(mxid="@alice:aosus.org", discourse_username=None)
    assert fetched is not None
    assert fetched.failure_count == 5


async def test_cooldown_survives_session_replacement(pg_pool) -> None:
    """Simulate the abuse path: cooldown set, then sessions replaced; it must persist."""
    limits = PairingRateLimitRepository(pg_pool)
    now = datetime.now(UTC)
    cooldown_until = now + timedelta(minutes=15)

    for _ in range(3):
        await limits.record_issuance(
            mxid="@mallory:aosus.org",
            discourse_username="victim",
            now=now,
            window=timedelta(hours=1),
        )
    await limits.apply_cooldown(
        mxid="@mallory:aosus.org",
        discourse_username="victim",
        cooldown_until=cooldown_until,
        now=now,
    )
    # New pairing session would DELETE from pairing_sessions, but rate-limit rows stay.
    await limits.record_failure(mxid="@mallory:aosus.org", discourse_username="victim", now=now)

    state = await limits.get_state(mxid="@mallory:aosus.org", discourse_username="victim")
    assert state is not None
    assert state.cooldown_until == cooldown_until
    assert state.issuance_count == 3
    assert state.failure_count == 1

    other_scope = await limits.get_state(mxid="@mallory:aosus.org", discourse_username=None)
    assert other_scope is None or other_scope.cooldown_until is None


async def test_apply_cooldown_resets_failure_count_when_requested(pg_pool) -> None:
    """Re-arming a cooldown clears the failure counter so the next cooldown
    requires a fresh threshold crossing."""
    limits = PairingRateLimitRepository(pg_pool)
    now = datetime.now(UTC)

    for _ in range(5):
        await limits.record_failure(mxid="@ivy:aosus.org", discourse_username=None, now=now)
    await limits.apply_cooldown(
        mxid="@ivy:aosus.org",
        discourse_username=None,
        cooldown_until=now + timedelta(minutes=15),
        now=now,
        reset_failure_count=True,
    )

    state = await limits.get_state(mxid="@ivy:aosus.org", discourse_username=None)
    assert state is not None
    assert state.cooldown_until == now + timedelta(minutes=15)
    assert state.failure_count == 0


async def test_window_reset_allows_new_issuances(pg_pool) -> None:
    limits = PairingRateLimitRepository(pg_pool)
    now = datetime.now(UTC)
    window = timedelta(hours=1)

    state = None
    for offset in range(4):
        state = await limits.record_issuance(
            mxid="@dave:aosus.org",
            discourse_username=None,
            now=now + timedelta(minutes=offset),
            window=window,
        )
    assert state is not None
    assert state.issuance_count == 4
    assert state.window_started_at == now

    after_window = await limits.record_issuance(
        mxid="@dave:aosus.org",
        discourse_username=None,
        now=now + window + timedelta(minutes=1),
        window=window,
    )
    assert after_window.issuance_count == 1
    assert after_window.window_started_at > now
