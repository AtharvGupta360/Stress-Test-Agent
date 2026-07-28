#!/usr/bin/env python3
"""Apply migrations/*.sql in filename order, once each."""

from __future__ import annotations

import asyncio
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from stressagent.config import settings  # noqa: E402

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"


async def main() -> int:
    conn = await asyncpg.connect(settings().database_url)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        rows = await conn.fetch("SELECT filename FROM schema_migrations")
        applied = {r["filename"] for r in rows}

        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                print(f"  skip  {path.name}")
                continue
            print(f"apply   {path.name}")
            # One transaction per file: a failed migration leaves no partial DDL.
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
                )
        print("migrations up to date")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
