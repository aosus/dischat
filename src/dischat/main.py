from __future__ import annotations

import asyncio
import logging

from dischat.config import load_settings
from dischat.logging import configure_logging


async def run() -> None:
    settings = load_settings()
    settings.validate_runtime_requirements()
    logging.getLogger(__name__).info(
        "Dischat service configuration loaded from %s", settings.config_file
    )


def main() -> None:
    configure_logging()
    asyncio.run(run())
