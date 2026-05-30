CREATE UNIQUE INDEX IF NOT EXISTS user_watches_category_unique_idx
ON user_watches (mxid, mode, category_id)
WHERE category_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS user_watches_all_unique_idx
ON user_watches (mxid, mode)
WHERE category_id IS NULL;
