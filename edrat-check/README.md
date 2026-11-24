# edrat-check

An independent checker for the eDRAT proofs that Sundance writes (`sundance-smt --proof=FILE`). It is a
single Python file with no dependencies beyond the standard library (Python >= 3.7). It needs `drat-trim`
for the propositional part, and an SMT solver (Z3 by default) as an oracle for small quantifier-free
lemmas. The paper's checker, Valido, is not open source. This checker was written from the proof format
that Sundance emits (`src/proof/` in Sundance), not from Valido.

```sh
edrat_check.py QUERY.smt2 PROOF.edrat --drat-trim bin/drat-trim --oracle-bin bin/z3 [--timeout 600] [-v]
# prints "s VERIFIED" (exit 0), "s NOT VERIFIED" (1), "s TIMEOUT" (3), or "s ERROR" (2)
```

## The format, as Sundance writes it

```
(declare-sort ...) (declare-datatypes ...) (declare-fun ...) (define-fun ...)   ; header (not trusted)
(edrat-literal N TERM)          ; DIMACS variable N stands for the Boolean TERM
(declare-skolem L I NAME SORT)  ; the I-th Skolem constant for the quantified literal L
a C 0       ; assumption: a CNF clause of the input
t TH C 0    ; theory lemma; TH is uf | lia | lra | lira | dt | b (Boolean) | bg (combination)
q C 0       ; quantifier instance: -P I, where P = (forall xs phi) and I = phi[xs := ts]
s C 0       ; Skolemization: -P C with P = (exists xs psi), or P C with P = (forall xs phi)
C 0         ; derived clause (RUP)
d C 0       ; deletion
```

The checker treats the header as untrusted. It takes declarations and assertions only from the query.

## How each step is checked

| step | check |
|---|---|
| `edrat-literal` | It parses the term with `let`s expanded, `define-fun`s inlined, annotations dropped, and z3's `is-C` mapped to `(_ is C)`. A variable cannot be given two different terms. |
| `a` | The clause must follow from the query's assertions. First this tries three-valued Boolean propagation over the term structure (Tseitin clauses, unit and flattened assertions), with failed-literal probing. If that fails, the oracle must show that assertions ∧ ¬C is unsat after every quantified subformula is replaced by a fresh Boolean constant. That replacement is sound: if the abstraction is unsat, so is the original. |
| `t` | ¬C must be unsat by itself: by propagation, or by the oracle with quantifiers abstracted. |
| `q` | Structural matching modulo bound-variable renaming. The binding cannot capture variables, and the inferred sorts must match the binders. |
| `s` | Same matching, where every variable must map to its declared Skolem constant. That constant must be fresh: it cannot occur in the query or in any earlier term. |
| `C 0`, `d` | All checked premises go into a CNF. The derived clauses and deletions, in order, go into a DRAT file. `drat-trim -U` (**RUP only**) checks that file. RAT is not sound when variables have a theory meaning. |

A premise that fails its own check is not an immediate error. It becomes an ordinary DRAT lemma, which
must then be RUP. This handles the clauses that Sundance re-adds as `t b` after it has derived them. It
is sound, but it means a bad premise is reported by drat-trim (`NOT VERIFIED`) rather than by its own
line. Use `-v` and `--keep DIR` to see the generated CNF, DRAT, and oracle queries.

## Trusted base and limitations

- Trusted: this script, drat-trim, and the oracle solver on quantifier-free queries.
- Sort inference is best-effort. If it cannot infer the sort of an instantiation term, it does not check
  that sort.
- Only top-level quantified literals can be instantiated or Skolemized; that is how Sundance uses them.
  `match`, `lambda`, and `define-fun-rec` bodies are treated as opaque terms.
- With multiple `check-sat` commands, it uses the assertions in scope at the first one.
- On 536 Sundance proofs sampled from the four benchmark sets (Verus, Verus additional, Dafny, F\*), it accepted
  all of them (median 0.2 s, 95th percentile 3.4 s; with Z3 4.13 and 5.3 as the oracle). It rejected proofs with flipped literals, weakened literal definitions,
  stale Skolem constants, and inputs with the goal removed (see `tests/run_tests.sh`).
