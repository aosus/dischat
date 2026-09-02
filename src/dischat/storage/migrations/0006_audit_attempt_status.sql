-- 0006_audit_attempt_status.sql
--
-- Makes the audit trail able to represent an attempt/outcome independently of
-- any later local persistence (delivery_messages.create_mapping):
--   * status tracks the attempt lifecycle:
--       - 'pending': the audit row was created BEFORE the external write; a
--         row still pending after the write means the external write may have
--         happened but the process crashed before its outcome was recorded
--         (for example between the Matrix send and the delivery mapping
--         insert).
--       - 'success' / 'failed': the outcome of the external write itself.
--   * action 'send_matrix_notice' covers Matrix notice writes (command
--     responses and permission/error replies), which are live external
--     writes like any other.
--
-- Existing rows keep their observed outcome via the backfill below.

ALTER TABLE audit_logs
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'success';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'audit_logs_status_not_null'
          AND conrelid = 'audit_logs'::regclass
    ) THEN
        ALTER TABLE audit_logs
            ADD CONSTRAINT audit_logs_status_not_null
            CHECK (status IN ('pending', 'success', 'failed'));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE indexname = 'audit_logs_status_created_at_idx'
    ) THEN
        -- Triage query: writes whose outcome was never resolved.
        CREATE INDEX audit_logs_status_created_at_idx
            ON audit_logs (status, created_at);
    END IF;
END $$;
