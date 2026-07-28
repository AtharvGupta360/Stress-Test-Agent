"""Shared state for one submission's trip through the pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..db import log_step
from ..models import AuthorOutput, ConstraintSpec
from ..runner.docker_runner import LANGUAGES, Sandbox


@dataclass
class Mismatch:
    seed: int
    size: int
    input: str
    expected: str
    actual: str
    reason: str


@dataclass
class Context:
    submission_id: str
    statement: str
    language: str
    source_code: str
    samples: list[dict]
    official_tests: list[dict]
    sandbox: Sandbox

    started_at: float = field(default_factory=time.monotonic)
    spec: ConstraintSpec | None = None
    author: AuthorOutput | None = None
    checker: str = ""
    author_revision: int = 0
    rounds_run: int = 0
    shrink_steps: int = 0

    @property
    def user_cmd(self) -> list[str]:
        return LANGUAGES[self.language].run_cmd

    @property
    def out_of_time(self) -> bool:
        return time.monotonic() - self.started_at > settings().max_wall_seconds_per_submission

    async def note(
        self, stage: str, kind: str, status: str, **output: Any
    ) -> None:
        """Append to the replay log.

        This doubles as the progress channel: the API process streams SSE by
        tailing agent_steps. An in-memory callback would not work -- the API and
        the worker are different processes.
        """
        await log_step(
            self.submission_id, stage=stage, kind=kind, status=status, output=output
        )
