"""The C compiler every build and test uses, and its flags: `$KURSAL_CC` when it is set, else Clang.

`-O3` optimizes; `-w` silences warnings about generated code (unused labels and variables, the
standard library's own prototypes of C functions); `-fno-builtin` keeps the C compiler from giving
a function the meaning of the C library function of the same name, which an `extern` or a Kursal
function may declare with another type. `compiler/main.kur` runs the same command.

`RUNTIME` is the C runtime every hosted executable links, `lib/libc`'s C files, whose functions the
`libc` package declares and the C `main` of `kursal build` calls; it needs the threads library."""

import os

from .resolve import LIB

FLAGS = ("-O3", "-w", "-fno-builtin")
RUNTIME = tuple(LIB / "libc" / f"{name}.c" for name in ("io", "fs", "env", "process", "start", "time", "thread"))


def c_compiler() -> str:
    return os.environ.get("KURSAL_CC") or "clang"


def cc_command(sources: list[str], output: str, *extra: str) -> list[str]:
    """Compile and link `sources` into `output`, with the threads library."""
    return [c_compiler(), *FLAGS, *sources, *extra, "-lpthread", "-o", output]
