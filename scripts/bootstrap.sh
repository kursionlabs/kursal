#!/bin/sh
# Bootstrap the self-hosted compiler into bin/.
#
#   scripts/bootstrap.sh [--check]
#
# Stage 0: the reference compiler builds compiler/ into bin/stage0/kursal.
# Stage 1: that compiler builds compiler/ again into bin/kursal.
# With --check, bin/kursal builds compiler/ once more into bin/stage2/kursal, and the C of stages 1
# and 2 must be identical.
#
# Every run of a Kursal-built compiler is capped at 6 GB of address space and 10 minutes.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
bin="$root/bin"
check=false
case "${1-}" in
    "") ;;
    --check) check=true ;;
    *) echo "usage: scripts/bootstrap.sh [--check]" >&2; exit 2 ;;
esac

export KURSAL_LIB="$root/lib"

kursal() {
    (ulimit -v 6000000; timeout 600 "$@")
}

mkdir -p "$bin/stage0"

echo "stage 0: the reference compiler builds compiler/" >&2
(cd "$root/reference-compiler" && uv run python tools/build.py "$root/compiler" "$bin/stage0/kursal" --times)

echo "stage 1: bin/stage0/kursal builds compiler/" >&2
kursal "$bin/stage0/kursal" build "$root/compiler" -o "$bin/kursal" --times

if $check; then
    mkdir -p "$bin/stage2"
    echo "stage 2: bin/kursal builds compiler/" >&2
    kursal "$bin/kursal" build "$root/compiler" -o "$bin/stage2/kursal" --times
    if cmp -s "$bin/kursal.c" "$bin/stage2/kursal.c"; then
        echo "fixpoint: stages 1 and 2 make the same C" >&2
    else
        echo "error: stages 1 and 2 make different C" >&2
        exit 1
    fi
fi

echo "built $bin/kursal" >&2
