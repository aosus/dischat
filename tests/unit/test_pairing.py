from datetime import UTC, datetime, timedelta

from dischat.pairing.service import PairingService


def test_pairing_session_accepts_valid_code() -> None:
    service = PairingService(ttl=timedelta(minutes=5))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session, raw_code = service.start_session("@alice:aosus.org", "alice", now)

    result = service.validate_code(session, raw_code, now + timedelta(seconds=1))

    assert result.ok is True
    assert result.message_key == "pairing.success"
    assert session.consumed_at is not None


def test_pairing_session_rejects_non_digit_code_with_prompt() -> None:
    service = PairingService()
    session, _ = service.start_session("@alice:aosus.org", "alice")

    result = service.validate_code(session, "nope")

    assert result.ok is False
    assert result.message_key == "pairing.prompt_code"


def test_pairing_session_rejects_expired_code() -> None:
    service = PairingService(ttl=timedelta(seconds=1))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session, raw_code = service.start_session("@alice:aosus.org", "alice", now)

    result = service.validate_code(session, raw_code, now + timedelta(seconds=2))

    assert result.ok is False
    assert result.message_key == "pairing.invalid_code"
