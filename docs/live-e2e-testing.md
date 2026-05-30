# Live E2E Testing

Set `RUN_LIVE_E2E=1`, provide `.env.live-e2e`, and use the dedicated compose file:

```bash
docker compose -f docker-compose.live-e2e.yml --env-file .env.live-e2e up --build --abort-on-container-exit
```

Live tests must only interact with Discourse category `56`.

The live suite currently exercises:

- Discourse topic creation in category `56` and delivery into the configured Matrix room
- Matrix reply roundtrip from the shared room back into Discourse via the relay account
- watched-category delivery into a Matrix DM

The configured `MATRIX_TEST_ROOM_ID` must already contain the bridge bot and the Matrix test user.
