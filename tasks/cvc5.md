<!-- Solver-specific "where to start" block for cvc5. Shared rules/objective/constraints/
     self-check/stop criterion live in tasks/_common.md. Rendered into {{START_HINTS}}. -->
Most of cvc5's gap to Z3 appears **with proofs enabled** — the scored config produces CPC proofs and only
counts a query when a checker accepts the proof. So look first at the proof side:

- **Checker-gap classes (often the cheapest wins).** Group the result CSV rows by `status` and by `check`
  (proof `NOT VERIFIED`, or unchecked: `ERROR`/`TIMEOUT`/`INCOMPLETE`). On the current `main`, most Verus
  proofs are uncheckable. Logos has no representation for `lambda` at all, and cvc5's proofs of queries
  with `define-fun`s that take arguments contain lambda terms (printed as `(define f () (lambda ...))` and
  used in later steps). Ethos, the fallback, has lambda in its CPC signature but stops earlier: it rejects the
  sort name `Type`, which Verus declares, and the `let`-wrapped `define` form. **Changing what cvc5 emits so that Logos or Ethos accepts
  the proofs is a legitimate, high-value improvement**; the checkers are frozen.
- **Cost of proof production and preprocessing**, especially on large queries,
  where timeouts concentrate. Logos is slow on big proofs (minutes for a few MB).

Then the ordinary solving gap: `--no-cbqi --user-pat=strict` are fixed, so improve the defaults inside the
solver (quantifier instantiation, e-matching, preprocessing) rather than the flags.
