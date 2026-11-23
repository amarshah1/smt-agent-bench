# smt-agent-bench

A small harness for one task: **have AI agents improve the open SMT solvers Sundance, cvc5 and Alt-Ergo
until they match Z3 on program-verification queries**. The queries are the benchmark sets of
[*Sundance: A Simple SMT Solver for Program Verification*](https://arxiv.org/abs/2611.05668).

This repo contains no benchmarks and no solver code. Its scripts pull them from their upstream repos:

| what | source | version |
|---|---|---|
| Verus development set (2,576 after deduplication) | [amarshah1/verus_benchmarks](https://github.com/amarshah1/verus_benchmarks) | pinned to the paper's commit |
| Verus held-out set (4,086) | [amarshah1/sundance_heldout_benchmarks](https://github.com/amarshah1/sundance_heldout_benchmarks) | pinned |
| Dafny (11,674) and F\* (3,195) queries, from Mariposa | [amarshah1/dafny_fstar_benchmarks](https://github.com/amarshah1/dafny_fstar_benchmarks) release v1.0 | pinned (sha256) |
| soundness canaries (satisfiable queries) | cvc5's regression suite | pinned tag |
| **Sundance** | [yaspar-org/Sundance-SMT](https://github.com/yaspar-org/Sundance-SMT) | `main`, built from source |
| **cvc5** | [cvc5/cvc5](https://github.com/cvc5/cvc5) | `main`, built from source |
| **Alt-Ergo** | [OCamlPro/alt-ergo](https://github.com/OCamlPro/alt-ergo) | `next`, built from source |
| Z3 (reference, not modified) | [Z3Prover/z3](https://github.com/Z3Prover/z3) | newest release |
| Logos, cvc5's newest proof checker (verified in Lean) | [cvc5/logos](https://github.com/cvc5/logos) | `main` |
| Ethos (fallback when Logos cannot parse a proof) | [cvc5/ethos](https://github.com/cvc5/ethos) | commit pinned by cvc5 |
| drat-trim (used by the eDRAT checker) | [marijnheule/drat-trim](https://github.com/marijnheule/drat-trim) | `master` |

Sundance proofs are checked by [`edrat-check/`](edrat-check/), a new open-source eDRAT checker in
this repo. The paper used Valido, which is not open source yet.

## Quick start

```sh
scripts/fetch_benchmarks.sh --sample 20     # or no flag: everything (3 GB download, 77 GB on disk)
scripts/fetch_solvers.sh                    # clone and build all solvers and checkers into solvers/, tools/, bin/
edrat-check/tests/run_tests.sh              # checker self-test

python3 scripts/bench.py run z3 --split dev        # the reference
python3 scripts/bench.py run sundance --split dev
python3 scripts/bench.py run cvc5 --split dev
python3 scripts/bench.py run alt-ergo --split dev
python3 scripts/bench.py score results/*-dev-t60 --ref results/z3-dev-t60
```

The machine needs GCC >= 10 (for cvc5), glibc >= 2.39 (for the Z3 release binary), Rust, opam, and elan
(the Lean toolchain manager, needed for Logos). On older machines, run everything through
`scripts/docker.sh`, which uses the Ubuntu 24.04 image in `Dockerfile`. For example:
`scripts/docker.sh scripts/fetch_solvers.sh`, or `scripts/docker.sh python3 scripts/bench.py run cvc5`.
Binaries built inside the container only run inside it.

`config.env` lists every URL and ref. Each one can be overridden from the environment, for example
`SUNDANCE_REF=c175059 scripts/fetch_solvers.sh --update sundance` builds the paper's Sundance commit.

## What gets measured

`bench.py run SOLVER` runs one fixed command line per solver (see `CONFIGS` in `scripts/bench.py`). The
configurations follow the paper:

| config | command | counts as solved when |
|---|---|---|
| `z3` (reference) | `z3` + the verifier's options, no proofs | `unsat` |
| `sundance` | `sundance-smt --arithmetic internalincremental --proof=P` (+`--infer-triggers` on Dafny/F\*) | `unsat` and **edrat-check accepts P** |
| `cvc5` | `cvc5 --no-cbqi --user-pat=strict --produce-proofs --dump-proofs --proof-format-mode=cpc --proof-granularity=dsl-rewrite` | `unsat` and **Logos says `correct`** (or Ethos accepts it, if Logos cannot parse it) |
| `alt-ergo` | `alt-ergo --timelimit=T` on the query rewritten by `scripts/prep_smtlib.py` | `unsat` (no proofs; the canaries keep it honest) |
| `sundance-noproof`, `cvc5-noproof`, `z3-proof` | the same without proofs, or with Z3 proofs (not checked) | for reference only |

- Time limit: 60 s per query (`--timeout`). The scores report the share of queries solved within 1 s,
  10 s and the limit, and the gap to Z3 in percentage points.
- Splits: `--split dev` is a fixed 10% sample of every set, for iteration. `--split full` is the whole set,
  and is what counts.
- Soundness: a `sat` answer on a benchmark (all of them are unsat: they come from verified projects), or an
  `unsat` answer on a canary, is a wrong answer, and it **invalidates the run**. Proofs that a checker
  *rejects* are reported separately from proofs it cannot parse or check in time.
- Results: `results/<config>-<split>-t<T>/<set>.csv`, one row per query, plus `meta.json` with the solver's git
  commit. Runs resume where they stopped.

The task for the agents is in [TASK.md](TASK.md).

## Baseline, and known issues as of 2026-11-18

The paper's numbers (60 s; % of queries solved):

| | Verus | held-out | Dafny | F\* |
|---|---:|---:|---:|---:|
| Z3, verifier options | 99.8 | 99.8 | 98.0 | 97.8 |
| Sundance (proofs) | 83.0 | 90.2 | 73.7 | 56.7 |
| cvc5 (proofs) | 88.4 | 95.3 | 69.8 | 54.9 |
| Alt-Ergo 2.4.3 | 79.8 | 82.8 | 70.4 | 59.3 |

The newest versions start in a different place:

- **Sundance `main`** (since PR #251) rejects every input that contains a `Real`-sorted term, including
  unused declarations in a prelude. This rejects nearly all held-out, Dafny and F\* queries
  ("unsupported theory: real arithmetic"). The paper's commit was `c175059`.
- **Logos** cannot parse the `(define f () (lambda ...))` commands that cvc5 prints for `define-fun`s with
  arguments, which Verus and F\* queries use. **Ethos** (the pinned version) rejects the sort name `Type`,
  which Verus declares. So, for now, cvc5's Verus proofs cannot be checked. Changing how cvc5 prints its
  proofs so that the checkers accept them is a legitimate improvement. Changing the checkers is not.
- Z3 5.x has a built-in `List` sort, so queries that declare `List` fail in Z3. The eDRAT checker then falls
  back to RUP for the affected lemmas.
- Alt-Ergo `next`/2.6.x is under the **OCamlPro Non-Commercial License**, not open source. Check that the
  license allows your use before you have agents modify it. `ALTERGO_REF=v2.4.3-free` selects the last
  CeCILL-C release (the one the paper used). The parser of that release needs
  `scripts/prep_smtlib.py --legacy-ae`.

## Layout

```
config.env                 every upstream URL / ref
scripts/fetch_benchmarks.sh, fetch_solvers.sh, docker.sh
scripts/bench.py           run / score / list / select-canaries
scripts/prep_smtlib.py     z3-dialect -> standard SMT-LIB for Alt-Ergo
edrat-check/               the eDRAT proof checker and its tests
TASK.md                    the prompt for the agents
```

Generated and ignored: `benchmarks/ solvers/ tools/ bin/ results/`.
