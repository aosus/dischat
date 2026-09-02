UPDATE audit_logs
SET status = CASE
    WHEN success IS TRUE THEN 'success'
    WHEN success IS FALSE THEN 'failed'
    ELSE 'pending'
END
WHERE status IS DISTINCT FROM CASE
    WHEN success IS TRUE THEN 'success'
    WHEN success IS FALSE THEN 'failed'
    ELSE 'pending'
END;

ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS audit_logs_outcome_consistent;
ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_outcome_consistent CHECK (
    (status = 'pending' AND success IS NULL)
    OR (status = 'success' AND success IS TRUE)
    OR (status = 'failed' AND success IS FALSE)
) NOT VALID;
ALTER TABLE audit_logs VALIDATE CONSTRAINT audit_logs_outcome_consistent;
