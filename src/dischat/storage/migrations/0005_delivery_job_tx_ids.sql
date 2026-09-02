-- Durable per-job Matrix transaction id (issue #10, post-send/pre-persist window).
--
-- The Matrix spec deduplicates sends by (device, transaction_id): retrying a send
-- with the SAME tx id cannot create a second event even if the first attempt was
-- already accepted by the homeserver. A transaction id that is generated fresh per
-- attempt (nio's default) does not help across process crashes or lease reclaims,
-- so we persist one per job row BEFORE the Matrix write. Any later attempt of the
-- same job reuses it, making the send itself idempotent across recovery.
ALTER TABLE delivery_jobs ADD COLUMN IF NOT EXISTS matrix_tx_id TEXT NULL;

-- A unique index guarantees that once a tx id has been stamped on a job it can
-- never be handed to a different job (and lets claim_next_job reuse the existing
-- stamp on reclaim instead of rotating it).
CREATE UNIQUE INDEX IF NOT EXISTS delivery_jobs_matrix_tx_id_key
ON delivery_jobs (matrix_tx_id)
WHERE matrix_tx_id IS NOT NULL;
