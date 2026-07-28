"""Test configuration.

The integration tests get their own database rather than sharing the
development one. Sharing does not work: these tests write real rows into the
real queue, and a worker running alongside them claims the junk within its
2-second poll interval -- before any test-local teardown can park it -- then
drives it through the full pipeline and bills the model API for it.

Pointing the tests at `<dbname>_test`, which no worker watches, makes the suite
both deterministic and free.
"""

from __future__ import annotations

import asyncio
import pathlib
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from stressagent import db
from stressagent.config import settings

MIGRATIONS = pathlib.Path(__file__).parents[1] / "migrations"


def _test_database_url(url: str) -> tuple[str, str, str]:
    """Returns (test_url, maintenance_url, test_dbname)."""
    parts = urlparse(url)
    name = parts.path.lstrip("/")
    test_name = f"{name}_test"
    test_url = urlunparse(parts._replace(path=f"/{test_name}"))
    admin_url = urlunparse(parts._replace(path="/postgres"))
    return test_url, admin_url, test_name


async def _provision() -> str | None:
    live = settings().database_url
    test_url, admin_url, test_name = _test_database_url(live)

    try:
        conn = await asyncpg.connect(admin_url)
    except (OSError, asyncpg.PostgresError):
        return None

    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", test_name)
        if not exists:
            # CREATE DATABASE cannot run inside a transaction block.
            await conn.execute(f'CREATE DATABASE "{test_name}"')
    finally:
        await conn.close()

    conn = await asyncpg.connect(test_url)
    try:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            already = await conn.fetchval("SELECT to_regclass('public.submissions')")
            if already:
                break
            await conn.execute(path.read_text(encoding="utf-8"))
        await conn.execute("TRUNCATE submissions CASCADE")
    finally:
        await conn.close()

    return test_url


@pytest.fixture(scope="session", autouse=True)
def _isolated_database() -> None:
    url = asyncio.run(_provision())
    if url is None:
        return  # no Postgres available; the db tests skip themselves
    # settings() is lru_cached, so mutating the instance redirects every caller.
    settings().database_url = url
    db._pool = None
