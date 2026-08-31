-- Retention support for the Matrix event ledger (issue #9 review).
--
-- matrix_event_state stores one row per inbound side-effecting Matrix event.
-- Retention only needs to keep 'processed' rows long enough that a /sync
-- replay cannot re-deliver an event, which is bounded by the stored sync
-- token horizon (the bridge never re-reads events older than that token), so
-- old confirmed rows are safe to delete. Rows in 'claimed', 'owned', or
-- 'written' states are never touched: deleting one could re-open an external
-- write. The runtime prunes expired 'processed' rows on every iteration
-- (prune_processed_events); these statements make that prune cheap and index
-- the same predicate for ad-hoc administration.

-- Support the retention predicate: finding expired 'processed' rows must not
-- degrade into a full scan as the ledger grows.
CREATE INDEX IF NOT EXISTS matrix_event_state_processed_updated_at_idx
ON matrix_event_state (updated_at)
WHERE status = 'processed';

-- The retention prune and the claim-fence queries all resolve rows by the
-- (room_id, event_id) unique constraint or by this partial index, so the
-- old full-table status index has no remaining consumer; drop it to save
-- write amplification on every ledger update.
DROP INDEX IF EXISTS matrix_event_state_status_idx;
