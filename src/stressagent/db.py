"""Postgres access layer.

The queue lives in the same database as the audit log on purpose: claiming a
job, transitioning its state and appending the step that explains why all commit
in one transaction. With Redis in the middle those three can disagree, and the
replay log -- the whole point of persisting agent steps -- stops being truthful.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

from .config import settings
from .states import State

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    # asyncpg hands back jsonb as raw text unless told otherwise.
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings().database_url, min_size=2, max_size=10, init=_init_conn
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def idempotency_key(source_code: str, problem_id: str, language: str) -> str:
    """Identical resubmissions cost nothing.

    pipeline_version is part of the hash so that shipping a prompt or gate fix
    invalidates stale results -- otherwise a bad cached report would be served
    forever and the fix would be invisible.
    """
    blob = "\x00".join([source_code, problem_id, language, settings().pipeline_version])
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------- submissions


async def create_submission(
    *,
    statement: str,
    language: str,
    source_code: str,
    problem_id: str,
    samples: list[dict],
    official_tests: list[dict],
) -> tuple[dict, bool]:
    """Returns (row, created). created=False means we served a cached result."""
    key = idempotency_key(source_code, problem_id, language)
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO submissions
                (idempotency_key, problem_id, statement, language, source_code,
                 samples, official_tests)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
            """,
            key, problem_id, statement, language, source_code, samples, official_tests,
        )
        if row is not None:
            return dict(row), True

        existing = await conn.fetchrow(
            "SELECT * FROM submissions WHERE idempotency_key = $1", key
        )
        assert existing is not None
        # A DEGRADED result was produced without the model; let a resubmission
        # retry it rather than serving the stunted report forever.
        if existing["state"] == State.DEGRADED:
            retried = await conn.fetchrow(
                """
                UPDATE submissions
                   SET state = 'SUBMITTED', error = NULL, result = NULL,
                       lease_expires_at = NULL, finished_at = NULL
                 WHERE id = $1 AND state = 'DEGRADED'
                RETURNING *
                """,
                existing["id"],
            )
            if retried is not None:
                return dict(retried), True
        return dict(existing), False


async def get_submission(submission_id: str) -> dict | None:
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM submissions WHERE id = $1", submission_id)
        return dict(row) if row else None


async def claim_job(worker_id: str, lease_seconds: int) -> dict | None:
    """Atomically lease one job. `SKIP LOCKED` lets N workers poll the same
    table without serialising on each other."""
    p = await pool()
    async with p.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            WITH claimed AS (
                SELECT id FROM submissions
                 WHERE state NOT IN ('DONE', 'FAILED', 'DEGRADED')
                   AND (lease_expires_at IS NULL OR lease_expires_at < now())
                 ORDER BY created_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE submissions s
               SET lease_expires_at = now() + make_interval(secs => $2),
                   worker_id = $1,
                   attempts = s.attempts + 1
              FROM claimed
             WHERE s.id = claimed.id
            RETURNING s.*
            """,
            worker_id, lease_seconds,
        )
        return dict(row) if row else None


async def renew_lease(submission_id: str, lease_seconds: int) -> None:
    p = await pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE submissions SET lease_expires_at = now() + make_interval(secs => $2)"
            " WHERE id = $1",
            submission_id, lease_seconds,
        )


async def set_state(submission_id: str, state: State) -> None:
    p = await pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE submissions SET state = $2 WHERE id = $1", submission_id, state)


async def finish(
    submission_id: str, state: State, verdict: str | None, result: dict, error: str = ""
) -> None:
    p = await pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE submissions
               SET state = $2, verdict = $3, result = $4, error = NULLIF($5, ''),
                   finished_at = now(), lease_expires_at = NULL
             WHERE id = $1
            """,
            submission_id, state, verdict, result, error,
        )


async def get_usage(submission_id: str) -> dict:
    """Read the budget counters without touching them.

    Kept separate from add_usage so a pre-flight budget check cannot be mistaken
    for a call actually happening -- conflating the two silently halves the
    effective call budget.
    """
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tokens_used, llm_calls FROM submissions WHERE id = $1", submission_id
        )
        return dict(row) if row else {"tokens_used": 0, "llm_calls": 0}


async def add_usage(submission_id: str, tokens: int) -> dict:
    """Record exactly one completed model call. Returns the running totals."""
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE submissions
               SET tokens_used = tokens_used + $2, llm_calls = llm_calls + 1
             WHERE id = $1
            RETURNING tokens_used, llm_calls
            """,
            submission_id, tokens,
        )
        return dict(row)


# ---------------------------------------------------------------- agent steps


async def log_step(
    submission_id: str,
    *,
    stage: str,
    kind: str,
    status: str,
    payload: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    tokens: int = 0,
    duration_ms: int = 0,
) -> int:
    """Append to the replay log. `seq` is allocated inside the statement so two
    concurrent writers cannot pick the same number."""
    p = await pool()
    async with p.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO agent_steps
                (submission_id, seq, stage, kind, status, payload, output, tokens, duration_ms)
            VALUES (
                $1,
                (SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_steps WHERE submission_id = $1),
                $2, $3, $4, $5, $6, $7, $8
            )
            RETURNING seq
            """,
            submission_id, stage, kind, status, payload or {}, output or {}, tokens, duration_ms,
        )


async def get_steps(submission_id: str) -> list[dict]:
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM agent_steps WHERE submission_id = $1 ORDER BY seq", submission_id
        )
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ artifacts


async def save_artifact(
    submission_id: str, kind: str, content: str, revision: int = 0, meta: dict | None = None
) -> None:
    p = await pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO artifacts (submission_id, kind, revision, content, meta)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (submission_id, kind, revision)
            DO UPDATE SET content = EXCLUDED.content, meta = EXCLUDED.meta
            """,
            submission_id, kind, revision, content, meta or {},
        )


async def get_artifacts(submission_id: str) -> list[dict]:
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM artifacts WHERE submission_id = $1 ORDER BY kind, revision",
            submission_id,
        )
        return [dict(r) for r in rows]
