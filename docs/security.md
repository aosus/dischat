# Security

Security rules implemented in this baseline:

- pairing codes are hashed
- pairing codes expire
- pairing codes are single-use
- raw pairing codes are not stored in the database model
- posting identity must come from pairing or relay configuration
- live test category checks fail closed for unexpected category IDs
- public-category enforcement gates polling and routing (see below)

## Public category enforcement

The Discourse client uses an admin-operated API key, so the poller can observe activity
(read-restricted topics, private categories) that anonymous users cannot see. Dischat
therefore never trusts what the Discourse API returns when deciding what to bridge.
Visibility is decided solely by each category's bootstrap record (`is_public`, `enabled`)
stored in the `categories` table during startup sync.

Enforcement happens at two layers:

1. **Polling boundary** (`src/dischat/discourse/sync.py`). Before an event is stored or a
   delivery job is enqueued, `poll_once` resolves the post's `category_id` against the
   bootstrap record and skips the post entirely when:
   - the category has no bootstrap record (unknown), or
   - the record is disabled (`enabled = FALSE`), or
   - the record is not public (`is_public = FALSE`).

   Skipped posts never become discourse events, room deliveries, DMs, threaded replies,
   direct-reply or mention notifications.

2. **Repository / query boundary** (`src/dischat/storage/repositories.py`). The watch and
   room-link matching queries enforce the same flags as defense in depth, so the
   `all_public_categories` watch mode and `include_all_public_categories` room links can
   only ever match categories whose bootstrap record is both `is_public = TRUE` and
   `enabled = TRUE`. A specific `category` watch cannot deliver content from a category
   that later becomes read-restricted either: `UserWatchRepository.list_mxids_for_category`
   re-checks the flags at query time.

**Live-E2E exception.** Live end-to-end testing targets a single read-restricted test
category (`DISCOURSE_TEST_CATEGORY_ID`, asserted to be id 56 by
`src/dischat/testing/live.py`). That exception is isolated to live-E2E mode: it applies
only when `live_e2e_category_id` is configured, and only for that exact category ID. In
that mode the poller passes an explicit include-non-public flag down to the router, which
the repository queries honor for that category alone. Production deployments must leave
`DISCOURSE_TEST_CATEGORY_ID` unset, in which case no non-public category can ever be
delivered to Matrix.

## Visibility is revalidated before every poll

The `categories` snapshot is the poller's visibility source of truth, so
`refresh_category_visibility` (`src/dischat/main.py`) revalidates it against the
Discourse category listing **before every production poll** — not on a timer. There is
deliberately no cadence knob: any interval between the snapshot and the poll would be a
window in which a stored `is_public=TRUE` flag is an unverified claim, so a category an
admin just made read-restricted could keep being routed until the next scheduled
refresh. Revalidating per poll closes that window entirely: a visibility change is
picked up by the very next poll (bounded by `POLL_INTERVAL_SECONDS`, which also paces
the category listing call), and a newly public category opens up just as fast.

Because the visibility revalidation is the only thing that keeps the stored
`is_public`/`enabled` flags honest — and because the Discourse client's admin API key
means the post feed can keep working even when the category listing does not — a failed
revalidation must never leave the last-known snapshot in charge:

- When the revalidation fails, the poller state is marked `visibility_stale` and
  **polling is suspended entirely** until a refresh succeeds. `run_iteration` passes the
  stale flag into `poll_once`, which skips the whole Discourse poll (nothing is read, no
  events are created, no delivery jobs are enqueued, and `last_seen_post_id` does not
  advance). This closes both the `public -> private` + refresh-outage window and the
  quieter `public -> private` between two successful refreshes: no poll ever runs
  against an unverified snapshot.
- While stale, the revalidation is retried on **every** iteration, so the outage window
  is as short as the category listing allows. A successful refresh revalidates the
  snapshot and clears the flag.
- The stale-snapshot gate also lives inside `poll_once` itself (defense in depth), so
  any direct caller passing a stale state gets the same fail-closed behavior.
- Posts skipped while a category was private are re-evaluated if it later becomes
  public again (they never advanced `last_seen_post_id`); Matrix sync/commands and
  delivery of already-enqueued jobs are unaffected by the suspension. The live-E2E test
  category is exempt, since it never consults the visibility snapshot.

## Still to complete in runtime code

- persistent pairing attempt rate limits
- audit persistence for every live write path

Deployment secrets hygiene:

- the example `dischat/dischat` database credentials in `docker-compose.yml` are development-only; set a strong `POSTGRES_PASSWORD` in `.env` (git-ignored) before any real deployment
- the bundled PostgreSQL data volume (`postgres-data`) is the authoritative store for pairings, watches, room links, queued deliveries, and audit records — include it in your backup plan (see [Docker: Data persistence & backups](docker.md#data-persistence-backups))
