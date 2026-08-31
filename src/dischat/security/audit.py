from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

# Canonical audit actions for every live external write path.
# One action per external write; failed attempts are recorded with the same
# action and success=False plus a secret-free error_message.
ACTION_PAIRING_PM = "create_pairing_pm"
ACTION_DISCOURSE_REPLY = "create_discourse_reply"
ACTION_ROOM_DELIVERY = "deliver_matrix_room_message"
ACTION_DM_DELIVERY = "deliver_matrix_dm_message"
ACTION_SEND_MATRIX_NOTICE = "send_matrix_notice"

LIVE_WRITE_PATHS: tuple[tuple[str, str], ...] = (
    (ACTION_PAIRING_PM, "Discourse"),
    (ACTION_DISCOURSE_REPLY, "Discourse"),
    (ACTION_ROOM_DELIVERY, "Matrix"),
    (ACTION_DM_DELIVERY, "Matrix"),
    (ACTION_SEND_MATRIX_NOTICE, "Matrix"),
)

# Attempt lifecycle statuses. The audit row is created (status='pending',
# success=None) BEFORE the external write is performed and its outcome is
# updated (status='success'/'failed', success=True/False) BEFORE any dependent
# local persistence (for example delivery_messages.create_mapping). A crash
# between the external write and the outcome update therefore still leaves an
# audit row, marked pending with success=NULL, proving the write attempt was
# in flight without reporting it as successful.
STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

_ERROR_MESSAGE_MAX_LENGTH = 200


@dataclass(slots=True, frozen=True)
class AuditEntry:
    action: str
    discourse_username_used: str
    # None only while status='pending' (attempt row written before the external
    # write); update_outcome always sets it to True/False alongside status.
    success: bool | None
    mxid: str | None = None
    platform: str | None = None
    discourse_user_id_used: int | None = None
    topic_id: int | None = None
    post_id: int | None = None
    matrix_room_id: str | None = None
    matrix_event_id: str | None = None
    error_message: str | None = None
    status: str = STATUS_SUCCESS


class AuditLogsRepo(Protocol):
    async def record(self, entry: AuditEntry) -> int | None: ...

    async def update_outcome(
        self,
        audit_log_id: int,
        *,
        success: bool,
        error_message: str | None,
        post_id: int | None = None,
        matrix_event_id: str | None = None,
        matrix_room_id: str | None = None,
    ) -> None: ...


class MissingAuditLoggerError(TypeError):
    """Raised when an audit logger is required but none was wired."""

    def __init__(self) -> None:
        super().__init__("audit_logs repository is required for live write paths")


class MissingAuditIdError(RuntimeError):
    """Raised when a live attempt row cannot be resolved to an audit id.

    Attempt-first auditing requires a durable row id: without one the
    external write could proceed with no way to ever record its outcome,
    so live paths must fail closed instead of writing unaudited.
    """

    def __init__(self) -> None:
        super().__init__("audit repository returned no audit id for live write attempt")


# Secrets that must never reach audit_logs.error_message, even when an
# exception embeds them (httpx/httpcore embed the full request URL in
# connect errors; Discourse/Matrix exceptions can echo request bodies).
_REDACTED = "[REDACTED]"

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token"
    r"|refresh[_-]?token|password|passwd|secret|token)\s*[=:]\s*[^\s'\",;)\]]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[^\s'\",;)\]]+")
_PAIRING_CODE = re.compile(r"(?i)pairing[_ -]?code\s*\S*")
# URL with embedded credentials (userinfo): redact the whole URL.
_URL_WITH_CREDENTIALS = re.compile(
    r"(?i)\bhttps?://[^\s/'\"\\)\]]*:[^\s/@'\"\\)\]]*@[^\s'\"\\)\]]*"
)
# Any other http(s) URL: keep scheme+host, redact path+query (paths can
# embed access tokens, queries embed credentials).
_URL = re.compile(r"(?i)(https?://[^/?\s'\"\\)\]]+)([^\s'\"\\)\]]*)")


def _redact_url(match: re.Match[str]) -> str:
    scheme_and_host, path_and_query = match.group(1), match.group(2)
    if path_and_query:
        return f"{scheme_and_host}/{_REDACTED}"
    return scheme_and_host


def _redact_secrets(text: str) -> str:
    text = _SECRET_ASSIGNMENT.sub(_REDACTED, text)
    text = _BEARER_TOKEN.sub(_REDACTED, text)
    text = _PAIRING_CODE.sub(_REDACTED, text)
    text = _URL_WITH_CREDENTIALS.sub(_REDACTED, text)
    text = _URL.sub(_redact_url, text)
    return text


def failure_reason(exc: BaseException) -> str:
    """Extract a stable, single-line, secret-free failure reason.

    Raw exception text is treated as untrusted: it is redacted for known
    secret-bearing shapes (tokens, keys, URLs with credentials, query
    strings, pairing codes) before the newline flattening and 200-character
    truncation. The exception class name is always preserved as the stable
    prefix so triage does not depend on exception text surviving redaction.
    """
    # Exception messages can echo arbitrary request bodies and credentials;
    # regex redaction can never prove such text safe. Persist only the stable
    # exception class. Full diagnostics remain in ephemeral application logs.
    return exc.__class__.__name__[:_ERROR_MESSAGE_MAX_LENGTH]


async def record_audit_entry(
    audit_logs: AuditLogsRepo | None,
    entry: AuditEntry,
    *,
    require_logger: bool = False,
) -> None:
    """Record one audit entry via the configured repository.

    audit_logs may be None only on internal plumbing paths (fakes, bootstrap);
    live write paths must pass require_logger=True so misconfiguration fails
    loudly instead of leaving writes unrecorded.
    """
    if audit_logs is None:
        if require_logger:
            raise MissingAuditLoggerError()
        return
    await audit_logs.record(entry)


async def record_audit_attempt(
    audit_logs: AuditLogsRepo | None,
    entry: AuditEntry,
    *,
    require_logger: bool = False,
) -> int | None:
    """Record the attempt row BEFORE the external write is performed.

    Returns the audit row id (when the repository can provide one) so the
    outcome can be updated after the write, independently of any later local
    persistence. The pending attempt row is persisted with success=None: if
    the process crashes after the external write but before
    update_audit_outcome, the row remains with status='pending' and
    success=NULL, so an unresolved attempt can never be misread as a
    successful write by legacy/operator queries on the boolean.

    audit_logs may be None only on internal plumbing paths (fakes, bootstrap);
    live write paths must pass require_logger=True so misconfiguration fails
    loudly before any external write happens unrecorded.

    A live attempt also fails closed when the repository persists the row but
    returns no id: without an id the outcome could never be updated, so the
    write must be refused (MissingAuditIdError) instead of proceeding with an
    attempt row that can never resolve. Non-required plumbing paths keep
    returning None.
    """
    if audit_logs is None:
        if require_logger:
            raise MissingAuditLoggerError()
        return None
    audit_log_id = await audit_logs.record(entry)
    if audit_log_id is None and require_logger:
        raise MissingAuditIdError()
    return audit_log_id


async def update_audit_outcome(
    audit_logs: AuditLogsRepo | None,
    audit_log_id: int | None,
    *,
    success: bool,
    error_message: str | None,
    post_id: int | None = None,
    matrix_event_id: str | None = None,
    matrix_room_id: str | None = None,
) -> None:
    """Update a pending attempt row with its final outcome.

    Called AFTER the external write and BEFORE any dependent local
    persistence (delivery mapping insert), so a crash between the write and
    the mapping still leaves the audit row resolvable: present with
    status='pending' and success=NULL. post_id / matrix_event_id record the
    external write's identifier when the target system returned one.
    """
    if audit_logs is None or audit_log_id is None:
        return
    await audit_logs.update_outcome(
        audit_log_id,
        success=success,
        error_message=error_message,
        post_id=post_id,
        matrix_event_id=matrix_event_id,
        matrix_room_id=matrix_room_id,
    )
