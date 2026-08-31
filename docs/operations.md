# Operations

Dischat should emit structured logs to stdout and validate required configuration on startup.

Recommended operational tasks:

- run migrations before startup
- monitor failed delivery jobs
- rotate secrets outside the repository
- keep the bot invited to target rooms and DM rooms
- back up the database regularly (see [Docker: Data persistence & backups](docker.md#data-persistence-backups))

The Discourse feed high-water mark is persisted in
`discourse_poll_state`. After a restart, the poller pages backwards through
`/posts.json` until it reaches that cursor, so downtime longer than one feed
page does not skip posts. A brand-new deployment intentionally begins with the
current latest page instead of replaying the forum's entire history.

## Delivery job lifecycle

Delivery jobs move through the following states:

```
pending ──claim──> running ──success──> complete
   ▲                  │
   │                  ├──handled error──> failed ──(backoff)──> claim again
   └──────────────────┴──── lease expiry ── claim again (recovery)
```

- **Claim and lease**: when a worker claims a job it is set to `running` and a
  bounded lease is recorded (`claimed_at` / `lease_expires_at`). The lease is
  configurable via `DELIVERY_JOB_LEASE_SECONDS` (default 120 seconds) and must
  comfortably exceed the worst-case time of a single Matrix send.
- **Handled errors**: if delivery raises (network error, homeserver rejection,
  database hiccup), the job moves to `failed` with the exception text in
  `last_error` and a retry scheduled with exponential backoff (`next_attempt_at`,
  capped at 5 minutes). Failed jobs are re-claimed once their backoff elapses.
- **Crash recovery**: a worker that dies mid-delivery cannot release its claim,
  so claimed jobs carry an expiring lease instead. Any job still `running`
  whose lease has expired becomes claimable again in the next sweep; there is
  no separate recovery process to run. A restart therefore self-heals stranded
  jobs after at most one lease period.
- **Pre-lease rows**: the lease migration (`0004_delivery_job_leases.sql`)
  expires the leases of any `running` rows left by older versions so they are
  retried once after upgrading.

### At-least-once semantics and duplicate protection

Matrix sends and PostgreSQL writes cannot be wrapped in one transaction, so
between "send succeeded" and "mapping persisted" the process can die or the
lease can lapse while the result was never recorded. This makes delivery
**at-least-once** for that window, with two mitigations in place:

1. **Idempotent reconciliation before every send**: `deliver_job` checks the
   `delivery_messages` mapping first. If a mapping for the Discourse post and
   target room (or target recipient, for DMs) already exists, the previous
   attempt's send reached Matrix even though its outcome was never persisted;
   the retry marks the job complete without sending again. With this check, a
   retry can only produce a duplicate if the *previous* attempt failed between
   the homeserver acknowledging the event and the mapping write landing — not
   merely because a job was reclaimed.
2. **Durable Matrix transaction ids protect the reclaimed-retry path.** Before
   every Matrix write the job row is stamped with a stable transaction id
   (`matrix_tx_id`, persisted in PostgreSQL). If a send succeeded but the
   mapping never persisted, the retry re-sends with the SAME transaction id and
   the homeserver deduplicates the event. Because Matrix scopes transaction ids
   to a single **device** and a single **HTTP endpoint**, two further columns
  make this hold across restarts: the bot's stable `MATRIX_DEVICE_ID` is
   required and verified for every auth mode (a password re-login without it would mint a
   new device and invalidate the persisted ids), and the first resolved DM room
   is pinned on the job (`matrix_dm_room_id`) so a retry cannot send the same
   id to a different `/rooms/{roomId}/send` endpoint. With these in place the
   persisted id — not just the mapping above — is the source of truth for the
   crash window; the mapping-based reconciliation still guards the earlier
   window where the previous attempt persisted its outcome successfully.
3. **Fenced claims**: every claim mints a `claim_token`; lease renewal and the
   `complete`/`failed` transitions are compare-and-set on that token AND
   `status = 'running'`. A slow worker whose lease expired and whose job was
   reclaimed cannot overwrite the newer claim's state — its updates are
   silently rejected and logged.

Monitoring recommendations:

- alert on rows in `delivery_jobs` with `status = 'failed'`;
- alert on `status = 'running' AND lease_expires_at < NOW()` persisting across
  more than one poll cycle — it suggests a repeatedly dying worker rather than
  a single crash.

## Restart safety and idempotency

The Matrix `/sync` continuation token lives in the `matrix_sync_state` table.
On startup, Dischat loads it and resumes long-polling from there; only when no
token has ever been stored does it perform a fresh initial sync. The token is
written after each iteration completes its side effects (event processing,
Discourse polling, delivery drain), so a restart can replay at most one batch —
and batch replay is itself safe because of the event fence below.

Inbound Matrix events with external side effects — reply events (relayed to
Discourse as replies) and side-effecting command events such as `/pair`
(which sends a Discourse DM) — are both gated by the `matrix_event_state`
ledger: `claim` (insert marker + exclusive lease, unique per room+event)
happens before any Discourse write, the write outcome is recorded on the
marker as soon as the external write returns, and `confirm`
(`status = 'processed'`) happens only after the reply, its
`delivery_messages` mapping, and the room notice are all committed. Each
processing attempt holds a random lease token; only the lease holder may
record a write outcome, release the fence, or confirm the marker.
Consequences:

- processing the same event twice produces exactly one Discourse write;
- a replay racing a live worker always loses the fence (the worker's lease is
  fresh) and never writes a second time;
- if the process dies while the marker is still `claimed`, a replay takes the fence
  over once the lease lapses and delivers the event exactly once;
- if the process dies after the Discourse write and after the outcome was
  recorded but before the `delivery_messages` mapping is committed, the
  marker is reconciled from the recorded outcome: the reply is never written
  twice;
- once an attempt enters an external write, its marker becomes `owned` and is
  never automatically adopted or released; an ambiguous transport failure is
  therefore at-most-once and requires operator reconciliation.

Ambiguous-write policy: there is a gap between entering a Discourse write and
durably recording its outcome. Discourse offers no idempotency key for this
operation, so the bridge chooses confidentiality/integrity over automatic
availability: an `owned` marker is retained and never replayed. Alert on old
`owned` rows, reconcile the remote side manually, then either record the
outcome or explicitly remove the marker if the write is proven absent.

Mixed-version rolling upgrade: while an old-version process (before the lease
migration) still runs next to a new-version one, a pre-lease `claimed` marker
has NULL lease fields and is immediately adoptable by the new version —
without waiting for any takeover window. An old-version worker holding such a
marker mid-write can therefore have its fence adopted and the event written
again by a new-version worker. Deploy upgrades in a single restart (drain the
old process before starting the new one) rather than running mixed versions
through a sync batch.

Ledger retention: `matrix_event_state` accumulates one row per inbound
side-effecting Matrix event. Every iteration deletes `processed` rows older
than `MATRIX_EVENT_RETENTION_DAYS` (default 7 — comfortably longer than any
`/sync` replay horizon, which is bounded by the stored sync token); claimed,
owned, and written rows are never touched by retention. The delete is
supported by a partial index on `updated_at` for `status = 'processed'`.
