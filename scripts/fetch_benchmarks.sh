#!/usr/bin/env bash
# Download the paper's benchmark sets (and the soundness canaries) into benchmarks/.
#
#   benchmarks/verus_benchmarks/UFDTLIA/20241211-verus   Verus development set: 3,763 files. The 1,187
#                                                        anvil/splinterdb-smt-* copies of splinterdb/* are skipped
#                                                        by bench.py, which leaves 2,576.
#   benchmarks/sundance_heldout_benchmarks/UFDTLIA       Verus held-out set: 4,086 files
#   benchmarks/dafny_fstar_benchmarks/UFLIA              Mariposa Dafny (d_*, 11,674) and F* (fs_*, 3,195) queries
#   benchmarks/canaries                                  satisfiable queries from cvc5's regression suite
#
# usage: scripts/fetch_benchmarks.sh [--sample N] [--only verus|heldout|dafny_fstar|canaries]...
#   --sample N   only N random files per set (fixed seed), for trying things out. It downloads the
#                two smallest Dafny/F* tarballs (about 0.3 GB) and extracts only the sampled files.
# Full download: about 3 GB, and about 77 GB on disk. A rerun skips whatever is already there.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
source "$ROOT/config.env"
B=${BENCH_ROOT:-$ROOT/benchmarks}
ASSETS="UFLIA-d_fvbkv.tar.xz 1cb93b4d2e4dbbc3144a14ae636f97cc47960a60a27d1cb0c802e529bf1539de
UFLIA-d_komodo.tar.xz 015a9b71c08c8aeadb5438471350647be0ffebda046acd58ee3757af69384f0d
UFLIA-d_lvbkv.tar.xz acb10fc83b32a1ef6510b4b4923f6b1dddf0c42f2fe6d2a6356df78419ae0ce6
UFLIA-fs_dice.tar.xz a6ed9ed8f137bd22ccb0d0ce1b606c8630b8b0a629366a6a22b21e365a17420c
UFLIA-fs_vwasm.tar.xz 837f764d0234485ff38099bd91aed4ebd01b14125a52a753583f7c4a25c033eb"
die() { echo "fetch_benchmarks: $*" >&2; exit 1; }
only=() nsample=0
while [ $# -gt 0 ]; do
  case $1 in
    --only) only+=("$2"); shift 2 ;;
    --sample) nsample=$2; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument $1" ;;
  esac
done
want() { [ ${#only[@]} -eq 0 ] && return 0; for o in "${only[@]}"; do [ "$o" = "$1" ] && return 0; done; return 1; }
mkdir -p "$B"
sample() { shuf -n "$nsample" --random-source=<(yes) | sort; }

git_set() {  # name url commit folder expected
  local name=$1 url=$2 commit=$3 folder=$4 expected=$5 dir
  dir=$B/$(basename "$url" .git)
  if [ ! -d "$dir/.git" ]; then
    echo "cloning $name (blobless, sparse) ..."
    git clone --quiet --filter=blob:none --no-checkout "$url" "$dir"
  fi
  git -C "$dir" cat-file -e "$commit^{commit}" 2>/dev/null || git -C "$dir" fetch --quiet origin "$commit"
  if [ "$nsample" -gt 0 ]; then
    git -C "$dir" ls-tree -r --name-only "$commit" -- "$folder" | grep '\.smt2$' | grep -v '/anvil/splinterdb-smt-' \
      | sample | sed -e 's/[][*?\\]/\\&/g' -e 's|^|/|' | git -C "$dir" sparse-checkout set --no-cone --stdin
    expected=$nsample
  else
    git -C "$dir" sparse-checkout set --no-cone "/$folder/"
  fi
  git -C "$dir" -c advice.detachedHead=false checkout --quiet --force "$commit"
  n=$(find "$dir/$folder" -name '*.smt2' | wc -l)
  [ "$n" -eq "$expected" ] || die "$name: found $n files, expected $expected"
  echo "$name: $n files @ ${commit:0:12} (OK)"
}
want verus && git_set verus "$VERUS_REPO" "$VERUS_COMMIT" UFDTLIA/20241211-verus 3763
want heldout && git_set heldout "$HELDOUT_REPO" "$HELDOUT_COMMIT" UFDTLIA 4086

if want dafny_fstar; then
  d=$B/dafny_fstar_benchmarks; mkdir -p "$d/.tarballs"
  while read -r name sum; do
    if [ "$nsample" -gt 0 ]; then case $name in *d_komodo*|*fs_vwasm*) ;; *) continue ;; esac; fi
    top=${name%.tar.xz}; top=${top/UFLIA-/UFLIA/}
    if [ -f "$d/.done.$name" ] && [ -d "$d/$top" ]; then echo "$name: already extracted"; continue; fi
    f=$d/.tarballs/$name
    if [ ! -f "$f" ]; then
      echo "downloading $name ..."
      curl -fL -sS --retry 3 -C - -o "$f.part" "$DAFNY_FSTAR_RELEASE/$name" && mv "$f.part" "$f"
    fi
    echo "$sum  $f" | sha256sum -c --status - || { rm -f "$f"; die "$name: sha256 mismatch (deleted; rerun)"; }
    if [ "$nsample" -gt 0 ]; then
      mapfile -t files < <(tar -tJf "$f" | grep '\.smt2$' | sample)
      tar -xJf "$f" -C "$d" "${files[@]}"
      echo "$name: ${#files[@]} sampled files"
    else
      echo "extracting $name ..."; tar -xJf "$f" -C "$d"; touch "$d/.done.$name"; rm -f "$f"
    fi
  done <<< "$ASSETS"
  echo "Dafny/F*: $(find "$d/UFLIA" -name '*.smt2' | wc -l) files"
fi

if want canaries; then
  c=$B/.cvc5-regress
  if [ ! -d "$c/.git" ]; then
    echo "cloning cvc5 regressions for the soundness canaries (sparse) ..."
    git clone --quiet --filter=blob:none --no-checkout "$CANARY_REPO" "$c"
  fi
  git -C "$c" sparse-checkout set --no-cone /test/regress/cli/
  git -C "$c" -c advice.detachedHead=false checkout --quiet --force "$CANARY_COMMIT"
  python3 "$ROOT/scripts/bench.py" select-canaries "$c/test/regress/cli" "$B/canaries"
fi
echo "benchmarks in $B"
