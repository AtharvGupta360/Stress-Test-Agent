"""Pipeline states and the legal transitions between them.

The orchestration is a state machine, not a free-running agent loop. The model
is called at specific nodes; the edges are ordinary code. That is what makes a
run replayable and what keeps the token budget deterministic.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    SUBMITTED = "SUBMITTED"
    COMPILING = "COMPILING"
    JUDGING = "JUDGING"
    ANALYZING = "ANALYZING"
    AUTHORING = "AUTHORING"
    VALIDATING = "VALIDATING"
    STRESSING = "STRESSING"
    SHRINKING = "SHRINKING"
    EXPLAINING = "EXPLAINING"
    DONE = "DONE"
    FAILED = "FAILED"
    # Model API unavailable or budget exhausted: we still return the verdict,
    # just without the counterexample. Cached with a short TTL so a later retry
    # can upgrade the result.
    DEGRADED = "DEGRADED"


TERMINAL: frozenset[State] = frozenset({State.DONE, State.FAILED, State.DEGRADED})

# Every node may also fall to FAILED or DEGRADED; those edges are implicit.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.SUBMITTED: frozenset({State.COMPILING}),
    State.COMPILING: frozenset({State.JUDGING, State.DONE}),      # DONE on CE
    State.JUDGING: frozenset({State.ANALYZING, State.DONE}),      # DONE unless WA
    State.ANALYZING: frozenset({State.AUTHORING}),
    State.AUTHORING: frozenset({State.VALIDATING}),
    State.VALIDATING: frozenset({State.STRESSING, State.AUTHORING}),  # loop = repair
    State.STRESSING: frozenset({State.SHRINKING, State.DONE}),    # DONE if no mismatch
    State.SHRINKING: frozenset({State.EXPLAINING}),
    State.EXPLAINING: frozenset({State.DONE}),
}


def can_transition(src: State, dst: State) -> bool:
    if dst in (State.FAILED, State.DEGRADED):
        return src not in TERMINAL
    return dst in TRANSITIONS.get(src, frozenset())


class Verdict(StrEnum):
    AC = "AC"
    WA = "WA"
    TLE = "TLE"
    RE = "RE"
    CE = "CE"
    MLE = "MLE"


class BugClass(StrEnum):
    """Fixed taxonomy. The explainer picks from this list and cannot invent a
    label -- the counterexample itself is already proven by execution, so the
    only thing left to constrain is the wording."""

    INTEGER_OVERFLOW = "integer_overflow"
    OFF_BY_ONE = "off_by_one"
    WRONG_GREEDY = "wrong_greedy"
    MISSING_EDGE_CASE = "missing_edge_case"
    UNRESET_GLOBAL_STATE = "unreset_global_state"
    ARRAY_OUT_OF_BOUNDS = "array_out_of_bounds"
    WRONG_TIE_BREAK = "wrong_tie_break"
    PRECISION_ERROR = "precision_error"
    WRONG_COMPARATOR = "wrong_comparator"
    INCORRECT_RECURRENCE = "incorrect_recurrence"
    OUTPUT_FORMAT = "output_format"
    LOGIC_ERROR = "logic_error"
