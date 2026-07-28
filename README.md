# Stress-Test Agent

A debugging judge for competitive programming submissions.

You submit code. It compiles and runs against the official tests in a sandboxed
container. If the verdict is **Wrong Answer**, an LLM takes over — but it never
guesses the bug. It writes two programs instead: an obviously-correct brute
force and a randomized generator that respects the problem's constraints. The
backend then stress-tests your submission against the brute force until they
disagree, and shrinks that input to a minimal counterexample.

You get back four lines:

```
input:     3
           1 1000000000 1000000000
expected:  2000000001
actual:    -2147483647
bug class: integer_overflow
```

**The model proposes; execution verifies.** The sandbox is the oracle, so a
hallucinated explanation cannot reach the user.

---

## Architecture

Orchestration is a **state machine**, not a free-running agent loop. The model
is called at three nodes and nowhere else, which is what makes a run replayable
and the token budget deterministic.

```
SUBMITTED → COMPILE → JUDGE ─┬─ AC / CE / TLE / RE ──────────────→ DONE
                             └─ WA → ANALYZE → AUTHOR → VALIDATE
                                      → STRESS → SHRINK → EXPLAIN → DONE
                     (any node) ──────────────────→ DEGRADED / FAILED
```

| Node | LLM? | Output | Verified by |
|---|---|---|---|
| ANALYZE | yes | constraint spec (bounds, structure, size knob) | JSON schema |
| AUTHOR | yes | brute force **+** generator **+** validator, one call | the gates below |
| CHECKER | conditional | special judge, only if several answers are valid | samples |
| STRESS | no | a disagreement | execution |
| EXPLAIN | yes | bug class from a fixed enum | counterexample already proven |

Brute force and generator come from a **single call** — their dominant failure
mode is disagreeing on the exact input format, and one call sees both sides.

### The three gates

Nothing downstream is trusted until these pass.

- **Gate 0** — before any model call, run the submission on the provided tests.
  If it already fails one, that *is* the counterexample. Zero tokens.
- **Gate 1** — the brute force must reproduce the official samples. A reference
  implementation that can't match the samples is just a second buggy program.
- **Gate 2** — every generated input must pass a validator. Without this you
  eventually report a "bug" found on illegal input, and one false positive costs
  more trust than ten correct finds.

### Why the stress loop runs *inside* the container

Orchestrating rounds from outside means one `docker run` per round: 400 rounds ×
3 executions × ~300 ms cold start ≈ 6 minutes of pure Docker overhead before any
real work. So one container stays warm for the whole submission and the loop
lives inside it ([sandbox/driver.py](sandbox/driver.py)). The cgroup caps apply
to the container; each round costs a fork.

### Shrinking is seed search, not delta debugging

Generic ddmin on input bytes produces *invalid* input — drop a line and the
declared `n` no longer matches the array length. Instead the shrinker re-runs
the generator at smaller sizes: every candidate is valid by construction, and
the result is reproducible from `(seed, size)` alone. A pre-shrink input can be
megabytes; a seed is 8 bytes.

---

## Layout

```
migrations/001_init.sql          submissions (doubles as the queue), agent_steps, artifacts
sandbox/Dockerfile               judge image: g++, python3, jdk, unprivileged
sandbox/driver.py                the in-container stress + shrink loop
src/stressagent/
  config.py                      env-backed settings and budgets
  states.py                      states, legal transitions, bug taxonomy
  models.py                      API payloads + LLM structured-output schemas
  db.py                          asyncpg, SKIP LOCKED queue, replay log
  llm/client.py                  Gemini, budget enforcement, circuit breaker
  llm/prompts.py                 the three prompts + the repair prompt
  pipeline/stages.py             compile, judge, analyze, author, validate, stress, explain
  pipeline/run.py                the state-machine executor
  runner/docker_runner.py        warm sandbox: caps, file injection, JSONL streaming
  worker.py                      claim → run → repeat
  api/main.py                    submit, poll, SSE, replay
```

---

## Running it

```bash
cp .env.example .env      # set GEMINI_API_KEY
docker build -t stressagent/sandbox:latest sandbox/
docker compose up -d db
python scripts/migrate.py
docker compose up api worker
```

Submit:

```bash
curl -s localhost:8000/submissions -H 'content-type: application/json' -d '{
  "statement": "Given n integers, print their sum. 1 <= n <= 100000, 1 <= a_i <= 10^9.",
  "language": "cpp",
  "source_code": "#include <bits/stdc++.h>\nint main(){int n;std::cin>>n;int s=0;for(int i=0;i<n;i++){int x;std::cin>>x;s+=x;}std::cout<<s;}",
  "samples": [{"input": "3\n1 2 3\n", "output": "6\n"}]
}'
```

Then `GET /submissions/{id}` for the report, `/stream` for live progress, and
`/steps` for the replay log — every model call, every sandbox run, in order.

---

## Operational properties

- **Idempotency** — key is `sha256(source, problem_id, language, pipeline_version)`.
  The pipeline version is in the hash so shipping a prompt fix invalidates stale
  cached reports instead of serving them forever.
- **Budgets** — hard per-submission caps on tokens, model calls and wall clock.
  Exceeding one degrades the run; it never runs up an unbounded bill.
- **Circuit breaker** — process-wide. Once the model API starts failing, every
  submission degrades to verdict-only immediately rather than each discovering
  the outage on its own. `DEGRADED` results are re-runnable on resubmission.
- **Crash recovery** — jobs are leased, not dequeued. A killed worker's job
  becomes claimable when the lease lapses; `attempts` bounds the retries so a
  submission that reliably kills workers can't loop forever.
- **Isolation** — no network, read-only rootfs, tmpfs `/work`, `--cap-drop ALL`,
  `no-new-privileges`, CPU/memory/PID caps, plus per-process `RLIMIT_AS` inside
  the loop so one bad round can't OOM the container and lose the submission.
