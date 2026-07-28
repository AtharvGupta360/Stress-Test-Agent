"""Stateless worker: claim a job, run the state machine, repeat.

Stateless in the sense that matters -- all durable state is in Postgres, so a
worker can be killed at any point and another one picks the job up once the
lease expires. `attempts` bounds how many times that can happen, otherwise a
submission that reliably crashes a worker becomes an infinite loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid

from .config import settings
from .db import claim_job, close_pool, finish, pool
from .models import Report
from .pipeline.run import lease_seconds, run_submission
from .states import State

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
IDLE_POLL_SECONDS = 2.0


async def _handle(row: dict) -> None:
    submission_id = str(row["id"])
    if row["attempts"] > MAX_ATTEMPTS:
        log.warning("submission %s exceeded %d attempts", submission_id, MAX_ATTEMPTS)
        await finish(
            submission_id, State.FAILED, None,
            Report(
                verdict="ERROR",
                degraded_reason=f"exceeded {MAX_ATTEMPTS} attempts; likely crashes the worker",
            ).model_dump(mode="json"),
            error="max attempts exceeded",
        )
        return
    log.info("running submission %s (attempt %d)", submission_id, row["attempts"])
    await run_submission(row)
    log.info("finished submission %s", submission_id)


async def worker_loop(worker_id: str, shutdown: asyncio.Event) -> None:
    lease = lease_seconds()
    while not shutdown.is_set():
        try:
            row = await claim_job(worker_id, lease)
        except Exception:  # noqa: BLE001
            log.exception("claim failed; backing off")
            await _sleep_or_stop(shutdown, 5.0)
            continue

        if row is None:
            await _sleep_or_stop(shutdown, IDLE_POLL_SECONDS)
            continue

        try:
            await _handle(row)
        except Exception:  # noqa: BLE001
            # run_submission already writes a terminal state for anything it can
            # catch; reaching here means the failure was outside it.
            log.exception("unhandled failure on %s", row["id"])


async def _sleep_or_stop(shutdown: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
    )
    await pool()

    base = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    shutdown = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, shutdown.set)

    n = settings().worker_concurrency
    log.info("worker %s starting with concurrency %d", base, n)
    tasks = [asyncio.create_task(worker_loop(f"{base}-{i}", shutdown)) for i in range(n)]

    try:
        await asyncio.gather(*tasks)
    finally:
        await close_pool()
        log.info("worker %s stopped", base)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
