# Task: close {{SOLVER}}'s gap to Z3 on program-verification queries

You are an autonomous coding agent working on the **{{SOLVER}}** SMT solver ({{LANGUAGE}}), whose source is
in `solvers/{{SOLVER}}`. This file is your entry point; `README.md` and `TASK.md` at the repo root have the
full harness details.

## Objective (what is scored)

A fixed set of program-verification queries (from verified Verus, Dafny, and F\* projects) is run through
{{SOLVER}} and through **Z3** (the reference). Your score is the **mean gap to Z3, in percentage points, over
the four query sets** (Verus, held-out, Dafny, F\*) — the share of queries Z3 solves minus the share
{{SOLVER}} solves, averaged. **Lower is better; 0 means as good as Z3.** Maximize the fraction of queries
{{SOLVER}} solves within the time limit.

- The **unmodified solver you start from is the 0 anchor**; **Z3 is the reference** (its internals are not
  provided, and you must not call or link it). Your job is to move {{SOLVER}} toward Z3.
- {{SOLVED_CONDITION}}

## Hard constraints (a violation makes the whole run score 0)

1. **Soundness is non-negotiable.** Never answer `unsat` on a satisfiable input. Any wrong answer — `sat` on
   a benchmark, or `unsat` on a soundness canary — **invalidates the entire run (score 0)**, no matter how
   many other queries you solve.
2. **Only `solvers/{{SOLVER}}` and `results/` are writable.** The referee — `scripts/`, `edrat-check/`,
   `config.env`, `benchmarks/`, `tools/`, and `bin/z3` — is frozen and read-only. (This is enforced at the
   OS level; writes outside the solver will simply fail.) Change what the solver *emits*, not the referee.
3. **No network and no outside help at solve time.** You are offline. The solver must not call another SMT
   solver, use the network, read files other than the query, or keep state between runs. It runs
   single-threaded. Do not fetch the paper, upstream diffs, or any reference solution.
4. **No benchmark-specific hacks.** Do not recognize queries by file name, hash, prelude text, or project
   symbol names, and do not store answers or proofs. Heuristics that use the *structure* verifiers produce
   (patterns, `fuel` encodings, `has_type` guards, Boogie/F\* idioms) are fine if they stay correct on *any*
   input. Improvements must generalize: the final score is measured on a **held-out set and queries you
   never see**, which are **not on disk during your run** — you cannot tune to them, only to the dev split.
5. **The command line is fixed** (`CONFIGS` in `scripts/bench.py`). Change defaults *inside* the solver, not
   the flags.
6. **Keep the solver's own tests passing:** `{{TEST_CMD}}`.

## How to work and self-check

- Rebuild after each change: `{{BUILD_CMD}}` (the `bin/{{BIN}}` symlink then points at your new binary). Do
  **not** run `scripts/fetch_solvers.sh` — it is referee code and needs the network.
- Measure yourself on the fast **dev** split. This is **advisory only**: the authoritative grade is re-run
  by the harness after you stop, and it includes the held-out set you cannot see.

  ```sh
  python3 scripts/bench.py run {{CONFIG}} --split dev --out results/selfcheck --rerun
  python3 scripts/bench.py score results/selfcheck --ref results/z3-dev-t60
  ```

  For faster loops use `--timeout 10`, `--sets verus`, or `--limit N`. `python3 scripts/bench.py list
  --split dev` prints query paths so you can run the solver on single files.
- **Commit one idea per commit** in `solvers/{{SOLVER}}`, and in the message say what changed and its effect
  on dev (e.g. "verus 83.0 -> 85.1"). The harness grades your final committed state, so commit your work.

## Where to start

{{START_HINTS}}

## Stop criterion

You have a wall-clock budget of **{{TIME_BUDGET}}**. Work until you have made the gap as small as you can
within it, then stop. Stop earlier if you have run out of productive ideas and your last several attempts
produced no improvement on the dev split. Before stopping: ensure the solver **builds**, its **tests pass**,
your improvements are **committed**, and you have written a **`NOTES.md`** in `solvers/{{SOLVER}}` recording
what you tried, what worked and what did not (with dev numbers), and the remaining failure classes. When you
stop, the harness rebuilds your committed solver and grades it with the frozen referee — that grade is final.
