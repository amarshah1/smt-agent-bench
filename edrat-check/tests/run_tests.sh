#!/usr/bin/env bash
# Positive tests: Sundance's proofs of the *.smt2 files must check. Negative tests: the same proofs with the
# goal assertion removed from the input, and hand-made bogus proofs, must not check.
# usage: edrat-check/tests/run_tests.sh   (needs bin/drat-trim and bin/z3, or DRAT_TRIM / EDRAT_ORACLE)
set -uo pipefail
cd "$(dirname "$0")"
BIN=../../bin
chk() { python3 ../edrat_check.py "$1" "$2" --drat-trim "${DRAT_TRIM:-$BIN/drat-trim}" --oracle-bin "${EDRAT_ORACLE:-$BIN/z3}" | tail -1; }
fail=0
expect() {  # want input proof
  got=$(chk "$2" "$3")
  if [ "$got" = "s $1" ]; then echo "ok    $1: $2 $3"; else echo "FAIL  want $1, got '$got': $2 $3"; fail=1; fi
}
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
for f in *.smt2; do
  p=${f%.smt2}.edrat
  expect VERIFIED "$f" "$p"
  n=$(grep -n '^(assert' "$f" | tail -1 | cut -d: -f1)
  sed "${n}d" "$f" > "$tmp/$f"
  expect "NOT VERIFIED" "$tmp/$f" "$p"
done
expect "NOT VERIFIED" euf.smt2 bad_lemma.edrat
expect "NOT VERIFIED" skolem.smt2 bad_skolem.edrat
exit $fail
