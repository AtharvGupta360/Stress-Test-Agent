#!/usr/bin/env python3
"""Submit a known-buggy solution and print the report.

The planted bug is a 32-bit accumulator. The samples pass, and the failing
official test is deliberately too large to read, so the run has to go through
the full path: analyze -> author -> validate -> stress -> shrink -> explain.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time

import httpx

API = "http://127.0.0.1:8000"

STATEMENT = """\
Given an array of n integers, print their sum.

Input
The first line contains a single integer n (1 <= n <= 100000).
The second line contains n integers a_1, a_2, ..., a_n (1 <= a_i <= 10^9).

Output
Print one integer: the sum of the array.
"""

BUGGY_CPP = """\
#include <iostream>

int main() {
    int n;
    std::cin >> n;
    int sum = 0;
    for (int i = 0; i < n; i++) {
        int x;
        std::cin >> x;
        sum += x;
    }
    std::cout << sum << std::endl;
    return 0;
}
"""

CORRECT_CPP = BUGGY_CPP.replace("int sum = 0;", "long long sum = 0;")


def big_failing_test() -> dict:
    random.seed(7)
    values = [random.randint(9 * 10**8, 10**9) for _ in range(50)]
    return {
        "input": f"{len(values)}\n{' '.join(map(str, values))}\n",
        "output": f"{sum(values)}\n",
    }


def submit(source: str) -> str:
    payload = {
        "statement": STATEMENT,
        "language": "cpp",
        "source_code": source,
        "problem_id": f"demo-sum-{int(time.time())}",
        "samples": [{"input": "3\n1 2 3\n", "output": "6\n"}],
        "official_tests": [
            {"input": "3\n1 2 3\n", "output": "6\n"},
            big_failing_test(),
        ],
    }
    r = httpx.post(f"{API}/submissions", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def wait(submission_id: str, timeout: float = 420.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        r = httpx.get(f"{API}/submissions/{submission_id}", timeout=15)
        r.raise_for_status()
        body = r.json()
        if body["state"] != last:
            print(f"  [{body['state']}]", flush=True)
            last = body["state"]
        if body["state"] in ("DONE", "FAILED", "DEGRADED"):
            return body
        time.sleep(1.5)
    raise TimeoutError("submission did not finish in time")


def show(body: dict) -> None:
    result = body.get("result") or {}
    print(f"\nstate      {body['state']}")
    print(f"verdict    {body['verdict']}")
    print(f"tokens     {body['tokens_used']} across {body['llm_calls']} model calls")

    ce = result.get("counterexample")
    if ce:
        print(
            f"\nrounds     {result.get('rounds_run')} "
            f"(shrink steps: {result.get('shrink_steps')})"
        )
        print("\n--- counterexample ---")
        print(f"input:     {ce['input'].strip()}")
        print(f"expected:  {ce['expected'].strip()}")
        print(f"actual:    {ce['actual'].strip()}")
        print(f"bug class: {result.get('bug_class')}")
        print(f"\nwhy:       {result.get('explanation')}")
        print(f"fix:       {result.get('suggested_fix')}")
    else:
        print(f"\n{result.get('explanation') or result.get('degraded_reason')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--correct", action="store_true", help="submit the fixed version instead")
    ap.add_argument("--steps", action="store_true", help="print the replay log")
    args = ap.parse_args()

    source = CORRECT_CPP if args.correct else BUGGY_CPP
    print("submitting", "correct" if args.correct else "buggy", "solution...")
    submission_id = submit(source)
    print(f"id {submission_id}")

    body = wait(submission_id)
    show(body)

    if args.steps:
        r = httpx.get(f"{API}/submissions/{submission_id}/steps", timeout=15)
        print("\n--- replay log ---")
        for s in r.json()["steps"]:
            out = json.dumps(s["output"])[:110]
            print(f"{s['seq']:>3} {s['stage']:<9} {s['kind']:<11} {s['status']:<5} {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
