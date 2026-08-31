-- Delivery job leases (issue #10)
--
-- Workers claim delivery jobs by flipping them to 'running'. Before leases,
-- a crash left claimed jobs stuck 'running' forever because claims only
-- picked up 'pending'/'failed' rows. Add bounded lease columns:
--
--   claimed_at       when the current (or most recent) worker took the job
--   lease_expires_at when the lease lapses; expired 'running' jobs become
--                    claimable again by the next claim cycle
ALTER TABLE delivery_jobs ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ NULL;
ALTER TABLE delivery_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL;

-- Stranded rows from the pre-lease era: any job still 'running' was claimed by
-- a process that may be gone. Expire their leases immediately so the recovery
-- sweep requeues them after the upgrade (send-side dedup prevents duplicates).
UPDATE delivery_jobs SET lease_expires_at = NOW() WHERE status = 'running';

CREATE INDEX IF NOT EXISTS delivery_jobs_claim_scan_idx
ON delivery_jobs (status, next_attempt_at);
