-- 0007_audit_pending_success_nullable.sql
--
-- Makes the legacy `success` boolean tri-state while an attempt is pending.
--
-- Problem: attempt-first call sites wrote their pending AuditEntry with
-- success=TRUE (the column was BOOLEAN NOT NULL), so a crash between the
-- external write and update_outcome left a durable row with
-- status='pending' AND success=TRUE. Legacy/operator queries that classify
-- rows by `success` (for example
-- `SELECT ... WHERE success = TRUE` / `success = FALSE`) then reported an
-- unresolved attempt as a successful write.
--
-- Fix: `success` becomes nullable and pending attempt rows are written with
-- success=NULL:
--   * status='pending', success=NULL  -> attempt in flight / crash before the
--     outcome update; NOT a successful write.
--   * status='success', success=TRUE  -> the external write happened.
--   * status='failed',  success=FALSE -> the external write was refused.
-- update_outcome always sets status and success together, so `success` is
-- non-NULL exactly when the outcome is resolved, and `status` remains
-- authoritative for the lifecycle.

-- Stale rows from before this fix: a pending row claiming success=TRUE was
-- never actually resolved, so reclassify it as unknown outcome (NULL).
UPDATE audit_logs
SET success = NULL
WHERE status = 'pending' AND success IS TRUE;

-- success is tri-state while pending, so the NOT NULL constraint must go.
ALTER TABLE audit_logs ALTER COLUMN success DROP NOT NULL;
