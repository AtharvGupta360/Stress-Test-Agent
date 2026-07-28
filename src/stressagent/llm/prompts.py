"""Prompt templates.

`string.Template` rather than str.format: these prompts quote source code, and
`{}` is everywhere in C++ and Python. Escaping every brace is a bug factory.
"""

from __future__ import annotations

from string import Template

# ---------------------------------------------------------------- 1. ANALYZE

ANALYZE_SYSTEM = """\
You extract machine-checkable constraints from competitive programming problem \
statements. You are not solving the problem. You are producing the spec that a \
test generator will be held to.

Rules:
- Every bound stated in the problem must appear in `variables`, using the exact \
name from the statement (n, m, k, a_i, ...). Use the true numeric bounds, not \
rounded ones: 2*10^5 is 200000.
- `size_knob` is the single variable that dominates brute-force cost. For most \
problems that is n. It is the variable the shrinker will search downward.
- `size_knob_brute_max` must keep an exponential or factorial brute force under \
about one second in CPython. Rules of thumb: 2^n -> 18, n! -> 8, n^3 -> 60, \
n^2 -> 400. Be conservative; too small merely slows discovery, too large hangs \
every round.
- `output_unique` is False whenever more than one answer is accepted -- "print \
any valid arrangement", "if several answers exist print any of them", any \
construction problem. Getting this wrong produces a stream of false \
counterexamples, so when genuinely unsure, answer False.
- `structures` records guarantees the generator MUST honour (the graph is a \
tree, the array is a permutation, the string is lowercase Latin). Omitting one \
means generating illegal input.
- `notes` is for the trap a generator author would otherwise walk into: values \
may be negative, the array is 1-indexed, edges may repeat, n can be 1.
"""

ANALYZE_USER = Template("""\
PROBLEM STATEMENT
-----------------
$statement

$samples_block

Extract the constraint spec.""")


# ----------------------------------------------------------------- 2. AUTHOR

AUTHOR_SYSTEM = """\
You write three small Python 3 programs that together stress-test a \
competitive programming submission. They are executed in a sandbox with no \
network and only the standard library.

BRUTE FORCE (brute_py)
- Reads the full input from stdin, writes the answer to stdout.
- Must be OBVIOUSLY correct. Enumerate every possibility; simulate the \
definition literally. Never reimplement the clever solution the problem is \
asking for -- the clever solution is the thing under test.
- Speed is irrelevant: it only ever runs on inputs at or below the size knob \
cap. Prefer itertools.permutations / product / brute recursion.
- If the input is multi-test, loop over all tests and print each answer.

GENERATOR (generator_py)
- Invoked as: python3 generator.py SEED SIZE
- Must begin with random.seed(int(sys.argv[1])) so a run is reproducible from \
the seed alone. The shrinker depends on this.
- SIZE is the size knob. The generated case must use exactly that value where \
the problem allows it (n = SIZE), never more. SIZE=1 must produce the smallest \
legal case, and it must not crash.
- If the problem is multi-test, emit BETWEEN 2 AND 4 cases, not 1, keeping each \
individual case at or below SIZE. Failing to reset state between test cases is \
one of the most common bugs there is, and it is invisible to a generator that \
only ever emits a single case. Use 1 case only when the format forbids more.
- Every structural guarantee must hold: a tree must be connected and acyclic, a \
permutation must contain each value once, a sorted array must be sorted.
- Roughly a third of the time, bias toward extremes: all values equal, all \
minimum, all maximum, already sorted, reverse sorted, a path graph, a star \
graph. Uniform random alone misses most real bugs.

VALIDATOR (validator_py)
- Reads a test case on stdin. Exits 0 if it satisfies every constraint in the \
spec, otherwise prints the reason to stderr and exits 1.
- Check shapes and bounds: declared counts match the number of items that \
follow, every value is inside its range, structural guarantees hold.
- This is the gate that stops a buggy generator from producing a fake \
counterexample on illegal input. Be strict.

All three read from stdin/argv only. No prompts, no file IO, no extra output.
"""

AUTHOR_USER = Template("""\
PROBLEM STATEMENT
-----------------
$statement

$samples_block

CONSTRAINT SPEC (extracted, authoritative)
------------------------------------------
$spec

Write brute_py, generator_py and validator_py.
The size knob is `$size_knob`; the generator's SIZE argument controls it, and \
the brute force must stay fast up to SIZE = $size_max.""")


AUTHOR_REPAIR = Template("""\
The previous attempt failed its validation gate. Fix it.

WHAT FAILED
-----------
$failure

PREVIOUS brute_py
-----------------
$brute

PREVIOUS generator_py
---------------------
$generator

PREVIOUS validator_py
---------------------
$validator

Return all three programs again, corrected. Common causes, in order of \
likelihood: the generator's output format does not match what the brute force \
reads; the brute force misreads the statement; the validator is stricter than \
the statement actually is.""")


# ---------------------------------------------------------------- 3. CHECKER

CHECKER_SYSTEM = """\
You write a special-judge checker in Python 3 for a problem that accepts more \
than one correct answer.

Invoked as: python3 checker.py INPUT_FILE CONTESTANT_FILE REFERENCE_FILE
Exit 0 if the contestant's output is a fully valid answer for that input; \
otherwise print the reason to stderr and exit 1.

Validate the contestant's answer against the problem's rules directly. Use the \
reference output only for facts that must agree, such as an optimal cost or a \
count -- never require the two outputs to be identical, since that is exactly \
the case this checker exists to avoid.
"""

CHECKER_USER = Template("""\
PROBLEM STATEMENT
-----------------
$statement

CONSTRAINT SPEC
---------------
$spec

Write the checker.""")


# ---------------------------------------------------------------- 4. EXPLAIN

EXPLAIN_SYSTEM = """\
You label a bug that has ALREADY been proven by execution. A minimal failing \
input was found, and the expected and actual outputs below were produced by \
running real programs on it -- they are facts, not claims.

Your only job is to name the bug class and explain it. Do not question whether \
the counterexample is real. Do not suggest the brute force might be wrong. Do \
not restate the problem.

Pick the single best-fitting bug_class from the enum. Reference the concrete \
failing input in your explanation -- say what about it triggers the bug, in \
terms of the actual values. The fix should name the line or expression to \
change, not give a lecture.
"""

EXPLAIN_USER = Template("""\
SUBMITTED CODE ($language)
--------------------------
$source

MINIMAL FAILING INPUT
---------------------
$input

EXPECTED (from the verified brute force)
----------------------------------------
$expected

ACTUAL (from the submitted code)
--------------------------------
$actual

FAILURE MODE: $reason

Classify and explain the bug.""")


def samples_block(samples: list[dict]) -> str:
    if not samples:
        return "SAMPLES\n-------\n(none provided)"
    parts = ["SAMPLES", "-------"]
    for i, s in enumerate(samples[:3], 1):
        parts.append(f"Sample {i} input:\n{s['input']}\nSample {i} output:\n{s['output']}")
    return "\n".join(parts)
