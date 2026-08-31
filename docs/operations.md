# Operations

Dischat should emit structured logs to stdout and validate required configuration on startup.

Recommended operational tasks:

- run migrations before startup
- monitor failed delivery jobs
- rotate secrets outside the repository
- keep the bot invited to target rooms and DM rooms
- back up the database regularly (see [Docker: Data persistence & backups](docker.md#data-persistence-backups))

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
   required with password auth (a password re-login without it would mint a
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
