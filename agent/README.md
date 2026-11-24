# agent/ — benchmark runner for Claude Code

Infrastructure for running the benchmark's tasks: launch **Claude Code** at a solver, confined so it
can only modify that solver and cannot reach the network, log everything, then grade the result with the
frozen referee (`scripts/bench.py` + the proof checkers). It does **not** orchestrate agents or encode
how to solve a task — it provides the backend, the sandbox, the logging, and the authoritative grade.

## Modules

| file | what it is |
|---|---|
| `backend.py` | Runs the `claude` CLI headless (`--print --output-format stream-json`). Auth resolution (`auto`/`api-key`/`subscription`/`bedrock`), argv build, synchronous run with a wall-clock timeout, stream-json parsing (final text, session id). Stdlib only. |
| `sandbox.py` | `FsPolicy` + an OS filesystem confinement wrapper (macOS Seatbelt / Linux bubblewrap) and the network tool-policy. |
| `metrics.py` | stream-json → cost/token/tool rollup; writes `summary.json` + `COST.md`. |
| `grade.py` | **Authoritative** grader (runs unsandboxed): rebuild the solver, run the referee, score vs Z3, apply the soundness gate. The single source of truth for "is it better". |
| `run.py` | Orchestrates one task: provenance → sandbox → launch → artifacts → grade. |

## Usage

```sh
# establish the Z3 reference once (grading compares against it)
python3 scripts/bench.py run z3 --split dev

# run a task (model/auth/network/budget are flags; the stop criterion is --timeout)
python3 -m agent.run --solver sundance --model claude-opus-4-8 --timeout 3600

# grade a solver's current state by hand (same grade run.py does after a task)
python3 -m agent.grade sundance          # dev + holdout; --no-holdout for a quick dev-only check
```

Auth is auto-detected: `ANTHROPIC_API_KEY` → api-key, else `CLAUDE_CODE_OAUTH_TOKEN` → subscription, else
`CLAUDE_CODE_USE_BEDROCK=1` → bedrock, else the `claude` CLI's stored login. Force it with `--auth`.

## What a run produces — `runs/<ts>-<model>-<solver>-<tag>/`

- `meta.json` — provenance (solver base commit, task-file SHA-256, platform, CLI version), config, the
  exact argv, the sandbox policy, and (after grading) the verdict.
- `transcript.jsonl` — the full stream-json event log (flushed live; survives a crash).
- `summary.json` / `COST.md` — tokens, cost, turns, tool-use counts.
- `result.md` — the agent's final message (its self-report; **advisory only**).
- `patch.diff` / `changed_files.txt` — the solver diff vs its base commit.
- `grade/` + `grade.json` — the authoritative rebuild + referee re-measure: the L1–L4 verdict, the mean
  gap to Z3, per-set stats, and the soundness result. `INVALID.md` appears if any answer was unsound.

The agent's self-reported success never counts; `grade.json` decides.

## Tasks — `tasks/_common.md` + `tasks/<solver>.{md,json}`

The agent-visible instruction is **composed** so the safety-critical rules have one source of truth:

- `tasks/_common.md` — the **shared** template, identical for every solver: objective (minimize the mean
  gap to Z3), the soundness gate (any unsound answer ⇒ score 0), writable vs frozen, the offline policy, the
  held-out firewall, the self-check recipe, the **stop criterion** (the `--timeout` budget; the agent also
  stops when it judges the task done), and the deliverables. Has `{{...}}` placeholders.
- `tasks/<solver>.md` — only this solver's **where-to-start** block (the one prose section that genuinely
  differs). Rendered into `{{START_HINTS}}`.
- `tasks/<solver>.json` — the **hidden** machine spec the agent never sees: config, scored sets, baseline,
  writable/frozen paths, the per-solver fields that fill the template (`language`, `build_cmd`, `test_cmd`,
  `solved_condition`, …), and a contamination canary. Consumed by `run.py`/`grade.py`.

`run.py` renders these into the exact prompt and saves it to the run dir as `prompt.md`. **`grade.py` is
not part of the agent's workflow** — the agent self-checks with `bench.py` on `dev`; `grade.py` is the
referee's post-run judge. It runs the dev split and the holdout split; the score is the holdout gap.

## What the agent can see

The agent works only on the dev split. `run.py` builds `.bench-view/`, which holds hard links to the 259 dev
queries and the canaries, and points the agent at it with `BENCH_ROOT`. The sandbox denies `benchmarks/`
(so the 2,317 holdout queries cannot be read), `.referee/` (the grader's Z3 holdout reference and its holdout
runs), and `runs/` (earlier grades). The grader runs outside the sandbox and uses the full `benchmarks/`.

## Sandbox policy (enforced, not prose)

- **Filesystem (hard, OS-enforced):** write only `solvers/<solver>` + `results/` + build caches
  (`~/.cargo`); the whole repo is read-only (so `scripts/`, `edrat-check/`, `config.env`, `benchmarks/`,
  `tools/`, `bin/z3` cannot be modified); `$HOME` is denied except the toolchain dirs and the `claude`
  state it needs to run. Verified: referee **reads** succeed, referee/`bin/z3`/`$HOME` **writes** fail.
- **Network:** `--network off` (default) drops `WebFetch`/`WebSearch` and denies network-capable Bash
  commands (curl/wget/pip/npm/git-network/…) via a `permissions.deny` tool-policy.

### macOS caveat (network)

macOS forbids nesting `sandbox-exec`, so we cannot wrap `claude` in our FS Seatbelt **and** have `claude`
OS-sandbox its own Bash tool for network at the same time. Network-off is therefore enforced at the
**tool-policy** layer (above), which is strong but not a kernel firewall — a command outside the deny list
could in principle reach the network. The filesystem confinement is a real OS sandbox regardless. On Linux,
FS confinement uses bubblewrap (no nesting conflict) and a socket-level block can be added for hard
network-off; `sandbox.py` is structured to grow that path. Use `--no-sandbox` only for debugging (unsafe).

## Extending

- **New solver target:** add `tasks/<solver>.{md,json}`, a `TEST_CMDS` entry in `grade.py`, and (if its
  toolchain lives under `$HOME`) extend `backend.toolchain_ro_paths()`. cvc5/alt-ergo also need their build
  dirs writable in `run.build_policy`.
- **New backend (e.g. a different CLI or the Anthropic API directly):** `backend.py` is one Claude-CLI
  implementation; factor a small `Backend` interface if a second backend is needed.
