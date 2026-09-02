-- Exclusive lease ownership for the Matrix event ledger (issue #9 review).
--
-- The previous adoption primitive UPDATE ... WHERE status = 'claimed'
-- re-returned the row to every concurrent caller while leaving it 'claimed',
-- so two replays (or a replay racing the live worker) could both proceed to
-- the external Discourse write; the write-outcome ledger only picked a winner
-- afterwards, when the duplicate side effect had already happened.
--
-- Ownership is now explicit:
--   - lease_owner  TEXT : a per-attempt random token; NULL for pre-lease rows.
--   - lease_expires_at   : when the lease lapses and takeover becomes legal.
-- Adoption is an atomic transition to a distinct 'owned' status guarded by the
-- owner token, so exactly one concurrent attempt can hold the fence.
--
-- The 'claimed' status is kept (still set by claim_event) so pre-lease
-- databases migrate in place: rows already 'claimed' by an older deployment
-- have NULL lease_owner/lease_expires_at, so adopt_event adopts them
-- immediately (lease_expires_at IS NULL is an adoptable condition) with no
-- takeover window. During a rolling upgrade an old-version worker can
-- therefore still be mid-write when a new-version worker adopts its marker
-- and writes again — see the operations documentation for the upgrade
-- guidance.

ALTER TABLE matrix_event_state ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE matrix_event_state ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
