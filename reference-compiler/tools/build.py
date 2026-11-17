"""Build an executable package with the reference compiler.

    uv run python tools/build.py PACKAGE OUTPUT [--times] [--kurs PATH]

writes OUTPUT.c and compiles it, with the standard library's C helpers, into OUTPUT (by `kursal.cc`:
`$KURSAL_CC`, else Clang, at -O3), reporting a C compiler that cannot run or fails; with --kurs, also
writes the lowered program, serialized as KURS, to PATH.
"""

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kursal.cc import c_compiler, cc_command  # noqa: E402
from kursal.cgen import emit_c  # noqa: E402
from kursal.check import check_program  # noqa: E402
from kursal.kurs import write_program  # noqa: E402
from kursal.lower import lower  # noqa: E402
from kursal.resolve import LIB, resolve  # noqa: E402
from kursal.sexpr import KursError  # noqa: E402
from kursal.sigs import signatures  # noqa: E402

C_HELPERS = [LIB / "libc/fs.c", LIB / "libc/thread.c"]


def build(package: Path, output: Path, times: bool, kurs: Optional[Path] = None) -> None:
    started = time.monotonic()

    def stage(name: str) -> None:
        nonlocal started
        if times:
            print(f"{name}: {time.monotonic() - started:.1f}s", file=sys.stderr)
        started = time.monotonic()

    resolver = resolve(package)
    sigs = signatures(resolver)
    stage("signatures")
    program = lower(resolver, sigs)
    stage("lower")
    if kurs is not None:
        kurs.write_text(write_program(program))
        stage("KURS")
    facts = check_program(program)
    stage("check")
    c_text = emit_c(program, facts, sigs.name(resolver.packages[0].entry_decl))
    stage("C")
    c_file = output.with_name(output.name + ".c")
    c_file.write_text(c_text)
    compiler = c_compiler()
    # The C compiler's own diagnostics go to stderr as it writes them; a failure is reported after them.
    try:
        built = subprocess.run(cc_command([str(c_file), *map(str, C_HELPERS)], str(output)))
    except OSError as error:
        raise SystemExit(f"cannot run the C compiler {compiler} (error {error.errno})")
    stage("cc")
    if built.returncode < 0:
        raise SystemExit(f"the C compiler {compiler} was killed by signal {-built.returncode}")
    if built.returncode != 0:
        raise SystemExit(f"the C compiler {compiler} exited with status {built.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--times", action="store_true", help="print the time each stage takes")
    parser.add_argument("--kurs", type=Path, metavar="PATH", help="also write the lowered program as KURS to PATH")
    args = parser.parse_args()
    failure: list[BaseException] = []

    def run() -> None:
        try:
            build(args.package, args.output, args.times, args.kurs)
        except KursError as error:
            failure.append(SystemExit(f"error: {error}"))
        except BaseException as error:  # noqa: BLE001 - re-raised on the main thread
            failure.append(error)

    # A large package recurses deeply through lowering and checking.
    sys.setrecursionlimit(1_000_000)
    threading.stack_size(1 << 30)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    if failure:
        raise failure[0]


if __name__ == "__main__":
    main()
