<!-- Solver-specific "where to start" block for Sundance. The shared rules,
     objective, constraints, self-check, and stop criterion live in tasks/_common.md
     and are identical across all solvers; only this block differs. Rendered into
     {{START_HINTS}} by agent/run.py. -->
Look at *why* queries fail before changing anything: group the result CSV rows by `status` (`error`,
`timeout`, `unknown`) and by project (the first path component). The `error` class is usually the largest
and cheapest to recover here — Sundance's eDRAT proofs, when it does solve, are almost always accepted, so
the gap is mostly inputs it never solves rather than proofs the checker rejects.

On the current `main`, a large class of held-out / Dafny / F\* queries fails up front with *"unsupported
theory: real arithmetic"* — Sundance rejects any input containing a `Real`-sorted term, even an unused
declaration in a prelude (see `src/theory_check.rs`). Making the solver accept and handle such inputs is
likely the cheapest large win.

The paper (Sec. 8 and 10) names the deeper gaps to Z3: relevancy propagation, eager instantiation, garbage
collection of instances, and efficient e-matching (code trees — the bottleneck on the large DICE\* F\*
queries). Check what reaches the proof checker, and prefer emitting simpler eDRAT steps where you can.
