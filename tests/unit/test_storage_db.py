from __future__ import annotations

from typing import cast

import asyncpg
import pytest

from dischat.storage.db import apply_sql_migrations


async def test_missing_migration_files_fail_startup(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="No SQL migrations found"):
        await apply_sql_migrations(cast("asyncpg.Pool", None), tmp_path)
