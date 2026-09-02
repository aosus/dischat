# Configuration

Dischat uses environment variables for secrets and deployment-specific values, and YAML for room-link configuration.

Key environment variables:

- `DATABASE_URL`
- `MATRIX_HOMESERVER_URL`
- `MATRIX_ACCESS_TOKEN`
- `MATRIX_BOT_MXID`
- `MATRIX_DEVICE_ID` (required; must match the access token's device, or is
  reused for password logins so Matrix transaction IDs stay valid on restart)
- `DISCOURSE_BASE_URL`
- `DISCOURSE_API_KEY`
- `DISCOURSE_SYSTEM_USERNAME`
- `DISCOURSE_RELAY_MATRIX_USERNAME`
- `DISCOURSE_RELAY_TELEGRAM_USERNAME`
- `DISCOURSE_RELAY_DISCORD_USERNAME`
- `POLL_INTERVAL_SECONDS`
- `DELIVERY_JOB_LEASE_SECONDS` (default 120; how long a claimed delivery job
  stays `running` before another worker may reclaim it — see operations doc)
- `MATRIX_EVENT_RETENTION_DAYS` (default 7; must be positive)
- `CONFIG_FILE`
- `DEFAULT_LOCALE`
