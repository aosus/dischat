# Security

Security rules implemented in this baseline:

- pairing codes are hashed
- pairing codes expire
- pairing codes are single-use
- raw pairing codes are not stored in the database model
- pairing-code issuance is rate limited persistently per Matrix user and per
  requested Discourse username (default: 3 issuances per rolling 1-hour window,
  stored server-side so starting a new session cannot reset it)
- failed code-verification attempts are counted persistently per Matrix user
  and per requested Discourse username across sessions; reaching the threshold
  (default: 5) applies a cooldown (default: 15 minutes) that blocks both new
  `/pair` issuances and further verification attempts until it expires
- verification is gated only by active cooldowns: exhausting the issuance
  window does not prevent a user from verifying a code they were already sent
- cooldowns re-arm: when one expires, the failure counter resets and the next
  `max_failures` failed attempts apply a fresh cooldown (protection is not
  disabled after the first cooldown)
- the per-session attempt cap remains as a secondary control (5 attempts per
  active pairing session)
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
- Once a fresh snapshot proves a post belongs to a private/disabled category,
  the durable scan cursor advances past it. Content observed as private is
  never retroactively delivered merely because the category later becomes
  public. Matrix sync/commands and delivery of already-enqueued jobs are
  unaffected by a visibility-refresh suspension. The live-E2E test category
  is exempt, since it never consults the visibility snapshot.

Deployment secrets hygiene:

- the bundled deployment uses a bootstrap `dischat_admin` role and a separate
  non-superuser `dischat` runtime/migration role; set different strong
  `POSTGRES_ADMIN_PASSWORD` and `POSTGRES_PASSWORD` values in `.env`
- `.dockerignore` excludes `.env*` and runtime `config*.yaml` files; Compose
  mounts `config.yaml` read-only so credentials and room mappings do not enter
  container image layers
- the bundled PostgreSQL data volume (`postgres-data`) is the authoritative store for pairings, watches, room links, queued deliveries, and audit records — include it in your backup plan (see [Docker: Data persistence & backups](docker.md#data-persistence-backups))

## Audit coverage for live write paths

Every external write performed by the running service records an entry in the
`audit_logs` table. The canonical action names live in
`src/dischat/security/audit.py` (`LIVE_WRITE_PATHS`).

| Action | Operation | Target system | Actor / context | Target identifiers recorded | Failure policy |
| --- | --- | --- | --- | --- | --- |
| `create_pairing_pm` | Send the pairing-code PM to a Discourse account | Discourse | Requesting Matrix user (`mxid`, `platform`) | `discourse_username_used` (PM recipient) | Failed attempts are recorded with `success = FALSE` and `error_message`; the write error is re-raised so upstream retry/logic still sees it. |
| `create_discourse_reply` | Relay a Matrix reply into a Discourse topic | Discourse | Matrix user (`mxid`, `platform`) or relay account username | `topic_id`, `post_id` (success only), `matrix_room_id`, `matrix_event_id` | Same failed-attempt policy. |
| `deliver_matrix_room_message` | Deliver a Discourse post into a linked Matrix room | Matrix | `system` (`mxid = "system"`, `platform = "system"`) | `topic_id`, `post_id`, `matrix_room_id`, `matrix_event_id` | Same failed-attempt policy, including `missing_discourse_event` before any send is attempted. |
| `deliver_matrix_dm_message` | Deliver a Discourse post as a Matrix DM | Matrix | `system` + target user (`mxid`) | `topic_id`, `post_id`, `mxid` (DM recipient), `matrix_room_id`, `matrix_event_id` | Same failed-attempt policy; `missing_dm_room_id` records the send result when no room id exists. |
| `send_matrix_notice` | Send a Matrix notice: command responses and permission/error replies | Matrix | Requesting Matrix user (`mxid`, `platform`) | `matrix_room_id`, `matrix_event_id` (success only) | Same failed-attempt policy. |

Audit policy:

- **Attempts are recorded before the write.** For every live write path the
  audit row is inserted with `status = 'pending'` *before* the external API
  call, and the row's outcome (`status = 'success'` / `'failed'`,
  `success`, `error_message`) is updated *after* the write completes but
  *before* any dependent local persistence such as the
  `delivery_messages.create_mapping(...)` insert. A crash between a successful
  external write and its mapping insert therefore still leaves an audit row
  (resolution: `status = 'pending'` means the write was attempted and may have
  succeeded; `status = 'success'` proves it did). Migration
  `0006_audit_attempt_status.sql` adds the `status` column with a
  `('pending', 'success', 'failed')` check constraint.
- **Failed attempts are retained.** Every external write records one row per
  attempt regardless of outcome; failures use the same action name with
  `success = FALSE`.
- **Failure reasons are stored without secrets.** `error_message` contains a
  stable reason token (for example `missing_discourse_event`) or only the
  exception class name. Arbitrary exception text is never persisted because
  it can echo request bodies or credentials in shapes no regex can safely
  enumerate. Pairing codes and their hashes, API keys, access tokens, and
  message bodies never enter `audit_logs`.
- **Coverage is enforced structurally.** Migration
  `0005_audit_write_paths.sql` adds an `action <> ''` check constraint plus
  `(created_at)` and `(success, created_at)` indexes for triage queries, and
  live write paths require an audit repository at wiring time
  (`MissingAuditLoggerError`).

Not audited (not message-content writes performed by the running bridge):

- local PostgreSQL bookkeeping inside dischat itself (pairing sessions,
  delivery mappings, job queue state)
- read-only Discourse polls (`GET` endpoints) and Matrix syncs
- room membership operations: invite acceptance, room joins, and DM-room
  creation. These change membership only (no message content), are idempotent
  (repeated joins/creates are no-ops), and carry no message payload, so they
  are outside the message-write audit guarantee above.
