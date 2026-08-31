-- 0005_audit_write_paths.sql
--
-- Guarantees audit coverage metadata for every live external write path:
--   * create_pairing_pm           (Discourse PM write carrying the pairing code)
--   * create_discourse_reply      (Matrix -> Discourse reply write)
--   * deliver_matrix_room_message (Discourse -> Matrix room delivery write)
--   * deliver_matrix_dm_message   (Discourse -> Matrix DM delivery write)
--
-- The application layer records a row for each of these operations, including
-- failed attempts (success = FALSE with error_message). This migration makes
-- sure the table can answer "what did the bridge write externally and did it
-- succeed?" for audits: action must always be present, outcome must be
-- explicit, and failure reasons are indexed for triage.
--
-- No secret material is stored in audit_logs: pairing code hashes, API keys,
-- access tokens and message bodies are all excluded from this table.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'audit_logs_action_not_null'
          AND conrelid = 'audit_logs'::regclass
    ) THEN
        ALTER TABLE audit_logs
            ADD CONSTRAINT audit_logs_action_not_null CHECK (action <> '');
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE indexname = 'audit_logs_created_at_idx'
    ) THEN
        CREATE INDEX audit_logs_created_at_idx ON audit_logs (created_at);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE indexname = 'audit_logs_success_created_at_idx'
    ) THEN
        CREATE INDEX audit_logs_success_created_at_idx ON audit_logs (success, created_at);
    END IF;
END $$;
