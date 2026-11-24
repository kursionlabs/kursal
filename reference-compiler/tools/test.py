"""Run unittest modules in isolated processes, with bounded parallelism.

    .venv/bin/python tools/test.py                 # up to four workers
    .venv/bin/python tools/test.py -j 8             # choose concurrency
    .venv/bin/python tools/test.py -j 1 -v          # serial, full output
    .venv/bin/python tools/test.py tests.test_lower # selected unittest names

Each test group runs in its own process and fails if it runs longer than --timeout seconds.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
# The longest a test group may run, in seconds. The slowest, tests.test_compiler, takes about 650s with
# eight workers, and about half an hour more with KURSAL_SELFHOST=1 and valgrind installed.
TIMEOUT = 3600


def run_one(name: str, root: Path, timeout: float = TIMEOUT) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", name],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        partial = expired.output
        output = partial.decode(errors="replace") if isinstance(partial, bytes) else partial or ""
        return 1, f"{output}\nTimed out: {name} ran longer than {timeout:g}s and was stopped.\n", \
            time.monotonic() - started
    return result.returncode, result.stdout, time.monotonic() - started


def run_tests(names: list[str], jobs: int, root: Path = ROOT, verbose: bool = False,
              timeout: float = TIMEOUT) -> int:
    started = time.monotonic()
    failed = 0
    # Threads only supervise independent Python processes; test code never
    # shares interpreter globals. Keep each module's fixtures together.
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = {pool.submit(run_one, name, root, timeout): name for name in names}
        for future in as_completed(pending):
            name = pending[future]
            try:
                code, output, elapsed = future.result()
            except Exception as error:
                code, output, elapsed = 1, f"Runner error: {error}\n", 0.0
            failed += bool(code)
            print(f"{'FAIL' if code else 'PASS'} {name} ({elapsed:.2f}s)", flush=True)
            if code or verbose:
                print(output, end="" if output.endswith("\n") else "\n", flush=True)
    print(f"{len(names) - failed}/{len(names)} test groups passed in "
          f"{time.monotonic() - started:.2f}s ({jobs} workers)", flush=True)
    return int(failed != 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("-v", "--verbose", action="store_true", help="show output for passing groups too")
    parser.add_argument("--timeout", type=float, default=TIMEOUT,
                        help=f"seconds a test group may run before it is stopped and fails (default {TIMEOUT})")
    parser.add_argument("names", nargs="*", help="unittest module, class, or method names")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    # The suite is flat; use exactly unittest's default filename pattern.
    names = args.names or [f"tests.{p.stem}" for p in sorted((ROOT / "tests").glob("test*.py"))]
    if not names:
        parser.error("no tests found")
    return run_tests(names, args.jobs, verbose=args.verbose, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
