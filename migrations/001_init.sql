-- Stress-Test Agent :: initial schema
--
-- Design notes:
--   * `submissions` doubles as the job queue (FOR UPDATE SKIP LOCKED). Keeping
--     the queue in Postgres means a state transition and its audit row commit in
--     the same transaction -- no dual-store drift between Redis and the log.
--   * `agent_steps` is append-only. It is the replay log: every LLM call, every
--     sandbox execution, every gate decision, in order.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- submissions

CREATE TABLE submissions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- hash(source, problem_id, language, pipeline_version). Resubmitting
    -- identical code is free: we return the cached row.
    idempotency_key   TEXT NOT NULL UNIQUE,

    problem_id        TEXT,
    statement         TEXT NOT NULL,
    language          TEXT NOT NULL,
    source_code       TEXT NOT NULL,

    -- [{"input": "...", "output": "..."}, ...] from the problem statement.
    samples           JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Official/hidden tests, same shape. May be empty (then we rely on samples
    -- for the pre-gate and go straight to stress testing).
    official_tests    JSONB NOT NULL DEFAULT '[]'::jsonb,

    state             TEXT NOT NULL DEFAULT 'SUBMITTED',
    verdict           TEXT,                    -- AC / WA / TLE / RE / CE / MLE
    result            JSONB,                   -- final user-facing report
    error             TEXT,

    -- Budget accounting, enforced by the pipeline.
    tokens_used       INTEGER NOT NULL DEFAULT 0,
    llm_calls         INTEGER NOT NULL DEFAULT 0,

    attempts          INTEGER NOT NULL DEFAULT 0,
    -- Worker lease. A crashed worker's rows become claimable once this passes.
    lease_expires_at  TIMESTAMPTZ,
    worker_id         TEXT,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);

-- Terminal states never appear in this index, so the queue scan stays small
-- even as the table grows to millions of finished submissions.
CREATE INDEX submissions_queue_idx
    ON submissions (created_at)
    WHERE state NOT IN ('DONE', 'FAILED', 'DEGRADED');

CREATE INDEX submissions_created_idx ON submissions (created_at DESC);

-- ---------------------------------------------------------------- agent_steps

CREATE TABLE agent_steps (
    id             BIGSERIAL PRIMARY KEY,
    submission_id  UUID NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,

    stage          TEXT NOT NULL,   -- ANALYZE / AUTHOR / VALIDATE / STRESS / ...
    kind           TEXT NOT NULL,   -- llm_call / sandbox_run / gate / transition
    status         TEXT NOT NULL,   -- ok / fail / skip

    -- For llm_call we store the prompt hash rather than the full prompt; the
    -- prompt is reconstructible from (pipeline_version, stage, payload).
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    output         JSONB NOT NULL DEFAULT '{}'::jsonb,

    tokens         INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (submission_id, seq)
);

CREATE INDEX agent_steps_submission_idx ON agent_steps (submission_id, seq);

-- ------------------------------------------------------------------ artifacts

-- Generated source (brute force, generator, checker) and the counterexample.
-- Counterexamples are stored as (generator_seed, size_knob) where possible --
-- a pre-shrink input can be megabytes, a seed is 8 bytes.
CREATE TABLE artifacts (
    id             BIGSERIAL PRIMARY KEY,
    submission_id  UUID NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,   -- brute / generator / checker / counterexample
    revision       INTEGER NOT NULL DEFAULT 0,   -- bumped by the repair loop
    content        TEXT NOT NULL,
    meta           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (submission_id, kind, revision)
);

CREATE INDEX artifacts_submission_idx ON artifacts (submission_id);

-- ------------------------------------------------------------------- triggers

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER submissions_touch
    BEFORE UPDATE ON submissions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
