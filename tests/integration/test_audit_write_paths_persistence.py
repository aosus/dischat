from __future__ import annotations

from pathlib import Path

import asyncpg

from dischat.jobs.workers import SYSTEM_ACTOR
from dischat.security.audit import (
    ACTION_DISCOURSE_REPLY,
    ACTION_DM_DELIVERY,
    ACTION_PAIRING_PM,
    ACTION_ROOM_DELIVERY,
    AuditEntry,
)
from dischat.storage.repositories import AuditLogRepository


async def test_all_live_write_path_actions_persist(pg_pool) -> None:
    audit = AuditLogRepository(pg_pool)

    entries = [
        AuditEntry(
            action=ACTION_PAIRING_PM,
            mxid="@alice:aosus.org",
            platform="matrix",
            discourse_username_used="target_user",
            success=True,
        ),
        AuditEntry(
            action=ACTION_PAIRING_PM,
            mxid="@alice:aosus.org",
            platform="matrix",
            discourse_username_used="target_user",
            success=False,
            error_message="discourse 403: forbidden recipients",
        ),
        AuditEntry(
            action=ACTION_DISCOURSE_REPLY,
            mxid="@alice:aosus.org",
            platform="matrix",
            discourse_username_used="alice",
            topic_id=20,
            post_id=99,
            matrix_room_id="!room:test",
            matrix_event_id="$event",
            success=True,
        ),
        AuditEntry(
            action=ACTION_ROOM_DELIVERY,
            discourse_username_used=SYSTEM_ACTOR,
            mxid=SYSTEM_ACTOR,
            platform="system",
            topic_id=20,
            post_id=31,
            matrix_room_id="!room:test",
            matrix_event_id="$text1",
            success=True,
        ),
        AuditEntry(
            action=ACTION_DM_DELIVERY,
            discourse_username_used=SYSTEM_ACTOR,
            mxid="@bob:aosus.org",
            platform="system",
            topic_id=20,
            post_id=31,
            matrix_room_id="!dm:bob",
            matrix_event_id="$dm1",
            success=False,
            error_message="missing_dm_room_id",
        ),
    ]
    for entry in entries:
        await audit.record(entry)

    async with pg_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT action, success, error_message, topic_id
            FROM audit_logs
            ORDER BY id
            """
        )

    assert [row["action"] for row in rows] == [
        "create_pairing_pm",
        "create_pairing_pm",
        "create_discourse_reply",
        "deliver_matrix_room_message",
        "deliver_matrix_dm_message",
    ]
    assert [row["success"] for row in rows] == [
        True,
        False,
        True,
        True,
        False,
    ]
    assert rows[1]["error_message"] == "discourse 403: forbidden recipients"
    assert rows[4]["error_message"] == "missing_dm_room_id"


async def test_migration_indexes_and_constraint_exist(pg_pool) -> None:
    async with pg_pool.acquire() as connection:
        index_names = {
            row["indexname"]
            for row in await connection.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_logs'"
            )
        }
        constraint_names = {
            row["conname"]
            for row in await connection.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'audit_logs'::regclass"
            )
        }

    assert "audit_logs_created_at_idx" in index_names
    assert "audit_logs_success_created_at_idx" in index_names
    assert "audit_logs_action_not_null" in constraint_names


async def test_failed_audit_entries_are_queryable_for_triage(pg_pool) -> None:
    audit = AuditLogRepository(pg_pool)
    await audit.record(
        AuditEntry(
            action=ACTION_DISCOURSE_REPLY,
            mxid="@alice:aosus.org",
            platform="matrix",
            discourse_username_used="alice",
            topic_id=20,
            matrix_room_id="!room:test",
            matrix_event_id="$event",
            success=False,
            error_message="HTTP 429 rate limited",
        )
    )
    await audit.record(
        AuditEntry(
            action=ACTION_DISCOURSE_REPLY,
            mxid="@alice:aosus.org",
            platform="matrix",
            discourse_username_used="alice",
            topic_id=20,
            post_id=42,
            matrix_room_id="!room:test",
            matrix_event_id="$event2",
            success=True,
        )
    )

    async with pg_pool.acquire() as connection:
        failed_count = await connection.fetchval(
            "SELECT COUNT(*) FROM audit_logs WHERE success = FALSE AND error_message IS NOT NULL"
        )
        success_row = await connection.fetchrow(
            "SELECT topic_id, post_id FROM audit_logs WHERE success = TRUE"
        )

    assert failed_count == 1
    assert success_row is not None
    assert success_row["topic_id"] == 20
    assert success_row["post_id"] == 42


async def test_empty_action_is_rejected_by_migration_check(pg_pool) -> None:
    async with pg_pool.acquire() as connection:
        try:
            await connection.execute(
                """
                INSERT INTO audit_logs (action, discourse_username_used, success)
                VALUES ('', 'user', TRUE)
                """
            )
        except asyncpg.exceptions.CheckViolationError:
            pass
        else:
            raise AssertionError("empty action should violate audit_logs_action_not_null")


async def test_pending_attempt_row_persists_with_null_success(pg_pool) -> None:
    """The attempt-first pending row is durable with success=NULL: a crash
    before update_outcome leaves an unresolved row, not a success=TRUE one."""
    audit = AuditLogRepository(pg_pool)
    audit_id = await audit.record(
        AuditEntry(
            action=ACTION_ROOM_DELIVERY,
            discourse_username_used=SYSTEM_ACTOR,
            success=None,
            status="pending",
        )
    )
    assert audit_id is not None

    async with pg_pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT success, status FROM audit_logs WHERE id = $1", audit_id
        )

    assert row is not None
    assert row["status"] == "pending"
    assert row["success"] is None


async def test_pending_row_not_counted_as_success_by_legacy_boolean_queries(pg_pool) -> None:
    """Operator triage that classifies by the legacy boolean must never count
    an unresolved (pending, success=NULL) attempt as a delivered write."""
    audit = AuditLogRepository(pg_pool)
    await audit.record(
        AuditEntry(
            action=ACTION_ROOM_DELIVERY,
            discourse_username_used=SYSTEM_ACTOR,
            success=None,
            status="pending",
        )
    )
    await audit.record(
        AuditEntry(
            action=ACTION_ROOM_DELIVERY,
            discourse_username_used=SYSTEM_ACTOR,
            success=True,
            status="success",
        )
    )
    await audit.record(
        AuditEntry(
            action=ACTION_ROOM_DELIVERY,
            discourse_username_used=SYSTEM_ACTOR,
            success=False,
            status="failed",
            error_message="matrix 429",
        )
    )

    async with pg_pool.acquire() as connection:
        pending_claiming_success = await connection.fetchval(
            "SELECT COUNT(*) FROM audit_logs WHERE status = 'pending' AND success = TRUE"
        )
        resolved_success = await connection.fetchval(
            "SELECT COUNT(*) FROM audit_logs WHERE status <> 'pending' AND success = TRUE"
        )

    assert pending_claiming_success == 0
    assert resolved_success == 1


async def test_migration_0007_reclassifies_stale_pending_success_rows(pg_pool) -> None:
    """Re-running 0007's backfill turns pre-fix durable rows (status='pending'
    AND success=TRUE) into unresolved (success=NULL) rows."""
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "dischat"
        / "storage"
        / "migrations"
        / "0007_audit_pending_success_nullable.sql"
    )
    async with pg_pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO audit_logs (action, discourse_username_used, success, status)
            VALUES ('deliver_matrix_room_message', 'system', TRUE, 'pending')
            """
        )

        await connection.execute(migration_path.read_text(encoding="utf-8"))

        row = await connection.fetchrow(
            """
            SELECT success, status
            FROM audit_logs
            WHERE status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """
        )

    assert row is not None
    assert row["status"] == "pending"
    assert row["success"] is None
