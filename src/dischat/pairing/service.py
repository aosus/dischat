from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dischat.i18n import translate
from dischat.pairing.codes import generate_code, hash_code, verify_code


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


class PairingService:
    def __init__(self, ttl: timedelta = timedelta(minutes=10), max_attempts: int = 5) -> None:
        self._ttl = ttl
        self._max_attempts = max_attempts

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
