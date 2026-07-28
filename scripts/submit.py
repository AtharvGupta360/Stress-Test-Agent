#!/usr/bin/env python3
"""Submit your own problem and solution.

Point it at a problem directory:

    problems/my-problem/
        statement.txt        the problem text, including the constraints
        solution.cpp         your submission (.cpp / .py / .java)
        samples/
            1.in  1.out
            2.in  2.out
        tests/               optional: hidden/official tests, same naming
            1.in  1.out

    python scripts/submit.py problems/my-problem

If your code passes every test you have but an online judge says it is wrong,
add --judge-says WA. That is the usual case: you have the samples, not the test
that actually fails.

    python scripts/submit.py problems/my-problem --judge-says WA
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

LANG_BY_SUFFIX = {".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".py": "python", ".java": "java"}


def load_tests(directory: Path) -> list[dict]:
    """Read `1.in`/`1.out` pairs, ordered numerically where possible."""
    if not directory.is_dir():
        return []

    def sort_key(p: Path) -> tuple[int, str]:
        return (int(p.stem), "") if p.stem.isdigit() else (1 << 30, p.stem)

    tests = []
    for infile in sorted(directory.glob("*.in"), key=sort_key):
        outfile = infile.with_suffix(".out")
        if not outfile.exists():
            print(f"  warning: {infile.name} has no matching .out, skipping", file=sys.stderr)
            continue
        if not infile.read_text(encoding="utf-8").strip():
            continue  # unfilled scaffold slot
        tests.append(
            {
                "input": infile.read_text(encoding="utf-8"),
                "output": outfile.read_text(encoding="utf-8"),
            }
        )
    return tests


def find_solution(root: Path, explicit: Path | None) -> tuple[str, str]:
    if explicit is not None:
        path = explicit
    else:
        candidates = [p for p in root.iterdir() if p.suffix in LANG_BY_SUFFIX]
        if not candidates:
            raise SystemExit(f"no solution file (.cpp/.py/.java) found in {root}")
        if len(candidates) > 1:
            names = ", ".join(p.name for p in sorted(candidates))
            raise SystemExit(f"several solution files in {root} ({names}); pass --code")
        path = candidates[0]

    language = LANG_BY_SUFFIX.get(path.suffix)
    if language is None:
        raise SystemExit(f"unsupported language for {path.name}")
    return language, path.read_text(encoding="utf-8")


def build_payload(args: argparse.Namespace) -> dict:
    root: Path = args.problem

    statement_path = args.statement or root / "statement.txt"
    if not statement_path.exists():
        raise SystemExit(f"no statement at {statement_path}")
    statement = statement_path.read_text(encoding="utf-8")

    language, source = find_solution(root, args.code)

    # Refuse scaffolding that was never filled in. Submitting placeholder text
    # spends real tokens producing a confidently useless report.
    for label, text in (("statement.txt", statement), ("the solution file", source)):
        if "PASTE_HERE" in text:
            raise SystemExit(f"{label} still contains the placeholder -- paste your content first")

    samples = load_tests(root / "samples")
    official = load_tests(root / "tests")

    if not samples and not official:
        print(
            "  warning: no samples found. Gate 1 cannot verify the reference\n"
            "           implementation, so results will be less reliable.",
            file=sys.stderr,
        )

    print(f"  language   {language}")
    print(f"  samples    {len(samples)}")
    print(f"  tests      {len(official)}")
    if args.judge_says:
        print(f"  judge says {args.judge_says} (overrides a local pass)")

    return {
        "statement": statement,
        "language": language,
        "source_code": source,
        "problem_id": args.problem_id or root.name,
        "samples": samples,
        "official_tests": official,
        "external_verdict": args.judge_says,
    }


def wait(api: str, submission_id: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        body = httpx.get(f"{api}/submissions/{submission_id}", timeout=15).json()
        if body["state"] != last:
            print(f"  [{body['state']}]", flush=True)
            last = body["state"]
        if body["state"] in ("DONE", "FAILED", "DEGRADED"):
            return body
        time.sleep(1.5)
    raise SystemExit("timed out waiting for the submission to finish")


def show(body: dict) -> None:
    result = body.get("result") or {}
    print(f"\nstate      {body['state']}")
    print(f"verdict    {body['verdict']}")
    print(f"tokens     {body['tokens_used']} across {body['llm_calls']} model calls")

    ce = result.get("counterexample")
    if not ce:
        print(f"\n{result.get('explanation') or result.get('degraded_reason') or '(no report)'}")
        return

    print(
        f"\nrounds     {result.get('rounds_run')} "
        f"(shrink steps: {result.get('shrink_steps')})"
    )
    print("\n--- counterexample ---")
    print(f"input:\n{ce['input'].rstrip()}")
    print(f"\nexpected:  {ce['expected'].strip()}")
    print(f"actual:    {ce['actual'].strip() or '(no output)'}")
    print(f"bug class: {result.get('bug_class')}")
    if result.get("explanation"):
        print(f"\nwhy:       {result['explanation']}")
        print(f"fix:       {result.get('suggested_fix')}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Submit your own problem to the Stress-Test Agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("problem", type=Path, help="problem directory")
    ap.add_argument("--code", type=Path, help="solution file (default: the only one found)")
    ap.add_argument("--statement", type=Path, help="statement file (default: statement.txt)")
    ap.add_argument("--problem-id", default="", help="defaults to the directory name")
    ap.add_argument(
        "--judge-says",
        default="",
        choices=["", "WA", "TLE", "RE"],
        help="an online judge already rejected this, but you lack the failing test",
    )
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--steps", action="store_true", help="print the replay log")
    ap.add_argument("--artifacts", action="store_true", help="print the generated programs")
    args = ap.parse_args()

    if not args.problem.is_dir():
        raise SystemExit(f"{args.problem} is not a directory")

    print(f"reading {args.problem}")
    payload = build_payload(args)

    try:
        r = httpx.post(f"{args.api}/submissions", json=payload, timeout=30)
    except httpx.ConnectError:
        raise SystemExit(f"cannot reach the API at {args.api} -- is the server running?") from None
    if r.status_code >= 400:
        raise SystemExit(f"submit failed: HTTP {r.status_code}\n{r.text}")

    body = r.json()
    submission_id = body["id"]
    if not body["created"]:
        print(f"\nidentical submission already judged ({submission_id[:8]}), returning cache")
        show(httpx.get(f"{args.api}/submissions/{submission_id}", timeout=15).json())
        return 0

    print(f"\nsubmitted {submission_id}")
    show(wait(args.api, submission_id, args.timeout))

    if args.steps:
        steps = httpx.get(f"{args.api}/submissions/{submission_id}/steps", timeout=15).json()
        print("\n--- replay log ---")
        for s in steps["steps"]:
            out = json.dumps(s["output"])[:100]
            print(f"{s['seq']:>3} {s['stage']:<9} {s['kind']:<11} {s['status']:<5} {out}")

    if args.artifacts:
        arts = httpx.get(f"{args.api}/submissions/{submission_id}/artifacts", timeout=15).json()
        for a in arts["artifacts"]:
            print(f"\n--- {a['kind']} (revision {a['revision']}) ---")
            print(a["content"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
