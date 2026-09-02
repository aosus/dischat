from __future__ import annotations

import os
import time
from pathlib import Path

HEARTBEAT_FILE = Path(os.getenv("DISCHAT_HEARTBEAT_FILE", "/tmp/dischat-heartbeat"))
MAX_AGE_SECONDS = int(os.getenv("DISCHAT_HEALTH_MAX_AGE_SECONDS", "300"))


def write_heartbeat() -> None:
    HEARTBEAT_FILE.touch()


def main() -> None:
    try:
        age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    except OSError as exc:
        raise SystemExit(f"Dischat heartbeat is unavailable: {exc}") from exc
    if age > MAX_AGE_SECONDS:
        raise SystemExit(f"Dischat heartbeat is stale ({age:.0f}s > {MAX_AGE_SECONDS}s)")


if __name__ == "__main__":
    main()
