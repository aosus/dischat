# Security

Security rules implemented in this baseline:

- pairing codes are hashed
- pairing codes expire
- pairing codes are single-use
- raw pairing codes are not stored in the database model
- posting identity must come from pairing or relay configuration
- live test category checks fail closed for unexpected category IDs

Still to complete in runtime code:

- persistent pairing attempt rate limits
- audit persistence for every live write path
- production poller filtering of non-public categories before enqueue
