#!/usr/bin/env bash
# Clone and build the solvers and proof checkers. Each binary is linked into bin/.
#
# usage: scripts/fetch_solvers.sh [--update] [TARGET...]
#   TARGET: sundance cvc5 alt-ergo z3 drat-trim logos ethos   (default: all)
#   --update  fast-forward existing clones to the newest upstream commit before building.
#             Without it, an existing clone is only rebuilt, so the agent's edits are kept.
#
#   solvers/sundance   yaspar-org/Sundance-SMT, built with --no-default-features (no Z3 linked in)
#   solvers/cvc5       cvc5/cvc5, ./configure.sh unrestricted --auto-download
#   solvers/alt-ergo   OCamlPro/alt-ergo, built with dune in a local opam switch (opam is bootstrapped if missing)
#   solvers/z3         newest Z3 release binary (the reference; it is not modified). Built from the release
#                      source if the binary does not run here.
#   tools/drat-trim    used by edrat-check (RUP checking)
#   tools/logos        cvc5's Lean-verified CPC proof checker (needs elan; it is installed if missing)
#   tools/ethos        cvc5's C++ CPC proof checker; fallback for proofs Logos cannot parse
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
source "$ROOT/config.env"
S=$ROOT/solvers; TOOLS=$ROOT/tools; BIN=$ROOT/bin
mkdir -p "$S" "$TOOLS" "$BIN"
JOBS=${JOBS:-$(nproc)}
update=0; targets=()
for a in "$@"; do case $a in --update) update=1 ;; -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;; *) targets+=("$a") ;; esac; done
[ ${#targets[@]} -gt 0 ] || targets=(drat-trim z3 sundance cvc5 alt-ergo logos ethos)
log() { echo "[fetch_solvers] $*"; }
link() { [ -x "$1" ] || { log "ERROR: $1 was not built"; exit 1; }; ln -sf "$1" "$BIN/$2"; log "bin/$2 -> $1"; }
latest_tag() {  # newest release tag of a GitHub repo (no API call, so no rate limit)
  curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$1/releases/latest" | sed 's|.*/tag/||'
}
release_assets() {  # names of the assets of release $2 of repo $1
  curl -fsSL "https://github.com/$1/releases/expanded_assets/$2" | grep -o 'releases/download/[^"]*' | sed 's|.*/||' | sort -u
}
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

clone() {  # url ref dir
  if [ ! -d "$3/.git" ]; then
    log "cloning $1 ($2)"
    git clone --quiet --recurse-submodules "$1" "$3"
    git -C "$3" checkout --quiet "$2"
  elif [ $update = 1 ]; then
    log "updating $3 to origin/$2"
    git -C "$3" fetch --quiet origin
    git -C "$3" merge --ff-only --quiet "origin/$2" || { log "cannot fast-forward $3 (local commits?); left as is"; }
    git -C "$3" submodule update --init --recursive --quiet
  fi
  log "$(basename "$3") at $(git -C "$3" rev-parse --short HEAD) ($(git -C "$3" log -1 --format=%cs))"
}

build_sundance() {
  clone "$SUNDANCE_REPO" "$SUNDANCE_REF" "$S/sundance"
  ( cd "$S/sundance" && cargo build --release --no-default-features --features minisat 2>&1 | tail -3 ) \
    || ( cd "$S/sundance" && cargo build --release --no-default-features 2>&1 | tail -3 )
  link "$S/sundance/target/release/sundance-smt" sundance-smt
}

build_cvc5() {
  if [ "${CVC5_PREBUILT:-0}" = 1 ]; then cvc5_release; return; fi
  clone "$CVC5_REPO" "$CVC5_REF" "$S/cvc5"
  cd "$S/cvc5"
  if [ ! -f build/Makefile ] && [ ! -f build/build.ninja ]; then
    python3 -c 'import tomli, pyparsing' 2>/dev/null || python3 -m pip install --user -q tomli pyparsing
    # cvc5 needs GCC >= 10 or Clang >= 12, CMake >= 3.16 and Python >= 3.9
    ./configure.sh unrestricted --auto-download 2>&1 | tail -5 \
      || { log "cvc5 configure failed (old compiler?). Use scripts/docker.sh, or CVC5_PREBUILT=1 for the newest release binary"; exit 1; }
  fi
  cmake --build build -j "$JOBS" 2>&1 | tail -3
  link "$S/cvc5/build/bin/cvc5" cvc5
}

cvc5_release() {  # newest release, static build (for baselines on machines that cannot compile cvc5)
  local tag os arch d
  tag=$(latest_tag cvc5/cvc5)
  os=Linux; [ "$(uname -s)" = Darwin ] && os=macOS
  arch=$(uname -m); [ "$arch" = aarch64 ] && arch=arm64
  d=$S/cvc5-release/$tag
  if [ ! -x "$d/bin/cvc5" ]; then
    mkdir -p "$d"; log "downloading cvc5 $tag ($os-$arch-static)"
    curl -fsSL -o "$d/c.zip" "https://github.com/cvc5/cvc5/releases/download/$tag/cvc5-$os-$arch-static.zip"
    ( cd "$d" && unzip -q -o c.zip && rm c.zip && mkdir -p bin && cp cvc5-*/bin/cvc5 bin/ )
  fi
  link "$d/bin/cvc5" cvc5
}

ensure_opam() {
  if command -v opam >/dev/null; then OPAM=$(command -v opam); return; fi
  OPAM=$TOOLS/bin/opam
  if [ ! -x "$OPAM" ]; then
    mkdir -p "$TOOLS/bin"
    local arch; arch=$(uname -m); [ "$arch" = aarch64 ] && arch=arm64
    local v; v=$(latest_tag ocaml/opam)
    log "downloading opam $v"
    curl -fsSL -o "$OPAM" "https://github.com/ocaml/opam/releases/download/$v/opam-$v-$arch-$(uname -s | tr A-Z a-z)"
    chmod +x "$OPAM"
  fi
}

build_altergo() {
  clone "$ALTERGO_REPO" "$ALTERGO_REF" "$S/alt-ergo"
  ensure_opam
  export OPAMROOT=${OPAMROOT:-$TOOLS/opam} OPAMYES=1 OPAMCOLOR=never
  [ -d "$OPAMROOT" ] || "$OPAM" init --bare -n --disable-sandboxing -q
  cd "$S/alt-ergo"
  [ -d _opam ] || "$OPAM" switch create . "ocaml-base-compiler.$ALTERGO_OCAML" --deps-only --no-install -q
  eval "$("$OPAM" env --switch=. --set-switch)"
  "$OPAM" install -q --deps-only ./alt-ergo-lib.opam ./alt-ergo.opam
  dune build -p alt-ergo-lib,alt-ergo @install 2>&1 | tail -3 || dune build @install 2>&1 | tail -3
  link "$S/alt-ergo/_build/install/default/bin/alt-ergo" alt-ergo
}

build_z3() {
  local tag v
  if [ "$Z3_VERSION" = latest ]; then
    tag=$(latest_tag Z3Prover/z3)
  else tag=z3-$Z3_VERSION; fi
  v=${tag#z3-}
  local d=$S/z3/$tag; mkdir -p "$d"
  if [ ! -x "$d/bin/z3" ]; then
    local arch=x64; [ "$(uname -m)" = aarch64 ] && arch=arm64
    local asset; asset=$(release_assets Z3Prover/z3 "$tag" | grep "^z3-$v-$arch-glibc.*\.zip$" | head -1)
    if [ -n "$asset" ]; then
      log "downloading $asset"
      curl -fsSL -o "$d/z3.zip" "https://github.com/Z3Prover/z3/releases/download/$tag/$asset"
      ( cd "$d" && unzip -q -o z3.zip && rm z3.zip && mkdir -p bin && cp z3-*/bin/z3 bin/z3 )
    fi
    if [ -x "$d/bin/z3" ] && ! "$d/bin/z3" --version >/dev/null 2>&1; then rm -f "$d/bin/z3"; fi
    if [ ! -x "$d/bin/z3" ]; then
      log "release binary does not run here (glibc?); building $tag from source"
      [ -d "$d/src/.git" ] || git clone --quiet --depth 1 --branch "$tag" "$Z3_REPO" "$d/src"
      if cmake -S "$d/src" -B "$d/src/build" -DCMAKE_BUILD_TYPE=Release -DZ3_BUILD_LIBZ3_SHARED=OFF >/dev/null 2>&1; then
        cmake --build "$d/src/build" -j "$JOBS" 2>&1 | tail -2
      else      # cmake too old: use z3's own python build script
        ( cd "$d/src" && rm -rf build && python3 scripts/mk_make.py >/dev/null && make -s -C build -j "$JOBS" 2>&1 | tail -2 )
      fi
      mkdir -p "$d/bin" && cp "$d/src/build/z3" "$d/bin/z3"
    fi
  fi
  "$d/bin/z3" --version
  link "$d/bin/z3" z3
}

build_drat_trim() {
  clone "$DRAT_TRIM_REPO" "$DRAT_TRIM_REF" "$TOOLS/drat-trim"
  make -s -C "$TOOLS/drat-trim" 2>/dev/null
  link "$TOOLS/drat-trim/drat-trim" drat-trim
}

build_logos() {
  clone "$LOGOS_REPO" "$LOGOS_REF" "$TOOLS/logos"
  export PATH=${ELAN_HOME:-$HOME/.elan}/bin:$PATH
  if ! command -v lake >/dev/null; then
    log "installing elan (Lean toolchain manager)"
    curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none
  fi
  ( cd "$TOOLS/logos" && scripts/build.sh logos 2>&1 | tail -3 )
  link "$TOOLS/logos/.lake/build/bin/logos" logos
}

build_ethos() {
  local ref=$ETHOS_REF pin
  # by default use the Ethos commit that the cvc5 clone pins (its proofs target that version)
  pin=$(sed -n 's/^ETHOS_VERSION="\(.*\)"/\1/p' "$S/cvc5/contrib/get-ethos-checker" 2>/dev/null || true)
  if [ "$ref" = cvc5-pinned ] && [ -n "$pin" ]; then ref=$pin; elif [ "$ref" = cvc5-pinned ]; then ref=main; fi
  clone "$ETHOS_REPO" "$ref" "$TOOLS/ethos"
  git -C "$TOOLS/ethos" checkout --quiet "$ref" 2>/dev/null || { git -C "$TOOLS/ethos" fetch --quiet origin; git -C "$TOOLS/ethos" checkout --quiet "$ref"; }
  ( cd "$TOOLS/ethos" && { [ -d build ] || ./configure.sh >/dev/null; } && cmake --build build -j "$JOBS" 2>&1 | tail -2 )
  link "$TOOLS/ethos/build/src/ethos" ethos
  # Ethos needs cvc5's CPC signature. Take it from upstream (not from the working tree that agents edit),
  # at the upstream commit the cvc5 clone is based on.
  local src commit
  if [ -d "$S/cvc5/.git" ]; then src=$S/cvc5; commit=$(git -C "$src" merge-base HEAD "origin/$CVC5_REF")
  else src=$TOOLS/cvc5-sig-src; [ -d "$src/.git" ] || git clone --quiet --filter=blob:none --no-checkout "$CVC5_REPO" "$src"
       git -C "$src" fetch --quiet origin "$CVC5_REF"; commit=$(git -C "$src" rev-parse FETCH_HEAD); fi
  rm -rf "$TOOLS/cpc-signature"; mkdir -p "$TOOLS/cpc-signature"
  git -C "$src" archive "$commit" proofs/eo | tar -x -C "$TOOLS/cpc-signature"
  log "CPC signature from cvc5 ${commit:0:10} in tools/cpc-signature/proofs/eo"
}

for t in "${targets[@]}"; do
  case $t in
    sundance) build_sundance ;; cvc5) build_cvc5 ;; alt-ergo|altergo) build_altergo ;; z3) build_z3 ;;
    drat-trim) build_drat_trim ;; logos) build_logos ;; ethos) build_ethos ;;
    *) echo "unknown target $t" >&2; exit 2 ;;
  esac
  cd "$ROOT"
done
