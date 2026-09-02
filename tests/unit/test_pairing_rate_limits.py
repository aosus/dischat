from datetime import UTC, datetime, timedelta

from dischat.pairing.service import (
    PairingRateLimitPolicy,
    crosses_failure_threshold,
    evaluate_issuance,
    evaluate_verification,
    is_cooldown_active,
    should_apply_cooldown,
)
from dischat.storage.repositories import PairingRateLimitState

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_state(
    *,
    issuance_count: int = 0,
    failure_count: int = 0,
    window_started_at: datetime | None = None,
    cooldown_until: datetime | None = None,
) -> PairingRateLimitState:
    return PairingRateLimitState(
        mxid="@alice:aosus.org",
        discourse_username="alice",
        issuance_count=issuance_count,
        failure_count=failure_count,
        window_started_at=window_started_at or NOW,
        cooldown_until=cooldown_until,
    )


def test_issuance_allowed_when_no_state() -> None:
    decision = evaluate_issuance(None, policy=PairingRateLimitPolicy(), now=NOW)
    assert decision.allowed is True
    assert decision.retry_at is None


def test_issuance_blocked_inside_window() -> None:
    state = make_state(issuance_count=3, window_started_at=NOW - timedelta(minutes=10))
    decision = evaluate_issuance(state, policy=PairingRateLimitPolicy(), now=NOW)

    assert decision.allowed is False
    assert decision.retry_at == NOW + timedelta(minutes=50)


def test_issuance_allowed_again_after_window_rolls_over() -> None:
    policy = PairingRateLimitPolicy()
    state = make_state(
        issuance_count=5,
        window_started_at=NOW - policy.issuance_window - timedelta(seconds=1),
    )
    decision = evaluate_issuance(state, policy=policy, now=NOW)

    assert decision.allowed is True


def test_active_cooldown_blocks_issuance_even_in_fresh_window() -> None:
    cooldown_until = NOW + timedelta(minutes=15)
    state = make_state(cooldown_until=cooldown_until)
    decision = evaluate_issuance(state, policy=PairingRateLimitPolicy(), now=NOW)

    assert decision.allowed is False
    assert decision.retry_at == cooldown_until


def test_expired_cooldown_no_longer_blocks() -> None:
    state = make_state(cooldown_until=NOW - timedelta(seconds=1))
    assert is_cooldown_active(state, now=NOW) is False


def test_verification_allowed_without_state_and_unaffected_by_counters() -> None:
    policy = PairingRateLimitPolicy()
    state = make_state(issuance_count=99, window_started_at=NOW - timedelta(minutes=1))
    decision = evaluate_verification(state, policy=policy, now=NOW)

    assert decision.allowed is True


def test_verification_blocked_by_active_cooldown() -> None:
    cooldown_until = NOW + timedelta(minutes=10)
    state = make_state(cooldown_until=cooldown_until)
    decision = evaluate_verification(state, policy=PairingRateLimitPolicy(), now=NOW)

    assert decision.allowed is False
    assert decision.retry_at == cooldown_until


def test_crosses_failure_threshold_requires_max_failures() -> None:
    policy = PairingRateLimitPolicy(max_failures=5)
    below = make_state(failure_count=4)
    at_limit = make_state(failure_count=5)

    assert crosses_failure_threshold(below, policy=policy) is False
    assert crosses_failure_threshold(at_limit, policy=policy) is True


def test_crosses_failure_threshold_false_without_state() -> None:
    assert crosses_failure_threshold(None, policy=PairingRateLimitPolicy()) is False


def test_should_apply_cooldown_when_threshold_crossed_without_cooldown() -> None:
    policy = PairingRateLimitPolicy()
    state = make_state(failure_count=policy.max_failures)
    assert should_apply_cooldown(state, policy=policy, now=NOW) is True


def test_should_not_apply_cooldown_while_active_cooldown_in_progress() -> None:
    policy = PairingRateLimitPolicy()
    state = make_state(
        failure_count=policy.max_failures, cooldown_until=NOW + timedelta(minutes=15)
    )
    assert should_apply_cooldown(state, policy=policy, now=NOW) is False


def test_should_rearm_cooldown_after_previous_cooldown_expired() -> None:
    policy = PairingRateLimitPolicy()
    state = make_state(failure_count=policy.max_failures, cooldown_until=NOW - timedelta(seconds=1))
    assert should_apply_cooldown(state, policy=policy, now=NOW) is True


def test_should_apply_cooldown_false_below_threshold_or_without_state() -> None:
    policy = PairingRateLimitPolicy()
    assert should_apply_cooldown(make_state(failure_count=1), policy=policy, now=NOW) is False
    assert should_apply_cooldown(None, policy=policy, now=NOW) is False
