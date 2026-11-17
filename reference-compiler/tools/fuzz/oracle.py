"""Run one program through both compilers and say which bug class, if any, it shows.

A program is a package: its files by relative path. `run_case` writes it into a directory of its own,
runs the reference pipeline (tools/fuzz/run_ref.py) in a process under a timeout and a memory limit,
then, when a self-hosted compiler is given, `kursal lower` and `kursal build` on the same directory,
then optionally compiles the C under sanitizers and runs it. Every run of a compiler is bounded, so a
hang or a runaway allocation becomes a finding."""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
REFERENCE = HERE.parents[1]
ROOT = REFERENCE.parent
LIB = ROOT / "lib"
C_HELPERS = [LIB / "libc/fs.c", LIB / "libc/thread.c"]

# Bug classes, in priority order.
CHECK_REJECTS = "check-rejects"        # the front end accepts; the checker rejects what it made
KURS_ROUNDTRIP = "kurs-roundtrip"      # the checker accepts the program but not its KURS read back
CRASH_REF = "crash-ref"                # a Python exception other than KursError, or a timeout
CRASH_SELF = "crash-self"              # a signal, an unexpected status or a timeout of the self-hosted compiler
DIFF_LOWER = "diff-lower"              # the compilers differ on lowering: verdict, message or KURS
DIFF_BUILD = "diff-build"              # the compilers differ on the whole build: verdict, message or C
CC_FAILS = "cc-fails"                  # the C does not compile
RUN_FAILS = "run-fails"                # the program traps, crashes, or a sanitizer reports
RUN_DIFFERS = "run-differs"            # the program's status differs between -O0 with sanitizers and -O3

MEMORY_LIMIT = 4 << 30
TIMEOUT = 20


def limit_memory() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))


@dataclass
class Outcome:
    """What the compilers made of one program. `bug` is the first bug class it shows, or None."""
    accepted: bool = False                     # the reference's front end accepted it
    checked: bool = False                      # and the checker accepted what it made
    stage: str = ""
    error: Optional[str] = None
    bug: Optional[str] = None
    detail: str = ""
    ran: bool = False
    timings: dict[str, float] = field(default_factory=dict)

    def signature(self) -> str:
        """The bug class and its normalized message, for telling findings apart."""
        return f"{self.bug}: {normalize(self.detail)}" if self.bug else ""


def normalize(text: str) -> str:
    """A message without what varies between programs showing the same bug: paths, positions, names."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    if "Traceback" in text:  # a Python crash: its exception and the innermost frame of the compiler
        lines = [line for line in text.splitlines() if line.strip()]
        frames = [line.strip() for line in lines if line.strip().startswith("File ") and "/kursal/" in line]
        first = lines[-1] + (" @ " + re.sub(r", line \d+", "", frames[-1].split("/kursal/")[-1]) if frames else "")
    first = re.sub(r"/\S+?\.kur", "F", first)
    first = re.sub(r"\d+:\d+:", "", first)
    first = re.sub(r"[%&$][\w#.!']+", "V", first)
    first = re.sub(r"`[^`]*`", "N", first)
    first = re.sub(r"'[^']*'", "N", first)
    first = re.sub(r"\b[a-z_]+[0-9]+\b", "N", first)
    first = re.sub(r"\d+", "D", first)
    return first.strip()[:160]


def manifest(entry: Optional[str]) -> str:
    text = f'[package]\nname="p"\nsource_root="."\n'
    if entry:
        text += f'kind="executable"\nentry="{entry}"\n'
    text += f'[dependencies.core]\npath="{LIB / "core"}"\n'
    return text


# A program's files may name, under this key, the path to compile within them (a package directory or
# a single module); by default it is the directory holding them.
TARGET = "@target"


def materialize(directory: Path, files: dict[str, str]) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    for rel, text in files.items():
        if rel == TARGET:
            continue
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_text(text)


def run(args: list[str], timeout: int = TIMEOUT, cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None, limited: bool = True) -> tuple[Optional[int], str, str]:
    """(status, stdout, stderr); status None on a timeout, negative for a signal. AddressSanitizer
    reserves terabytes of address space, so a sanitized program runs without the limit."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              preexec_fn=limit_memory if limited else None,
                              cwd=cwd, env=env, errors="replace")
    except subprocess.TimeoutExpired:
        return None, "", f"timeout after {timeout}s"
    return done.returncode, done.stdout, done.stderr


def snapshot_reference(directory: Path) -> None:
    """Run the reference compiler as it is now for the rest of this process and its children."""
    target = directory / "kursal"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(REFERENCE / "kursal", target, ignore=shutil.ignore_patterns("__pycache__"))
    os.environ["KURSAL_FUZZ_REFERENCE"] = str(directory)
    os.environ["KURSAL_LIB"] = str(LIB)


def reference(directory: Path) -> dict[str, Optional[str]]:
    status, out, err = run([sys.executable, str(HERE / "run_ref.py"), str(directory.resolve())], timeout=TIMEOUT * 3)
    if status is None:
        return {"stage": "frontend", "crash": "timeout", "error": None, "kurs": None, "c": None, "entry": None}
    if status != 0:
        return {"stage": "frontend", "crash": f"status {status}\n{err[-4000:]}", "error": None, "kurs": None,
                "c": None, "entry": None}
    result: dict[str, Optional[str]] = json.loads(out)
    return result


def self_status_bug(status: Optional[int], err: str) -> Optional[str]:
    """A self-hosted run's status is 0 or 1 with a message; anything else crashed."""
    if status is None:
        return "timeout"
    if status not in (0, 1) or (status == 1 and not err.strip()):
        return f"status {status}: {err.strip()[-2000:]}"
    return None


def compile_and_run(c_file: Path, work: Path, out: Outcome) -> None:
    """Compile the C at -O3 and at -O0 under sanitizers, run both, and compare their statuses."""
    from kursal.cc import FLAGS, c_compiler
    cc = c_compiler()
    fast, checked = work / "fast", work / "checked"
    status, _, err = run([cc, *FLAGS, str(c_file), *map(str, C_HELPERS), "-lpthread", "-o", str(fast)], timeout=120)
    if status != 0:
        out.bug, out.detail = CC_FAILS, err[-3000:]
        return
    sanitize = ["-O0", "-g", "-w", "-fno-builtin", "-fsanitize=undefined,address", "-fno-sanitize-recover=all"]
    status, _, err = run([cc, *sanitize, str(c_file), *map(str, C_HELPERS), "-lpthread", "-o", str(checked)],
                         timeout=120)
    if status != 0:
        out.bug, out.detail = CC_FAILS, "sanitized: " + err[-3000:]
        return
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:abort_on_error=0", UBSAN_OPTIONS="print_stacktrace=1")
    fast_status, _, fast_err = run([str(fast)], timeout=10)
    checked_status, _, checked_err = run([str(checked)], timeout=30, env=env, limited=False)
    out.ran = True
    if fast_status is None or checked_status is None:
        out.bug, out.detail = RUN_FAILS, "timeout"
    elif "runtime error" in checked_err or "AddressSanitizer" in checked_err:
        line = next((x for x in checked_err.splitlines() if "runtime error" in x or "ERROR:" in x), checked_err)
        out.bug, out.detail = RUN_FAILS, line + "\n" + checked_err[-3000:]
    elif fast_status == checked_status == -signal.SIGILL:
        pass  # both stopped at a trap (`core::process::trap`, a failed check): the program's own stop
    elif fast_status < 0 or checked_status < 0:
        out.bug, out.detail = RUN_FAILS, f"signal {-min(fast_status, checked_status)}\n{fast_err}{checked_err}"
    elif fast_status != checked_status:
        out.bug, out.detail = RUN_DIFFERS, f"-O3 {fast_status}, -O0 sanitized {checked_status}"


def run_case(files: dict[str, str], work: Path, selfhost: Optional[Path] = None, execute: bool = False) -> Outcome:
    """Every bug `files` shows: the first one found, in priority order, is `bug`."""
    out = Outcome()
    materialize(work / "pkg", files)
    package = work / "pkg" / files.get(TARGET, ".")
    ref = reference(package)
    out.stage = ref["stage"] or ""
    out.error = ref["error"]
    if ref["crash"]:
        out.bug, out.detail = CRASH_REF, ref["crash"]
        return out
    out.accepted = ref["kurs"] is not None
    out.checked = ref["stage"] in ("c", "ok")
    if ref["stage"] == "check":
        out.bug, out.detail = CHECK_REJECTS, ref["error"] or ""
        return out
    if ref["stage"] == "check-kurs":
        out.bug, out.detail = KURS_ROUNDTRIP, ref["error"] or ""
        return out
    if selfhost is not None:
        env = dict(os.environ, KURSAL_LIB=str(LIB))
        status, kurs, err = run([str(selfhost), "lower", str(package)], env=env)
        crashed = self_status_bug(status, err)
        if crashed:
            out.bug, out.detail = CRASH_SELF, "lower: " + crashed
            return out
        want = ref["kurs"] if ref["kurs"] is not None else f"error: {ref['error']}"
        got = kurs if status == 0 else f"error: {err.strip()}"
        if got != want:
            out.bug, out.detail = DIFF_LOWER, first_difference(want, got)
            return out
        if out.accepted:
            binary = work / "self"
            status, _, err = run([str(selfhost), "build", str(package), "-o", str(binary)], env=env, timeout=120)
            crashed = self_status_bug(status, err)
            if crashed:
                out.bug, out.detail = CRASH_SELF, "build: " + crashed
                return out
            if ref["c"] is None:
                if status == 0:
                    out.bug, out.detail = DIFF_BUILD, f"reference rejects at {ref['stage']}: {ref['error']}"
                    return out
            elif status != 0:
                if not err.strip().startswith("error: cc") and "cc" not in err[:40]:
                    out.bug, out.detail = DIFF_BUILD, f"self-hosted build fails: {err.strip()[:2000]}"
                    return out
            else:
                c_text = (work / "self.c").read_text(errors="replace")
                if c_text != ref["c"]:
                    out.bug, out.detail = DIFF_BUILD, "C differs: " + first_difference(ref["c"], c_text)
                    return out
    if execute and ref["c"] is not None and ref["entry"] is not None:
        c_file = work / "ref.c"
        c_file.write_text(ref["c"])
        compile_and_run(c_file, work, out)
    return out


def first_difference(want: str, got: str) -> str:
    a, b = want.splitlines(), got.splitlines()
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"line {i + 1}:\n  reference: {x[:400]}\n  self-host: {y[:400]}"
    return f"lengths {len(a)} and {len(b)} lines:\n  reference: {(a[len(b)] if len(a) > len(b) else '')[:400]}\n" \
           f"  self-host: {(b[len(a)] if len(b) > len(a) else '')[:400]}"
