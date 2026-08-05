"""The pipeline stages.

Each function is one node of the state machine in states.py. They are ordinary
async functions -- the model is called inside three of them and nowhere else,
which is what keeps the token budget predictable.
"""

from __future__ import annotations

import json

from ..config import settings
from ..db import get_artifact, save_artifact
from ..llm import prompts
from ..llm.client import generate
from ..models import AuthorOutput, CheckerOutput, ConstraintSpec, ExplainOutput
from ..runner.docker_runner import ExecResult
from ..states import Verdict
from .context import Context, Mismatch

# Ceiling on the analyzer's estimate, not a target. Calibration walks down from
# here to whatever the generated brute force can actually sustain.
MAX_SIZE_KNOB = 100_000

# How long the brute force gets on one calibration case before we call it too slow.
BRUTE_CALIBRATION_TIMEOUT = 12.0

# Each failure halves the size knob, so this reaches 1 from any starting point.
MAX_CALIBRATION_STEPS = 10

# Internal sentinel: the brute force was too slow, which is a size problem to be
# solved by calibration rather than a code problem to send back to the model.
TOO_SLOW = "\x00too-slow"


def tokens(text: str) -> list[str]:
    return text.split()


# --------------------------------------------------------- compile and judge


async def compile_and_judge(ctx: Context) -> tuple[Verdict, dict]:
    """Compile, then run the official tests. No model calls happen here."""
    compile_res = await ctx.sandbox.compile_user(ctx.language, ctx.source_code)
    if compile_res.code != 0:
        await ctx.note("COMPILE", "sandbox_run", "fail", stderr=compile_res.stderr[:4000])
        return Verdict.CE, {"message": compile_res.stderr[:4000]}
    await ctx.note("COMPILE", "sandbox_run", "ok")

    tests = ctx.official_tests or ctx.samples
    for i, test in enumerate(tests):
        res: ExecResult = await ctx.sandbox.exec(
            ctx.user_cmd, stdin_data=test["input"], timeout=settings().sandbox_per_run_timeout
        )
        if res.timed_out:
            await ctx.note("JUDGE", "sandbox_run", "fail", test=i, why="timeout")
            return Verdict.TLE, {"test_index": i}
        if res.code != 0:
            await ctx.note("JUDGE", "sandbox_run", "fail", test=i, why="runtime_error")
            return Verdict.RE, {"test_index": i, "stderr": res.stderr[:2000]}
        if tokens(res.stdout) != tokens(test["output"]):
            await ctx.note("JUDGE", "sandbox_run", "fail", test=i, why="wrong_answer")
            # Gate 0: the failing official test IS a counterexample, already
            # minimal-ish and free. No reason to spend a single token finding one.
            return Verdict.WA, {
                "test_index": i,
                "free_counterexample": {
                    "input": test["input"],
                    "expected": test["output"],
                    "actual": res.stdout,
                },
            }

    await ctx.note("JUDGE", "sandbox_run", "ok", tests=len(tests))
    return Verdict.AC, {"tests": len(tests)}


# -------------------------------------------------------------------- analyze


async def analyze(ctx: Context) -> ConstraintSpec:
    # A retry after a rate-limit or crash reuses the same submission row, so a
    # spec extracted on an earlier attempt is still valid. Recomputing it spends
    # a model call to rediscover a fact we already wrote down -- which on a
    # throttled key is the difference between finishing and not.
    cached = await get_artifact(ctx.submission_id, "spec")
    if cached:
        spec = ConstraintSpec.model_validate_json(cached)
        ctx.spec = spec
        await ctx.note("ANALYZE", "gate", "skip", why="reused_cached_spec")
        return spec

    spec = await generate(
        ctx.submission_id,
        stage="ANALYZE",
        system=prompts.ANALYZE_SYSTEM,
        prompt=prompts.ANALYZE_USER.substitute(
            statement=ctx.statement, samples_block=prompts.samples_block(ctx.samples)
        ),
        schema=ConstraintSpec,
        temperature=0.1,
    )
    # Only an upper bound against a wild estimate -- the real limit is measured
    # in _calibrate_size(). A fixed low cap is actively harmful: the size knob
    # means different things in different problems (a count of elements, where
    # the brute force is exponential and must stay tiny; or a magnitude, where
    # it is linear and 10^5 is cheap), and capping a magnitude at a count-sized
    # number puts real bugs permanently out of reach.
    spec.size_knob_brute_max = max(1, min(spec.size_knob_brute_max, MAX_SIZE_KNOB))
    ctx.spec = spec
    await save_artifact(ctx.submission_id, "spec", spec.model_dump_json(indent=2))
    await ctx.note(
        "ANALYZE", "gate", "ok",
        size_knob=spec.size_knob, brute_max=spec.size_knob_brute_max,
        output_unique=spec.output_unique,
        structures=[s.value for s in spec.structures],
    )
    return spec


# --------------------------------------------------------------------- author


async def author(ctx: Context, failure: str = "") -> AuthorOutput:
    """Brute force, generator and validator in one call.

    Splitting them across calls doubles the rate of the dominant failure mode:
    the two programs disagreeing on the exact input format.
    """
    assert ctx.spec is not None
    spec_json = ctx.spec.model_dump_json(indent=2)

    if failure and ctx.author is not None:
        prompt = prompts.AUTHOR_REPAIR.substitute(
            failure=failure,
            brute=ctx.author.brute_py,
            generator=ctx.author.generator_py,
            validator=ctx.author.validator_py,
        )
    else:
        prompt = prompts.AUTHOR_USER.substitute(
            statement=ctx.statement,
            samples_block=prompts.samples_block(ctx.samples),
            spec=spec_json,
            size_knob=ctx.spec.size_knob,
            size_max=ctx.spec.size_knob_brute_max,
        )

    out = await generate(
        ctx.submission_id,
        stage="AUTHOR",
        system=prompts.AUTHOR_SYSTEM,
        prompt=prompt,
        schema=AuthorOutput,
        temperature=0.4 if failure else 0.2,
        max_output_tokens=16384,
    )
    ctx.author = out
    for kind, content in (
        ("brute", out.brute_py),
        ("generator", out.generator_py),
        ("validator", out.validator_py),
    ):
        await save_artifact(ctx.submission_id, kind, content, revision=ctx.author_revision)
    # kind="artifact", not "llm_call": the client already logged the call
    # itself, and double-labelling makes the replay log read as two calls.
    await ctx.note("AUTHOR", "artifact", "ok", revision=ctx.author_revision, repair=bool(failure))
    return out


async def author_checker(ctx: Context) -> str:
    """Only for problems where several outputs are equally correct."""
    assert ctx.spec is not None
    out = await generate(
        ctx.submission_id,
        stage="CHECKER",
        system=prompts.CHECKER_SYSTEM,
        prompt=prompts.CHECKER_USER.substitute(
            statement=ctx.statement, spec=ctx.spec.model_dump_json(indent=2)
        ),
        schema=CheckerOutput,
        temperature=0.2,
    )
    ctx.checker = out.checker_py
    await save_artifact(ctx.submission_id, "checker", out.checker_py)
    await ctx.note("CHECKER", "artifact", "ok")
    return out.checker_py


# ------------------------------------------------------------------- validate


async def validate(ctx: Context) -> str:
    """The gates. Returns "" on success, or a failure description to repair with.

    Nothing downstream is trustworthy until these pass. A brute force that
    cannot reproduce the samples is not a reference implementation, it is a
    second buggy program, and stress-testing against it produces noise.
    """
    assert ctx.author is not None
    a = ctx.author

    await ctx.sandbox.put_file("brute.py", a.brute_py)
    await ctx.sandbox.put_file("generator.py", a.generator_py)
    await ctx.sandbox.put_file("validator.py", a.validator_py)
    if ctx.checker:
        await ctx.sandbox.put_file("checker.py", ctx.checker)

    # -- syntax -------------------------------------------------------------
    for name in ("brute.py", "generator.py", "validator.py"):
        res = await ctx.sandbox.exec(["python3", "-m", "py_compile", name], timeout=15)
        if res.code != 0:
            await ctx.note("VALIDATE", "gate", "fail", which=name, why="syntax")
            return f"{name} does not compile:\n{res.stderr[:1500]}"

    # -- Gate 2: the generator runs, and its output is legal -----------------
    assert ctx.spec is not None
    failure = await _probe(ctx, 1)
    if failure:
        return failure

    failure = await _calibrate_size(ctx)
    if failure:
        return failure

    # -- Gate 1: the brute force reproduces the official samples -------------
    for i, sample in enumerate(ctx.samples):
        res = await ctx.sandbox.exec(
            ["python3", "brute.py"], stdin_data=sample["input"], timeout=20
        )
        if res.timed_out or res.code != 0:
            await ctx.note("VALIDATE", "gate", "fail", which="brute", sample=i)
            return f"brute.py failed on sample {i + 1}:\n{res.stderr[:1500]}"

        if ctx.checker:
            ok = await _run_checker(ctx, sample["input"], res.stdout, sample["output"])
        else:
            ok = tokens(res.stdout) == tokens(sample["output"])
        if not ok:
            await ctx.note("VALIDATE", "gate", "fail", which="brute", sample=i, why="mismatch")
            return (
                f"brute.py disagrees with sample {i + 1}.\n"
                f"Input:\n{sample['input'][:800]}\n"
                f"Expected:\n{sample['output'][:800]}\n"
                f"brute.py printed:\n{res.stdout[:800]}"
            )

    await ctx.note("VALIDATE", "gate", "ok", samples_checked=len(ctx.samples))
    return ""


async def _probe(ctx: Context, size: int) -> str:
    """Generate one case at `size`, validate it, and run the brute force on it.

    Returns "" if all three succeed, otherwise a repair description. A timeout
    is reported as the sentinel TOO_SLOW so the caller can decide whether to
    shrink the size knob or give up.
    """
    gen = await ctx.sandbox.exec(["python3", "generator.py", "12345", str(size)], timeout=15)
    if gen.code != 0 or not gen.stdout.strip():
        await ctx.note("VALIDATE", "gate", "fail", which="generator", size=size)
        return f"generator.py failed at SIZE={size} (exit {gen.code}):\n{gen.stderr[:1500]}"

    val = await ctx.sandbox.exec(["python3", "validator.py"], stdin_data=gen.stdout, timeout=15)
    if val.code != 0:
        await ctx.note("VALIDATE", "gate", "fail", which="validator", size=size)
        return (
            f"generator.py produced input its own validator rejects at "
            f"SIZE={size}: {val.stderr[:800]}\nInput was:\n{gen.stdout[:800]}"
        )

    bru = await ctx.sandbox.exec(
        ["python3", "brute.py"], stdin_data=gen.stdout, timeout=BRUTE_CALIBRATION_TIMEOUT
    )
    if bru.timed_out:
        return TOO_SLOW
    if bru.code != 0:
        await ctx.note("VALIDATE", "gate", "fail", which="brute", why="crash", size=size)
        return (
            f"brute.py crashed on its own generator's output at SIZE={size}:\n"
            f"{bru.stderr[:1500]}\nInput was:\n{gen.stdout[:800]}"
        )
    return ""


async def _calibrate_size(ctx: Context) -> str:
    """Measure the largest size knob the brute force can actually sustain.

    The analyzer's estimate is a guess made from prose, and guessing low is not
    the safe direction it appears to be: a size knob capped below where a bug
    first appears makes that bug permanently invisible, and the run reports
    "no disagreement found" with total confidence. So start at the estimate and
    halve until it fits, recording where it landed.
    """
    assert ctx.spec is not None

    for _ in range(MAX_CALIBRATION_STEPS):
        size = ctx.spec.size_knob_brute_max
        result = await _probe(ctx, size)

        if result == TOO_SLOW:
            if size <= 1:
                await ctx.note("VALIDATE", "gate", "fail", which="brute", why="timeout", size=1)
                return (
                    "brute.py times out even at SIZE=1. It is far too expensive; "
                    "write a simpler, more direct simulation of the statement."
                )
            ctx.spec.size_knob_brute_max = max(1, size // 2)
            await ctx.note(
                "VALIDATE", "gate", "skip",
                why="brute_too_slow", was=size, now=ctx.spec.size_knob_brute_max,
            )
            continue

        if result:
            return result

        await ctx.note("VALIDATE", "gate", "ok", which="calibration", size_knob=size)
        return ""

    return "could not find a workable size for brute.py"


async def _run_checker(ctx: Context, inp: str, contestant: str, reference: str) -> bool:
    await ctx.sandbox.put_file("_in.txt", inp)
    await ctx.sandbox.put_file("_user.txt", contestant)
    await ctx.sandbox.put_file("_ref.txt", reference)
    res = await ctx.sandbox.exec(
        ["python3", "checker.py", "_in.txt", "_user.txt", "_ref.txt"], timeout=15
    )
    return res.code == 0


# --------------------------------------------------------- stress and shrink


async def stress(ctx: Context) -> Mismatch | None:
    """Hand the whole loop to the in-container driver and stream its progress.

    Shrinking happens inside the same invocation: the driver already has the
    generator warm, and a round trip per shrink step would cost more than the
    search itself.
    """
    assert ctx.spec is not None
    s = settings()

    await ctx.sandbox.put_file("driver.py", _driver_source())

    cmd = [
        "python3", "driver.py",
        "--user-cmd", " ".join(ctx.user_cmd),
        "--rounds", str(s.stress_rounds),
        "--size-min", "1",
        "--size-max", str(ctx.spec.size_knob_brute_max),
        "--time-budget", str(s.stress_time_budget),
        "--per-run-timeout", str(s.sandbox_per_run_timeout),
        "--seed-base", "1",
    ]

    found: Mismatch | None = None
    async for event in ctx.sandbox.exec_stream(cmd, timeout=s.stress_time_budget + 90):
        kind = event.get("event")
        if kind == "progress":
            ctx.rounds_run = event.get("round", 0)
            await ctx.note("STRESS", "progress", "ok", round=ctx.rounds_run)
        elif kind == "mismatch":
            await ctx.note("STRESS", "gate", "ok", why=event.get("reason"), seed=event.get("seed"))
        elif kind == "shrink":
            ctx.shrink_steps += 1
            await ctx.note("SHRINK", "progress", "ok", size=event.get("size"))
        elif kind == "done":
            ctx.rounds_run = event.get("rounds", ctx.rounds_run)
            ctx.shrink_steps = event.get("shrink_steps", ctx.shrink_steps)
            if event.get("found"):
                found = Mismatch(
                    seed=event.get("seed", 0),
                    size=event.get("size", 0),
                    input=event.get("input", ""),
                    expected=event.get("expected", ""),
                    actual=event.get("actual", ""),
                    reason=event.get("reason", "wrong answer"),
                )
            else:
                await ctx.note(
                    "STRESS", "gate", "skip",
                    why=event.get("reason"), rounds=ctx.rounds_run,
                )
            break

    if found is not None:
        await save_artifact(
            ctx.submission_id, "counterexample", found.input,
            meta={"seed": found.seed, "size": found.size, "reason": found.reason},
        )
    return found


# -------------------------------------------------------------------- explain


async def explain(ctx: Context, m: Mismatch) -> ExplainOutput:
    return await generate(
        ctx.submission_id,
        stage="EXPLAIN",
        system=prompts.EXPLAIN_SYSTEM,
        prompt=prompts.EXPLAIN_USER.substitute(
            language=ctx.language,
            source=ctx.source_code[:20000],
            input=m.input[:4000],
            expected=m.expected[:2000],
            actual=m.actual[:2000] or "(no output)",
            reason=m.reason,
        ),
        schema=ExplainOutput,
        temperature=0.2,
        max_output_tokens=2048,
    )


# ------------------------------------------------------------------- helpers

_DRIVER_CACHE: str | None = None


def _driver_source() -> str:
    """The driver is baked into the image, but we also ship it in at runtime so
    a driver fix does not require rebuilding and redistributing the image."""
    global _DRIVER_CACHE
    if _DRIVER_CACHE is None:
        from pathlib import Path

        path = Path(__file__).resolve().parents[3] / "sandbox" / "driver.py"
        _DRIVER_CACHE = path.read_text(encoding="utf-8")
    return _DRIVER_CACHE


def spec_summary(spec: ConstraintSpec) -> str:
    return json.dumps(spec.model_dump(), indent=2, default=str)
