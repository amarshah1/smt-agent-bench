#!/usr/bin/env bash
# Run a command inside the Dockerfile's environment, as your own user, with this repo mounted at the
# same path (so bin/ symlinks and results stay valid outside the container).
#   scripts/docker.sh scripts/fetch_solvers.sh
#   scripts/docker.sh python3 scripts/bench.py run cvc5 --split dev
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${BENCH_IMAGE:-smt-agent-bench}
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -q -t "$IMAGE" "$ROOT" >/dev/null
mkdir -p "$ROOT/tools/home"
tty=(); [ -t 0 ] && tty=(-it)
case $PWD/ in "$ROOT"/*) wd=$PWD ;; *) wd=$ROOT ;; esac
exec docker run --rm --ulimit stack=-1:-1 ${tty[@]+"${tty[@]}"} -u "$(id -u):$(id -g)" -e HOME="$ROOT/tools/home" -e JOBS -e GITHUB_TOKEN \
  -v "$ROOT:$ROOT" -w "$wd" "$IMAGE" "${@:-bash}"
