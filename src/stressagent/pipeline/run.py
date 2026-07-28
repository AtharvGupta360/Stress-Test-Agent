"""State-machine executor for one submission."""

from __future__ import annotations

import asyncio
import logging

from ..config import settings
from ..db import finish, renew_lease, set_state
from ..llm.client import BudgetExhausted, MalformedResponse, ModelUnavailable
from ..models import Counterexample, Report
from ..runner.docker_runner import Sandbox, SandboxError, sandbox_slots
from ..states import State, Verdict
from . import stages
from .context import Context, Mismatch

log = logging.getLogger(__name__)

MAX_AUTHOR_REVISIONS = 3
LEASE_SECONDS = 120

# A failing official test is only worth returning as-is if a human can actually
# read it. Above this, we go looking for a minimal one instead.
MINIMAL_INPUT_CHARS = 400


async def _heartbeat(submission_id: str, stop: asyncio.Event) -> None:
    """Keep the lease alive while a long stress run is in flight, so another
    worker does not decide this job is orphaned and start it again."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=LEASE_SECONDS / 3)
        except TimeoutError:
            await renew_lease(submission_id, LEASE_SECONDS)


async def run_submission(row: dict) -> None:
    submission_id = str(row["id"])
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(submission_id, stop))
    try:
        async with sandbox_slots():
            await _execute(row, submission_id)
    except SandboxError as exc:
        log.exception("sandbox failure")
        await finish(
            submission_id, State.FAILED, None,
            Report(verdict="ERROR", degraded_reason=str(exc)).model_dump(mode="json"),
            error=str(exc)[:2000],
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("pipeline failure")
        await finish(
            submission_id, State.FAILED, None,
            Report(verdict="ERROR", degraded_reason=str(exc)).model_dump(mode="json"),
            error=str(exc)[:2000],
        )
    finally:
        stop.set()
        await beat


async def _execute(row: dict, submission_id: str) -> None:
    sandbox = Sandbox(submission_id)
    async with sandbox:
        ctx = Context(
            submission_id=submission_id,
            statement=row["statement"],
            language=row["language"],
            source_code=row["source_code"],
            samples=list(row["samples"] or []),
            official_tests=list(row["official_tests"] or []),
            sandbox=sandbox,
        )

        # ---------------------------------------------------------- compile
        await set_state(submission_id, State.COMPILING)
        await set_state(submission_id, State.JUDGING)
        verdict, detail = await stages.compile_and_judge(ctx)

        if verdict is not Verdict.WA:
            # AC / CE / TLE / RE all terminate here. Only WA is worth the
            # expense of authoring a reference implementation.
            report = Report(verdict=verdict.value, explanation=_terse(verdict, detail))
            await finish(submission_id, State.DONE, verdict.value, report.model_dump(mode="json"))
            return

        try:
            mismatch = await _find_counterexample(ctx, detail)
        except (ModelUnavailable, BudgetExhausted, MalformedResponse) as exc:
            # The verdict is still worth returning; we just cannot explain it.
            await ctx.note("PIPELINE", "transition", "fail", why=type(exc).__name__)
            report = Report(
                verdict=verdict.value,
                rounds_run=ctx.rounds_run,
                degraded_reason=f"{type(exc).__name__}: {exc}"[:500],
            )
            await finish(
                submission_id, State.DEGRADED, verdict.value, report.model_dump(mode="json")
            )
            return

        if mismatch is None:
            report = Report(
                verdict=verdict.value,
                rounds_run=ctx.rounds_run,
                explanation=(
                    f"Wrong answer on the official tests, but {ctx.rounds_run} randomized "
                    "rounds against the reference implementation found no disagreement. "
                    "The bug likely needs a larger or more adversarial input than the "
                    "brute force can reach."
                ),
            )
            await finish(submission_id, State.DONE, verdict.value, report.model_dump(mode="json"))
            return

        # ---------------------------------------------------------- explain
        await set_state(submission_id, State.EXPLAINING)
        try:
            expl = await stages.explain(ctx, mismatch)
            bug_class, explanation, fix = expl.bug_class, expl.explanation, expl.suggested_fix
        except (ModelUnavailable, BudgetExhausted, MalformedResponse) as exc:
            # We have a verified counterexample. Losing the label is a cosmetic
            # loss, so ship the four lines rather than degrading the whole run.
            await ctx.note("EXPLAIN", "llm_call", "fail", why=type(exc).__name__)
            bug_class, explanation, fix = None, "", ""

        report = Report(
            verdict=verdict.value,
            counterexample=Counterexample(
                input=mismatch.input, expected=mismatch.expected, actual=mismatch.actual
            ),
            bug_class=bug_class,
            explanation=explanation,
            suggested_fix=fix,
            rounds_run=ctx.rounds_run,
            shrink_steps=ctx.shrink_steps,
        )
        await finish(submission_id, State.DONE, verdict.value, report.model_dump(mode="json"))


async def _find_counterexample(ctx: Context, judge_detail: dict) -> Mismatch | None:
    """Gate 0, then the authoring/validation/stress path."""
    free = judge_detail.get("free_counterexample")
    fallback: Mismatch | None = None
    if free:
        fallback = Mismatch(
            seed=-1, size=-1, input=free["input"],
            expected=free["expected"], actual=free["actual"], reason="wrong answer",
        )
        # Only short-circuit when the failing test is ALREADY small enough to
        # read. Official tests routinely carry n=200000, and handing back a
        # 200k-line input is not a counterexample anyone can act on --
        # minimality is the entire product. A large failing test still proves
        # the verdict, so we keep it as a fallback and go find a small one.
        if len(free["input"]) <= MINIMAL_INPUT_CHARS:
            await ctx.note("STRESS", "gate", "skip", why="failing_test_already_minimal")
            return fallback
        await ctx.note(
            "STRESS", "gate", "ok",
            why="failing_test_too_large_to_read", chars=len(free["input"]),
        )

    try:
        minimal = await _author_and_stress(ctx)
    except (ModelUnavailable, BudgetExhausted, MalformedResponse):
        # A large but real counterexample beats degrading to nothing.
        if fallback is not None:
            await ctx.note("STRESS", "gate", "skip", why="model_failed_using_large_test")
            return fallback
        raise

    # Every unminimised exit still returns the fallback when we have one: the
    # user came for a counterexample, and a big one is worth more than none.
    return minimal or fallback


async def _author_and_stress(ctx: Context) -> Mismatch | None:
    spec = await stages.analyze(ctx)

    if not spec.output_unique:
        # Several answers are acceptable, so a diff would flag every valid one.
        await stages.author_checker(ctx)

    failure = ""
    for revision in range(MAX_AUTHOR_REVISIONS):
        if ctx.out_of_time:
            await ctx.note("AUTHOR", "gate", "skip", why="wall_clock_budget")
            return None
        ctx.author_revision = revision
        await set_state(ctx.submission_id, State.AUTHORING)
        await stages.author(ctx, failure)

        await set_state(ctx.submission_id, State.VALIDATING)
        failure = await stages.validate(ctx)
        if not failure:
            break
    else:
        # Never got a trustworthy reference implementation. Reporting a
        # counterexample from an unvalidated brute force would be a guess, and
        # guessing is the one thing this system exists not to do.
        await ctx.note("VALIDATE", "gate", "fail", why="max_revisions_exhausted")
        return None

    await set_state(ctx.submission_id, State.STRESSING)
    return await stages.stress(ctx)


def _terse(verdict: Verdict, detail: dict) -> str:
    match verdict:
        case Verdict.AC:
            return f"Accepted on all {detail.get('tests', 0)} tests."
        case Verdict.CE:
            return f"Compilation failed:\n{detail.get('message', '')}"
        case Verdict.TLE:
            return f"Time limit exceeded on test {detail.get('test_index', 0) + 1}."
        case Verdict.RE:
            return (
                f"Runtime error on test {detail.get('test_index', 0) + 1}: "
                f"{detail.get('stderr', '')[:400]}"
            )
        case _:
            return ""


def lease_seconds() -> int:
    return max(LEASE_SECONDS, settings().max_wall_seconds_per_submission // 2)
