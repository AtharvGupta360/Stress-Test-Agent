#!/usr/bin/env python3
"""In-container stress driver.

This is the piece that makes the whole thing viable. Orchestrating the stress
loop from outside would mean one `docker run` per round: 400 rounds x 3
executions x ~300ms cold start is ~6 minutes of pure Docker overhead before any
real work happens.

So the loop lives *inside* one long-lived container. The cgroup caps (cpu,
memory, pids, no network) apply to the container; the loop is internal and
costs a fork per execution instead of a container start.

Emits JSON Lines on stdout so the worker can stream progress to the client.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from dataclasses import dataclass

PER_PROC_MEMORY_BYTES = 256 * 1024 * 1024


def emit(**payload: object) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _limit_child() -> None:
    """Cap the child independently of the container.

    The container cap protects the host; this protects the *loop* -- without it
    a single runaway allocation OOM-kills the container and we lose the whole
    submission instead of one round.
    """
    resource.setrlimit(resource.RLIMIT_AS, (PER_PROC_MEMORY_BYTES, PER_PROC_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))


@dataclass
class RunResult:
    stdout: str
    stderr: str
    code: int
    timed_out: bool
    wall_ms: int


def run(cmd: list[str], stdin_data: str = "", timeout: float = 5.0) -> RunResult:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=_limit_child,
            cwd="/work",
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            stdout=(exc.stdout or b"").decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            stderr="timeout",
            code=-9,
            timed_out=True,
            wall_ms=int((time.monotonic() - started) * 1000),
        )
    return RunResult(
        stdout=proc.stdout,
        stderr=proc.stderr[-4000:],
        code=proc.returncode,
        timed_out=False,
        wall_ms=int((time.monotonic() - started) * 1000),
    )


def tokens(text: str) -> list[str]:
    """Token comparison, the same rule real judges use.

    Differences in trailing newlines or inter-token spacing are not bugs, and
    reporting them as counterexamples is the fastest way to lose a user's trust.
    """
    return text.split()


# --------------------------------------------------------------------- stages


def gen_input(seed: int, size: int, timeout: float) -> RunResult:
    return run(["python3", "generator.py", str(seed), str(size)], timeout=timeout)


def validate_input(data: str, timeout: float) -> RunResult | None:
    if not os.path.exists("/work/validator.py"):
        return None
    return run(["python3", "validator.py"], stdin_data=data, timeout=timeout)


def check_outputs(
    test_input: str, user_out: str, brute_out: str, timeout: float
) -> bool:
    """True when the user's output is acceptable.

    With a checker present we cannot diff -- several answers may be equally
    correct, and diffing would report false counterexamples on every one of them.
    """
    if os.path.exists("/work/checker.py"):
        for name, content in (
            ("_in.txt", test_input),
            ("_user.txt", user_out),
            ("_brute.txt", brute_out),
        ):
            with open(f"/work/{name}", "w") as fh:
                fh.write(content)
        res = run(
            ["python3", "checker.py", "_in.txt", "_user.txt", "_brute.txt"],
            timeout=timeout,
        )
        return res.code == 0
    return tokens(user_out) == tokens(brute_out)


@dataclass
class Mismatch:
    seed: int
    size: int
    test_input: str
    expected: str
    actual: str
    reason: str


def probe(
    seed: int, size: int, user_cmd: list[str], timeout: float
) -> Mismatch | None:
    """One round. Returns a Mismatch if this (seed, size) exposes a disagreement."""
    gen = gen_input(seed, size, timeout)
    if gen.code != 0 or not gen.stdout.strip():
        return None  # generator hiccup on this seed; not the user's problem

    test_input = gen.stdout

    val = validate_input(test_input, timeout)
    if val is not None and val.code != 0:
        # The generator produced out-of-bounds input. Skip it -- reporting a bug
        # found on illegal input is worse than reporting nothing.
        return None

    brute = run(["python3", "brute.py"], stdin_data=test_input, timeout=timeout * 3)
    if brute.timed_out or brute.code != 0:
        return None  # brute too slow / broken at this size; caller shrinks size

    user = run(user_cmd, stdin_data=test_input, timeout=timeout)
    if user.timed_out:
        return Mismatch(seed, size, test_input, brute.stdout, "", "timeout")
    if user.code != 0:
        return Mismatch(
            seed, size, test_input, brute.stdout, user.stdout,
            f"runtime error (exit {user.code}): {user.stderr[:200]}",
        )
    if not check_outputs(test_input, user.stdout, brute.stdout, timeout):
        return Mismatch(seed, size, test_input, brute.stdout, user.stdout, "wrong answer")
    return None


def shrink_candidates(found_size: int) -> list[int]:
    """Sizes to try, smallest first.

    Scanning 1..found_size linearly is fine when the bug was found at size 8 and
    hopeless when it was found at 50000. Sweep the small sizes exhaustively --
    that is where an answer is worth having -- then climb geometrically, so the
    number of probes grows with the logarithm of the size rather than the size.
    """
    sizes = [s for s in range(1, min(found_size, 31))]
    step = max(31, 1)
    while step < found_size:
        sizes.append(step)
        step = max(step + 1, int(step * 1.5))
    return [s for s in sizes if s < found_size]


def shrink(
    found: Mismatch, user_cmd: list[str], timeout: float, seeds_per_size: int, deadline: float
) -> tuple[Mismatch, int]:
    """Seed search, not delta debugging.

    Generic ddmin on input bytes produces *invalid* input -- drop a line and the
    declared n no longer matches the array length. Instead we re-run the
    generator at smaller sizes: every candidate is valid by construction, and
    the result is reproducible from (seed, size) alone.
    """
    best = found
    steps = 0
    for size in shrink_candidates(found.size):
        if time.monotonic() > deadline:
            break
        for k in range(seeds_per_size):
            if time.monotonic() > deadline:
                break
            steps += 1
            candidate = probe(found.seed * 7919 + k, size, user_cmd, timeout)
            if candidate is not None:
                emit(event="shrink", size=size, seed=candidate.seed)
                return candidate, steps
    return best, steps


# ----------------------------------------------------------------------- main


SMALL_SWEEP_FRACTION = 0.5


def pick_size(i: int, size_min: int, size_max: int, rounds: int) -> int:
    """Choose the size knob for round i.

    Two phases, because bugs cluster in two very different places.

    The first half sweeps the smallest sizes *densely and exhaustively* -- every
    value from size_min upward, one per round, no repeats. Dense matters: some
    failure sets are sparse and exact, like a carry bug that only fires at
    n = 199, 399, 599. Sampling near those values is worthless; you have to land
    on them. Sweeping consecutively is the only way to guarantee that, and it
    costs nothing because small cases are the cheapest to run.

    The second half samples log-uniformly across the whole legal range, so a bug
    that only appears at magnitude -- an accumulator overflowing at 10^5 -- is
    still reachable without spending 100000 rounds ramping up to it.
    """
    if size_max <= size_min:
        return size_min

    span = size_max - size_min + 1
    sweep_rounds = max(1, int(rounds * SMALL_SWEEP_FRACTION))
    if i < sweep_rounds:
        return size_min + (i % min(span, sweep_rounds))

    # Deterministic in i, so a run stays reproducible without touching random().
    frac = ((i * 2654435761) % 10_000) / 10_000.0
    lo = max(size_min, 1)
    size = int(round(lo * ((size_max / lo) ** frac)))
    return max(size_min, min(size, size_max))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-cmd", required=True, help="shell-free argv, space separated")
    ap.add_argument("--rounds", type=int, default=400)
    ap.add_argument("--size-min", type=int, default=1)
    ap.add_argument("--size-max", type=int, default=8)
    ap.add_argument("--seed-base", type=int, default=1)
    ap.add_argument("--time-budget", type=float, default=60.0)
    ap.add_argument("--per-run-timeout", type=float, default=5.0)
    ap.add_argument("--seeds-per-size", type=int, default=25)
    args = ap.parse_args()

    user_cmd = args.user_cmd.split()
    deadline = time.monotonic() + args.time_budget

    for i in range(args.rounds):
        if time.monotonic() > deadline:
            emit(event="done", found=False, rounds=i, reason="time_budget")
            return 0

        size = pick_size(i, args.size_min, args.size_max, args.rounds)
        seed = args.seed_base + i

        found = probe(seed, size, user_cmd, args.per_run_timeout)
        if i % 25 == 0:
            emit(event="progress", round=i, rounds=args.rounds)

        if found is not None:
            emit(event="mismatch", seed=found.seed, size=found.size, reason=found.reason)
            minimal, steps = shrink(
                found, user_cmd, args.per_run_timeout, args.seeds_per_size,
                min(deadline + 30, time.monotonic() + 30),
            )
            emit(
                event="done",
                found=True,
                rounds=i + 1,
                shrink_steps=steps,
                reason=minimal.reason,
                seed=minimal.seed,
                size=minimal.size,
                input=minimal.test_input,
                expected=minimal.expected,
                actual=minimal.actual,
            )
            return 0

    emit(event="done", found=False, rounds=args.rounds, reason="exhausted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
