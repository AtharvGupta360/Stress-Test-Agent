"""Integration tests for the queue and the replay log.

Requires the compose Postgres: `docker compose up -d db && python scripts/migrate.py`.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from stressagent import db
from stressagent.states import State


async def _db_available() -> bool:
    try:
        await (await db.pool()).fetchval("SELECT 1")
    except (OSError, asyncpg.PostgresError):
        return False
    return True


@pytest.fixture(autouse=True)
async def _fresh_pool():
    """The connection pool is a module-level singleton, which is right for the
    worker (one event loop per process) but wrong here: pytest-asyncio gives
    each test its own loop, and a pool bound to a closed loop raises on use.
    """
    if not await _db_available():
        await db.close_pool()
        pytest.skip("postgres not reachable")
    yield
    await db.close_pool()


def _payload(source: str) -> dict:
    return {
        "statement": "Print the sum.",
        "language": "cpp",
        "source_code": source,
        "problem_id": f"test-{uuid.uuid4().hex[:8]}",
        "samples": [{"input": "1\n2\n", "output": "2\n"}],
        "official_tests": [],
    }


async def test_identical_resubmission_is_free() -> None:
    payload = _payload("int main(){}")

    first, created_first = await db.create_submission(**payload)
    second, created_second = await db.create_submission(**payload)

    assert created_first is True
    assert created_second is False, "identical resubmission should hit the cache"
    assert first["id"] == second["id"]


async def test_pipeline_version_busts_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shipping a prompt fix must invalidate stale reports, not serve them forever."""
    payload = _payload("int main(){return 1;}")
    first, _ = await db.create_submission(**payload)

    settings = db.settings()
    monkeypatch.setattr(settings, "pipeline_version", "999")
    second, created = await db.create_submission(**payload)

    assert created is True
    assert first["id"] != second["id"]


async def test_claim_is_exclusive_and_survives_lease_expiry() -> None:
    payload = _payload("int main(){return 2;}")
    row, _ = await db.create_submission(**payload)
    submission_id = str(row["id"])

    claimed = await db.claim_job("worker-a", lease_seconds=60)
    assert claimed is not None

    # A second worker must not be able to take a job that is already leased.
    ids_taken = set()
    while (other := await db.claim_job("worker-b", lease_seconds=60)) is not None:
        ids_taken.add(str(other["id"]))
    assert str(claimed["id"]) not in ids_taken

    # Simulate the worker dying: expire the lease and confirm it is reclaimable.
    pool = await db.pool()
    await pool.execute(
        "UPDATE submissions SET lease_expires_at = now() - interval '1 minute' WHERE id = $1",
        row["id"],
    )
    reclaimed = await db.claim_job("worker-c", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed["attempts"] > 1, "reclaim must count as another attempt"

    await db.finish(submission_id, State.DONE, "AC", {"verdict": "AC"})


async def test_step_log_is_ordered_and_gapless() -> None:
    payload = _payload("int main(){return 3;}")
    row, _ = await db.create_submission(**payload)
    submission_id = str(row["id"])

    for stage in ("COMPILE", "JUDGE", "ANALYZE", "AUTHOR", "VALIDATE", "STRESS"):
        await db.log_step(submission_id, stage=stage, kind="transition", status="ok")

    steps = await db.get_steps(submission_id)
    assert [s["seq"] for s in steps] == [1, 2, 3, 4, 5, 6]
    assert [s["stage"] for s in steps][:2] == ["COMPILE", "JUDGE"]


async def test_budget_counters_accumulate() -> None:
    payload = _payload("int main(){return 4;}")
    row, _ = await db.create_submission(**payload)
    submission_id = str(row["id"])

    await db.add_usage(submission_id, 1200)
    usage = await db.add_usage(submission_id, 800)

    assert usage["tokens_used"] == 2000
    assert usage["llm_calls"] == 2
