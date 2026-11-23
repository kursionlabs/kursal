#!/bin/sh
# Bootstrap the self-hosted compiler into bin/.
#
#   scripts/bootstrap.sh [--check]
#
# Stage 0: the reference compiler builds compiler/ into bin/stage0/kursal.
# Stage 1: that compiler builds compiler/ again into bin/kursal.
# With --check, bin/kursal builds compiler/ once more into bin/stage2/kursal, and the C of stages 1
# and 2 (their split C, in bin/kursal.build and bin/stage2/kursal.build) must be identical.
#
# Every stage compiles its C with $KURSAL_CC, else clang, at -O3 (reference-compiler/kursal/cc.py);
# stages 1 and 2 split it into a file per module and link with link-time optimization.
# Every run of a Kursal-built compiler is capped at 6 GB of address space and 10 minutes, where
# the system can (not macOS's `ulimit -v`, nor its missing `timeout`).
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

# macOS has neither `timeout` (unless coreutils' gtimeout is installed) nor a working `ulimit -v`.
if command -v timeout >/dev/null 2>&1; then limit_time="timeout 600"
elif command -v gtimeout >/dev/null 2>&1; then limit_time="gtimeout 600"
else limit_time=""; fi

kursal() {
    (ulimit -v 6000000 2>/dev/null || true; exec $limit_time "$@")
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
    if diff -rq --exclude='*.o' "$bin/kursal.build" "$bin/stage2/kursal.build" >/dev/null; then
        echo "fixpoint: stages 1 and 2 make the same C" >&2
    else
        echo "error: stages 1 and 2 make different C" >&2
        exit 1
    fi
fi

echo "built $bin/kursal" >&2
