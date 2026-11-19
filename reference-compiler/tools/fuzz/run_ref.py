"""Run the reference compiler's pipeline on one package and print what each stage made, as JSON.

    uv run python tools/fuzz/run_ref.py PACKAGE

The fuzzer runs this in a process of its own per program, under a timeout and a memory limit, so a
hang or a runaway is a finding rather than a stall. Stages, in order: the front end (resolution,
signatures, inference, lowering), the checker on the lowered program, the checker again on the program
read back from its KURS text (what `kursal check FILE` sees), and C. The first stage that fails ends
the run: `{"stage": ..., "error": ...}` for a `KursError`, `{"stage": ..., "crash": ...}` with the
traceback for any other exception."""

import itertools
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

# A campaign runs a snapshot of the reference compiler taken when it starts (KURSAL_FUZZ_REFERENCE, the
# directory holding its `kursal` package), so editing the compiler meanwhile does not change its verdicts.
sys.path.insert(0, os.environ.get("KURSAL_FUZZ_REFERENCE") or str(Path(__file__).resolve().parents[2]))

from kursal import infer as I  # noqa: E402
from kursal import types as T  # noqa: E402
from kursal.cgen import emit_c  # noqa: E402
from kursal.check import check_program  # noqa: E402
from kursal.kurs import parse_program, write_program  # noqa: E402
from kursal.lower import lower  # noqa: E402
from kursal.resolve import resolve  # noqa: E402
from kursal.sexpr import KursError  # noqa: E402
from kursal.sigs import signatures  # noqa: E402


def pipeline(package: Path) -> dict[str, Optional[str]]:
    """What the pipeline makes of `package`, numbering fresh names from 0 as one compiler run does."""
    T._fresh = itertools.count()
    I._ids = itertools.count()
    result: dict[str, Optional[str]] = {"stage": "frontend", "error": None, "crash": None,
                                        "kurs": None, "c": None, "entry": None}
    try:
        resolver = resolve(package)
        sigs = signatures(resolver)
        program = lower(resolver, sigs)
        kurs = write_program(program)
        result["kurs"] = kurs
        entry_decl = resolver.packages[0].entry_decl
        result["entry"] = sigs.name(entry_decl) if entry_decl is not None else None
        result["stage"] = "check"
        facts = check_program(program)
        result["stage"] = "check-kurs"
        check_program(parse_program(kurs))
        result["stage"] = "c"
        result["c"] = emit_c(program, facts, result["entry"], hosted=True)
        result["stage"] = "ok"
    except KursError as error:
        result["error"] = str(error)
    except RecursionError:
        result["crash"] = "RecursionError\n" + traceback.format_exc()[-4000:]
    except Exception:  # noqa: BLE001 - every other exception is a finding
        result["crash"] = traceback.format_exc()[-8000:]
    return result


def main() -> None:
    package = Path(sys.argv[1])
    out: list[dict[str, Optional[str]]] = []
    sys.setrecursionlimit(200_000)
    threading.stack_size(512 << 20)
    worker = threading.Thread(target=lambda: out.append(pipeline(package)))
    worker.start()
    worker.join()
    json.dump(out[0], sys.stdout)


if __name__ == "__main__":
    main()
