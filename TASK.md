# Task: make an open SMT solver as good as Z3 on program-verification queries

You are working on **one** of these solvers, chosen by whoever started you:

| solver | source tree | language | config that is scored |
|---|---|---|---|
| Sundance | `solvers/sundance` | Rust, about 14k lines | `sundance`: eDRAT proofs, checked by `edrat-check` |
| cvc5 | `solvers/cvc5` | C++ | `cvc5`: CPC proofs, checked by Logos (Ethos as a fallback) |
| Alt-Ergo | `solvers/alt-ergo` | OCaml | `alt-ergo`: no proofs |

The queries come from verified Rust (Verus), Dafny and F\* projects. Z3 solves almost all of them, partly
because the verifiers were tuned for Z3. **Goal: close the gap between your solver and Z3 on the Verus queries
without breaking soundness.** For now only the Verus development set is scored: you work on its `dev` split
(259 queries, `splits/verus-dev.txt`), and the other 2,317 queries (`--split holdout`) check generalization.

## Setup

The harness is already built (see README.md). Check that these commands work before you start:

```sh
python3 scripts/bench.py run z3 --split dev             # reference results (do this once)
python3 scripts/bench.py run <config> --split dev       # your baseline
python3 scripts/bench.py score results/<config>-dev-t60 --ref results/z3-dev-t60
```

After you change the solver, rebuild it with `scripts/fetch_solvers.sh <solver>`. This keeps your edits;
`--update` would also pull upstream. Then rerun the benchmark with `--out results/<name> --rerun`. For fast
iterations, use `--timeout 10`, `--sets verus`, or `--limit N`. `python3 scripts/bench.py list --split dev`
prints the query paths, so you can run the solver on single files directly.

## Rules

1. **Soundness is non-negotiable.** Never answer `unsat` unless the input is unsatisfiable. Every `unsat` from
   Sundance or cvc5 must come with a proof that the checker accepts, or it does not count. Any wrong answer
   (`sat` on a benchmark, `unsat` on a canary in `benchmarks/canaries`) invalidates the whole run.
2. **Do not touch the referee.** Do not modify `scripts/`, `edrat-check/`, `config.env`, `benchmarks/`,
   `tools/` (Logos, Ethos, drat-trim, the CPC signature), or `bin/z3`. You can change what the solver emits.
   For example, cvc5 could print its proofs in a form that Logos or Ethos can parse, or Sundance could print
   simpler eDRAT steps.
3. **No outside help at solve time.** The solver must not call or link another SMT solver (Sundance is built
   with `--no-default-features`, so Z3 is not linked in). It must not use the network, read files other
   than the query, or keep state between runs. It runs single-threaded.
4. **No benchmark-specific hacks.** Do not recognize queries by file name, hash, prelude text, or symbol
   names of a specific project, and do not store answers or proofs. Heuristics that use the *structure*
   that verifiers produce (patterns, `fuel` encodings, `has_type` guards, Boogie/F\* idioms) are fine, as
   long as they would still be correct on any input. Improvements must generalize: the final score
   includes the holdout split, which you must not tune on, and may include queries you have never seen.
5. **The command line is fixed** (see `CONFIGS` in `scripts/bench.py`). Change defaults inside the solver
   instead of adding flags.
6. **Keep the solver's own tests passing**: `cargo test --release` for Sundance, `ctest` / the regression
   suite for cvc5, `dune test` for Alt-Ergo.

## Score

The final evaluation runs `bench.py run <config> --split dev --timeout 60` and `bench.py score`. The score is
the **gap to Z3 in percentage points on the Verus dev split** (lower is better; 0 means as good as Z3). The
same run on `--split holdout` must not regress.
The share solved within 1 s and 10 s, and the solver's diff (simpler is better), break ties.

## Deliverables

- Commits in your solver's git repo (`solvers/<solver>`). Make one commit per idea, and in each commit
  message say what changed and its effect on the dev split, for example "verus 83.0 -> 85.1".
- A `NOTES.md` in the solver's repo: what you tried, what worked and what did not, with numbers, and the
  remaining failure classes (for example matching loops, missing theory support, parse errors, proof-check
  failures).

## Where to start

- **Look at why queries fail before you change anything.** In the result CSVs, group the queries by status
  (`error`, `timeout`, `unknown`, rejected or unchecked proofs) and by project (the first path component).
  Error and checker-gap classes are often the cheapest wins.
- Sundance: the paper (Sec. 8 and 10) names the gaps to Z3: relevancy propagation, eager instantiation,
  garbage collection of instances, and efficient e-matching (code trees; this is the bottleneck on the large
  DICE\* queries). Also check the inputs that the current `main` rejects up front (`src/theory_check.rs`).
  To see what reaches the proof checker, run `edrat-check/edrat_check.py QUERY PROOF -v`.
- cvc5: most of its gap is with proofs enabled. Look at the cost of proof production, at preprocessing on
  large F\* queries, and at proofs that the checkers cannot parse.
- Alt-Ergo: look at parse errors and crashes on z3-dialect input (see `scripts/prep_smtlib.py` for what is
  already rewritten), at trigger handling (`:pattern`), and at datatypes.
