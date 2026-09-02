-- Reconciliation support for the Matrix event ledger (issue #9 review).
--
-- A bare 'claimed'/'processed' pair cannot tell whether the external
-- Discourse write already happened when a process dies mid-flight. The
-- ledger therefore records the write outcome (Discourse topic/post ids) in
-- a 'written' status as soon as the external write returns, so a replay can
-- adopt the already-created post instead of writing a duplicate.
--
-- response_notice stores the chat notice a fenced command delivery still
-- owes the room after the external pairing PM was created, so a replay of a
-- 'written' command event can finish the delivery without re-running the
-- command (which would send a second Discourse PM).

ALTER TABLE matrix_event_state ADD COLUMN IF NOT EXISTS discourse_topic_id BIGINT;
ALTER TABLE matrix_event_state ADD COLUMN IF NOT EXISTS discourse_post_id BIGINT;
ALTER TABLE matrix_event_state ADD COLUMN IF NOT EXISTS response_notice TEXT;
