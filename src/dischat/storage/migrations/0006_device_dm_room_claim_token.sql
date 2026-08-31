-- Restart-safe Matrix identity and lease fencing (issue #10, review round 3).
--
-- 1. matrix_device_id: Matrix transaction ids are scoped to (device, endpoint).
--    A password re-login without a stable device_id mints a NEW device on every
--    restart, so a job's persisted matrix_tx_id would no longer deduplicate.
--    We persist the bot's device id and pass it to every login so the tx id
--    stays valid across restarts. The bot typically logs in with an access
--    token (no device churn); this covers the supported password-auth path.
-- 2. matrix_dm_room_id: send_dm() previously re-ran ensure_dm_room() on each
--    attempt. If a retry picked (or created) a different DM room, the same tx
--    id would go to a different /rooms/{roomId}/send endpoint and could not
--    deduplicate. We persist the resolved DM room after the first attempt and
--    pin every retry to it.
ALTER TABLE delivery_jobs ADD COLUMN IF NOT EXISTS matrix_device_id TEXT NULL;
ALTER TABLE delivery_jobs ADD COLUMN IF NOT EXISTS matrix_dm_room_id TEXT NULL;

-- 3. claim_token: fencing token minted on every claim. Renewal and terminal
--    updates must be conditional on it (compare-and-set), so a slow worker
--    whose lease expired and whose job was reclaimed cannot overwrite the
--    newer claim's state.
ALTER TABLE delivery_jobs ADD COLUMN IF NOT EXISTS claim_token TEXT NULL;
