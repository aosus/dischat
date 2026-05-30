# Live E2E Testing

Set `RUN_LIVE_E2E=1` and use the dedicated compose file:

```bash
docker compose -f docker-compose.live-e2e.yml --env-file .env.live-e2e up --build --abort-on-container-exit
```

Live tests must only interact with Discourse category `56`.
