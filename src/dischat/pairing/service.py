from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dischat.i18n import translate
from dischat.pairing.codes import generate_code, hash_code, verify_code
from dischat.storage.repositories import PairingRateLimitState


@dataclass(slots=True)
class PairingSession:
    mxid: str
    discourse_username: str
    code_hash: str
    expires_at: datetime
    attempt_count: int = 0
    consumed_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def is_consumed(self) -> bool:
        return self.consumed_at is not None


@dataclass(slots=True, frozen=True)
class PairingResult:
    ok: bool
    message_key: str


@dataclass(slots=True, frozen=True)
class PairingRateLimitPolicy:
    """Durable, session-independent pairing abuse controls.

    All state lives in ``pairing_rate_limits`` (see migration 0006), so starting
    a new pairing session cannot reset it.
    """

    max_issuances_per_window: int = 3
    issuance_window: timedelta = timedelta(hours=1)
    max_failures: int = 5
    failure_cooldown: timedelta = timedelta(minutes=15)


DEFAULT_RATE_LIMIT_POLICY = PairingRateLimitPolicy()


@dataclass(slots=True, frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_at: datetime | None = None


def is_cooldown_active(state: PairingRateLimitState | None, *, now: datetime) -> bool:
    return state is not None and state.cooldown_until is not None and now < state.cooldown_until


def evaluate_issuance(
    state: PairingRateLimitState | None,
    *,
    policy: PairingRateLimitPolicy,
    now: datetime,
) -> RateLimitDecision:
    """Decide whether a new pairing code may be issued for this scope."""
    if is_cooldown_active(state, now=now):
        assert state is not None and state.cooldown_until is not None
        return RateLimitDecision(allowed=False, retry_at=state.cooldown_until)
    if state is not None and state.issuance_count >= policy.max_issuances_per_window:
        window_end = state.window_started_at + policy.issuance_window
        if now < window_end:
            return RateLimitDecision(allowed=False, retry_at=window_end)
    return RateLimitDecision(allowed=True)


def evaluate_verification(
    state: PairingRateLimitState | None,
    *,
    policy: PairingRateLimitPolicy,  # noqa: ARG001 kept for symmetric call sites
    now: datetime,
) -> RateLimitDecision:
    """Decide whether a code-verification attempt may proceed for this scope."""
    if is_cooldown_active(state, now=now):
        assert state is not None and state.cooldown_until is not None
        return RateLimitDecision(allowed=False, retry_at=state.cooldown_until)
    return RateLimitDecision(allowed=True)


def crosses_failure_threshold(
    state: PairingRateLimitState | None, *, policy: PairingRateLimitPolicy
) -> bool:
    return state is not None and state.failure_count >= policy.max_failures


def should_apply_cooldown(
    state: PairingRateLimitState | None,
    *,
    policy: PairingRateLimitPolicy,
    now: datetime,
) -> bool:
    """Whether crossing the failure threshold must (re)arm a cooldown.

    A cooldown is armed when the threshold is crossed and there is no *active*
    cooldown for this scope. An expired ``cooldown_until`` counts as no active
    cooldown, so the protection re-arms on the next threshold crossing instead
    of being disabled forever after the first cooldown. When a new cooldown is
    armed the failure counter is reset, so each cooldown window requires a
    fresh ``max_failures`` consecutive-ish failures.
    """
    if not crosses_failure_threshold(state, policy=policy):
        return False
    return not is_cooldown_active(state, now=now)


def remaining_seconds(retry_at: datetime, *, now: datetime) -> int:
    delta = (retry_at - now).total_seconds()
    return max(1, int(delta) + 1)


class PairingService:
    def __init__(
        self,
        ttl: timedelta = timedelta(minutes=10),
        max_attempts: int = 5,
        rate_limit_policy: PairingRateLimitPolicy = DEFAULT_RATE_LIMIT_POLICY,
    ) -> None:
        self._ttl = ttl
        self._max_attempts = max_attempts
        self.rate_limit_policy = rate_limit_policy

    def start_session(
        self, mxid: str, discourse_username: str, now: datetime | None = None
    ) -> tuple[PairingSession, str]:
        issued_at = now or datetime.now(UTC)
        raw_code = generate_code()
        session = PairingSession(
            mxid=mxid,
            discourse_username=discourse_username,
            code_hash=hash_code(raw_code),
            expires_at=issued_at + self._ttl,
        )
        return session, raw_code

    def validate_code(
        self, session: PairingSession, code: str, now: datetime | None = None
    ) -> PairingResult:
        checked_at = now or datetime.now(UTC)
        if session.is_consumed() or session.is_expired(checked_at):
            return PairingResult(ok=False, message_key="pairing.invalid_code")
        session.attempt_count += 1
        if session.attempt_count > self._max_attempts:
            return PairingResult(ok=False, message_key="pairing.invalid_code")
        if not code.isdigit() or len(code) != 6:
            return PairingResult(ok=False, message_key="pairing.prompt_code")
        if not verify_code(code, session.code_hash):
            return PairingResult(ok=False, message_key="pairing.invalid_code")
        session.consumed_at = checked_at
        return PairingResult(ok=True, message_key="pairing.success")

    def render_message(self, result: PairingResult, locale: str) -> str:
        return translate(result.message_key, locale)
