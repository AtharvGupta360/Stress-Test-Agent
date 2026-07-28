"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://stress:stress@localhost:5433/stressagent"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Per-submission budgets. These are hard caps: the pipeline degrades rather
    # than exceeding them, so a pathological problem statement cannot run up an
    # unbounded bill.
    max_tokens_per_submission: int = 120_000
    max_llm_calls_per_submission: int = 12
    max_wall_seconds_per_submission: int = 300

    sandbox_image: str = "stressagent/sandbox:latest"
    sandbox_cpus: float = 1.0
    sandbox_memory: str = "512m"
    sandbox_pids: int = 128
    sandbox_per_run_timeout: int = 5

    stress_rounds: int = 400
    stress_time_budget: int = 60

    worker_concurrency: int = 2
    global_sandbox_slots: int = 4

    breaker_fail_threshold: int = 5
    breaker_reset_seconds: int = 60

    # Bumped whenever prompts, gates, or the stress loop change semantics.
    # Part of the idempotency key so a fix invalidates stale cached results.
    pipeline_version: str = "1"


@lru_cache
def settings() -> Settings:
    return Settings()
