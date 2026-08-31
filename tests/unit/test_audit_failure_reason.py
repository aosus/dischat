"""Tests for failure_reason(): exception text must never reach audit_logs
with secret material intact (round-4 coderabbit finding on docs/security.md)."""

from __future__ import annotations

from dischat.security.audit import failure_reason


def test_preserves_class_name_and_message() -> None:
    assert failure_reason(RuntimeError("Matrix homeserver refused the send")) == (
        "RuntimeError: Matrix homeserver refused the send"
    )


def test_uses_class_name_when_message_is_empty() -> None:
    assert failure_reason(RuntimeError()) == "RuntimeError"


def test_redacts_api_key_assignment() -> None:
    exc = RuntimeError("Discourse rejected Api-Key=super-secret-key request")
    reason = failure_reason(exc)
    assert "super-secret-key" not in reason
    assert "[REDACTED]" in reason


def test_redacts_token_password_and_secret_assignments() -> None:
    for secret in (
        "access_token: abc123",
        "password=hunter2",
        "token=eyABC.def",
        "api_secret=xyz",
    ):
        reason = failure_reason(RuntimeError(f"failed with {secret} in flight"))
        assert "abc123" not in reason
        assert "hunter2" not in reason
        assert "eyABC" not in reason
        assert "xyz" not in reason
        assert "[REDACTED]" in reason


def test_redacts_bearer_tokens() -> None:
    reason = failure_reason(RuntimeError("auth failed: Bearer eyJhbGciOiTokenValue"))
    assert "eyJhbGciOiTokenValue" not in reason
    assert "[REDACTED]" in reason


def test_redacts_pairing_codes() -> None:
    reason = failure_reason(RuntimeError("pairing code 123456 rejected for target_user"))
    assert "123456" not in reason
    assert "[REDACTED]" in reason


def test_redacts_url_path_and_query_but_keeps_host() -> None:
    reason = failure_reason(
        RuntimeError("connect failed for https://aosus.org/posts.json?api_key=k123")
    )
    assert "https://aosus.org" in reason
    assert "posts.json" not in reason
    assert "api_key" not in reason
    assert "k123" not in reason


def test_redacts_urls_with_embedded_credentials() -> None:
    reason = failure_reason(RuntimeError("cannot reach https://user:pass@matrix.aosus.org/_matrix"))
    assert "user:pass" not in reason
    assert "matrix.aosus.org" not in reason
    assert "[REDACTED]" in reason


def test_flattens_newlines_to_single_line() -> None:
    reason = failure_reason(RuntimeError("line one\nline two\nline three"))
    assert "\n" not in reason
    assert "line one line two line three" in reason


def test_truncates_to_200_characters() -> None:
    reason = failure_reason(RuntimeError("x" * 500))
    assert len(reason) <= 200


def test_truncation_applies_after_redaction() -> None:
    # The secret sits beyond the 200th character: redaction must still remove
    # it before truncation so it cannot be re-exposed by the cut.
    exc = RuntimeError("prefix " + "y" * 190 + " token=leaky-secret-value")
    reason = failure_reason(exc)
    assert "leaky-secret-value" not in reason
    assert len(reason) <= 200
