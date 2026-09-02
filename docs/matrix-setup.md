# Matrix Setup

Configure a Matrix bot account and provide either a usable access token or
login credentials outside the repository. `MATRIX_DEVICE_ID` is mandatory:

- with an access token, use the token's actual device id. You can retrieve it
  from `GET /_matrix/client/v3/account/whoami` using that token;
- with password authentication, choose a stable id once and keep it unchanged
  across restarts. Dischat supplies it during login and verifies the resulting
  session with `whoami`.

Dischat refuses to start when the configured id differs from the authenticated
device. Matrix scopes transaction-id deduplication to the device, so silently
rotating it could duplicate messages after a retry.

The bot should be invited to linked rooms and accepts invites by default.
