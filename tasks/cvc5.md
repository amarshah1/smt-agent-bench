<!-- Solver-specific "where to start" block for cvc5. Shared rules/objective/constraints/
     self-check/stop criterion live in tasks/_common.md. Rendered into {{START_HINTS}}. -->
Most of cvc5's gap to Z3 appears **with proofs enabled** — the scored config produces CPC proofs and only
counts a query when a checker accepts the proof. So look first at the proof side:

- **Proof-checking gaps.** Group the result CSV rows by `status` and by `check` (`VERIFIED` = Logos,
  `VERIFIED-ETHOS` = the Ethos fallback; proof `NOT VERIFIED`; or unchecked: `ERROR`/`TIMEOUT`/`INCOMPLETE`).
  Logos cannot read `lambda`, so proofs of queries with parameterized `define-fun`s fall back to Ethos.
  Changing what cvc5 emits so that a checker accepts more proofs is legitimate; the checkers are frozen.
- **Cost of proof production and preprocessing**, especially on large queries,
  where timeouts concentrate. Logos is slow on big proofs (minutes for a few MB).

Then the ordinary solving gap: `--no-cbqi --user-pat=strict` are fixed, so improve the defaults inside the
solver (quantifier instantiation, e-matching, preprocessing) rather than the flags.
