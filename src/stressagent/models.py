"""Pydantic models: API payloads and LLM structured-output schemas.

The LLM schemas below are deliberately flat -- no nested unions, no Optional,
no field defaults. Gemini's structured-output mode silently drops `default` and
chokes on complex `anyOf`, so "absent" is encoded as a sentinel (-1 / "") that
the caller normalises.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .states import BugClass

# ------------------------------------------------------------------ API models


class Sample(BaseModel):
    input: str
    output: str


class SubmitRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=60_000)
    language: str = Field(pattern="^(cpp|python|java)$")
    source_code: str = Field(min_length=1, max_length=200_000)
    problem_id: str = ""
    samples: list[Sample] = Field(default_factory=list, max_length=20)
    official_tests: list[Sample] = Field(default_factory=list, max_length=200)

    # "An online judge already told me this is wrong, but I don't have the
    # failing test." Without this the samples pass, the verdict is AC, and the
    # pipeline stops before it can look for the bug the user knows is there.
    external_verdict: str = Field(default="", pattern="^(|WA|TLE|RE)$")


class Counterexample(BaseModel):
    input: str
    expected: str
    actual: str


class Report(BaseModel):
    """The four lines the user actually came for, plus provenance."""

    verdict: str
    counterexample: Counterexample | None = None
    bug_class: BugClass | None = None
    explanation: str = ""
    suggested_fix: str = ""
    rounds_run: int = 0
    shrink_steps: int = 0
    degraded_reason: str = ""


# ---------------------------------------------------- LLM: constraint analysis


class Structure(StrEnum):
    NONE = "none"
    TREE = "tree"
    CONNECTED_GRAPH = "connected_graph"
    SIMPLE_GRAPH = "simple_graph"
    PERMUTATION = "permutation"
    DISTINCT_VALUES = "distinct_values"
    SORTED_ASCENDING = "sorted_ascending"
    LOWERCASE_STRING = "lowercase_string"
    BINARY_STRING = "binary_string"
    BRACKET_SEQUENCE = "bracket_sequence"


class Variable(BaseModel):
    name: str
    min_value: int
    max_value: int
    description: str


class ConstraintSpec(BaseModel):
    """What the analyzer extracts from the statement. Everything downstream is
    conditioned on this, so it is the highest-leverage node in the pipeline."""

    multi_test: bool = Field(description="True if input begins with a test count T")
    t_max: int = Field(description="Max number of tests per file; -1 if not multi-test")

    variables: list[Variable] = Field(description="Every bound named in the statement")

    size_knob: str = Field(
        description="Name of the variable that dominates brute-force cost, e.g. 'n'"
    )
    size_knob_brute_max: int = Field(
        description="Largest value of the size knob the brute force can handle in ~1s"
    )

    structures: list[Structure] = Field(description="Structural guarantees on the input")
    sum_constraint: str = Field(
        description="e.g. 'sum of n over all tests <= 2e5'; empty string if none"
    )

    output_unique: bool = Field(
        description="False if several different outputs are equally correct; "
        "then a special-judge checker is required instead of a diff"
    )
    output_format: str = Field(description="One sentence on what to print")

    notes: str = Field(description="Anything a generator author must not get wrong")


# ------------------------------------------------------------- LLM: authoring


class AuthorOutput(BaseModel):
    """Brute force, generator and validator come from a *single* call.

    Splitting them across calls doubles the rate of the dominant failure mode:
    the two programs disagreeing on the exact input format.
    """

    brute_py: str = Field(
        description="Obviously-correct Python solution. Reads stdin, writes stdout. "
        "Exhaustive/naive is required; speed is irrelevant."
    )
    generator_py: str = Field(
        description="Python generator. Reads two argv args: seed (int) and "
        "size knob (int). Prints one valid test case to stdout. Must be "
        "deterministic given (seed, size)."
    )
    validator_py: str = Field(
        description="Python validator. Reads a test case on stdin, exits 0 if it "
        "satisfies every constraint, else exits 1 with a reason on stderr."
    )
    approach: str = Field(description="One sentence on how the brute force works")


class CheckerOutput(BaseModel):
    """Only authored when ConstraintSpec.output_unique is False."""

    checker_py: str = Field(
        description="Reads argv: input_file, contestant_output_file, brute_output_file. "
        "Exits 0 if the contestant output is a valid answer, else 1."
    )


# ------------------------------------------------------------ LLM: explanation


class ExplainOutput(BaseModel):
    bug_class: BugClass
    explanation: str = Field(description="2-4 sentences. Reference the failing input.")
    suggested_fix: str = Field(description="One or two sentences. Concrete.")
