<!-- Solver-specific "where to start" block for Alt-Ergo. Shared rules/objective/constraints/
     self-check/stop criterion live in tasks/_common.md. Rendered into {{START_HINTS}}. -->
Alt-Ergo produces no proofs, so your gap is pure solving power — but much of it is lost *before* solving, to
parse errors and crashes on the verifier-dialect input. Look there first:

- **Parse errors / crashes on the rewritten input.** The queries are z3-dialect and are rewritten to
  standard SMT-LIB by `scripts/prep_smtlib.py` (frozen) before Alt-Ergo sees them; group the result CSV rows
  by `status` (`error` is the big bucket) and read the notes. Handling more of what the verifiers emit —
  without changing the referee's rewriter — is the cheapest class.
- **Trigger handling (`:pattern`)** — quantifier instantiation driven by the patterns the verifiers attach.
- **Datatypes** — algebraic datatype reasoning on the Boogie/F\* encodings.

The command line is fixed (`alt-ergo --timelimit=T`), so improve the defaults inside the solver.
