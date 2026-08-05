"""HTTP API: submit, poll, stream, replay."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from ..config import settings
from ..db import (
    close_pool,
    create_submission,
    get_artifacts,
    get_steps,
    get_submission,
    pool,
)
from ..models import SubmitRequest
from ..states import TERMINAL, State

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await pool()
    yield
    await close_pool()


app = FastAPI(title="Stress-Test Agent", version="0.1.0", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    p = await pool()
    async with p.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"ok": True, "model": settings().gemini_model}


@app.post("/submissions", status_code=201)
async def submit(req: SubmitRequest, response: Response) -> dict:
    row, created = await create_submission(
        statement=req.statement,
        language=req.language,
        source_code=req.source_code,
        problem_id=req.problem_id,
        samples=[s.model_dump() for s in req.samples],
        official_tests=[s.model_dump() for s in req.official_tests],
        external_verdict=req.external_verdict,
    )
    if not created:
        # Idempotent replay: same code, same problem, same pipeline version.
        response.status_code = 200
    return {
        "id": str(row["id"]),
        "state": row["state"],
        "created": created,
        "result": row["result"],
    }


@app.get("/submissions/{submission_id}")
async def status(submission_id: str) -> dict:
    row = await get_submission(submission_id)
    if row is None:
        raise HTTPException(404, "no such submission")
    return {
        "id": str(row["id"]),
        "state": row["state"],
        "verdict": row["verdict"],
        "result": row["result"],
        "error": row["error"],
        "tokens_used": row["tokens_used"],
        "llm_calls": row["llm_calls"],
        "created_at": row["created_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


@app.get("/submissions/{submission_id}/steps")
async def steps(submission_id: str) -> dict:
    """The replay log: exactly how the bug was found, in order."""
    if await get_submission(submission_id) is None:
        raise HTTPException(404, "no such submission")
    rows = await get_steps(submission_id)
    return {
        "steps": [
            {
                "seq": r["seq"],
                "stage": r["stage"],
                "kind": r["kind"],
                "status": r["status"],
                "output": r["output"],
                "tokens": r["tokens"],
                "duration_ms": r["duration_ms"],
                "at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@app.get("/submissions/{submission_id}/artifacts")
async def artifacts(submission_id: str) -> dict:
    """The generated brute force, generator, validator and counterexample."""
    if await get_submission(submission_id) is None:
        raise HTTPException(404, "no such submission")
    rows = await get_artifacts(submission_id)
    return {
        "artifacts": [
            {
                "kind": r["kind"],
                "revision": r["revision"],
                "content": r["content"],
                "meta": r["meta"],
            }
            for r in rows
        ]
    }


@app.get("/submissions/{submission_id}/stream")
async def stream(submission_id: str) -> EventSourceResponse:
    """Live progress.

    Implemented by tailing agent_steps rather than an in-process callback: the
    API and the worker are separate processes, so the database is the only
    channel they actually share.
    """
    if await get_submission(submission_id) is None:
        raise HTTPException(404, "no such submission")

    async def events() -> AsyncIterator[dict]:
        last_seq = 0
        while True:
            for step in await get_steps(submission_id):
                if step["seq"] <= last_seq:
                    continue
                last_seq = step["seq"]
                yield {
                    "event": "step",
                    "data": json.dumps(
                        {
                            "seq": step["seq"],
                            "stage": step["stage"],
                            "kind": step["kind"],
                            "status": step["status"],
                            "output": step["output"],
                        }
                    ),
                }

            row = await get_submission(submission_id)
            if row is not None and row["state"] in TERMINAL:
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {"state": row["state"], "verdict": row["verdict"], "result": row["result"]}
                    ),
                }
                return
            await asyncio.sleep(0.75)

    return EventSourceResponse(events())


@app.get("/stats")
async def stats() -> dict:
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT state, count(*) AS n FROM submissions GROUP BY state ORDER BY state"
        )
        totals = await conn.fetchrow(
            "SELECT COALESCE(sum(tokens_used), 0) AS tokens,"
            " COALESCE(sum(llm_calls), 0) AS calls FROM submissions"
        )
    return {
        "by_state": {r["state"]: r["n"] for r in rows},
        "tokens_used": totals["tokens"],
        "llm_calls": totals["calls"],
        "states": [s.value for s in State],
    }


# Mounted last so the API routes above always win the match.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def run() -> None:
    import uvicorn

    uvicorn.run("stressagent.api.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
