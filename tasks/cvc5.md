<!-- Solver-specific "where to start" block for cvc5. Shared rules/objective/constraints/
     self-check/stop criterion live in tasks/_common.md. Rendered into {{START_HINTS}}. -->
Most of cvc5's gap to Z3 appears **with proofs enabled** — the scored config produces CPC proofs and only
counts a query when a checker accepts the proof. So look first at the proof side:

- **Checker-gap classes (often the cheapest wins).** Group the result CSV rows by `status` and by `check`
  (proof `NOT VERIFIED`, or unchecked: `ERROR`/`TIMEOUT`/`INCOMPLETE`). Two known gaps on the current
  `main`: Logos cannot parse the `(define f () (lambda ...))` form cvc5 prints for `define-fun`s with
  arguments (used by Verus and F\* queries), and Ethos (the pinned fallback) rejects the sort name `Type`
  that Verus declares — so those Verus proofs are currently uncheckable. **Changing how cvc5 prints its
  proofs so Logos or Ethos accepts them is a legitimate, high-value improvement**; the checkers are frozen.
- **Cost of proof production and preprocessing**, especially on the large F\* (DICE\*) queries — this is
  where timeouts concentrate.

Then the ordinary solving gap: `--no-cbqi --user-pat=strict` are fixed, so improve the defaults inside the
solver (quantifier instantiation, e-matching, preprocessing) rather than the flags.
