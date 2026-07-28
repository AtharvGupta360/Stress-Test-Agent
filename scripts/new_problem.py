#!/usr/bin/env python3
"""Scaffold an empty problem directory, ready to paste into.

    python scripts/new_problem.py my-problem
    python scripts/new_problem.py my-problem --lang python --samples 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PLACEHOLDER = "PASTE_HERE"

SUFFIX = {"cpp": ".cpp", "python": ".py", "java": ".java"}

STATEMENT = f"""\
{PLACEHOLDER} -- replace this whole file with the problem statement.

Keep the constraints section. It is the most important part of the file: the
bounds (1 <= n <= 100000, 1 <= a_i <= 10^9, and so on) are what the generator is
held to. Without them it produces illegal inputs and the results are worthless.

Keep the input and output format description too.
"""

CODE = {
    "cpp": f"// {PLACEHOLDER} -- replace with your submission.\n",
    "python": f"# {PLACEHOLDER} -- replace with your submission.\n",
    "java": f"// {PLACEHOLDER} -- replace with your submission.\n",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name", help="problem directory name")
    ap.add_argument("--lang", default="cpp", choices=sorted(SUFFIX))
    ap.add_argument("--samples", type=int, default=1, help="how many sample slots to create")
    ap.add_argument("--root", type=Path, default=Path("problems"))
    args = ap.parse_args()

    root: Path = args.root / args.name
    if root.exists():
        raise SystemExit(f"{root} already exists")

    (root / "samples").mkdir(parents=True)
    (root / "statement.txt").write_text(STATEMENT, encoding="utf-8")
    (root / f"solution{SUFFIX[args.lang]}").write_text(CODE[args.lang], encoding="utf-8")

    for i in range(1, args.samples + 1):
        (root / "samples" / f"{i}.in").write_text("", encoding="utf-8")
        (root / "samples" / f"{i}.out").write_text("", encoding="utf-8")

    print(f"created {root}\n")
    print(f"  {root / 'statement.txt'}          <- paste the problem statement")
    print(f"  {root / ('solution' + SUFFIX[args.lang])}          <- paste your code")
    for i in range(1, args.samples + 1):
        print(f"  {root / 'samples' / f'{i}.in'}         <- paste sample input {i}")
        print(f"  {root / 'samples' / f'{i}.out'}        <- paste expected output {i}")

    print(f"\nthen:  python scripts/submit.py {root.as_posix()} --judge-says WA")
    return 0


if __name__ == "__main__":
    sys.exit(main())
