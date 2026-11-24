"""Fuzz the compilers: generate and mutate programs, run each through both compilers, and keep every
program that shows a bug, minimized.

    uv run python -m tools.fuzz run --seed 1 --count 500 --out DIR [--selfhost ../bin/kursal] [-j 8]
                                    [--mode gen|mutate|mix] [--execute] [--minutes M]
    uv run python -m tools.fuzz show --seed 1 [--mode gen]       # print the program for a seed
    uv run python -m tools.fuzz one PACKAGE_OR_FILE [--selfhost ../bin/kursal] [--execute]
    uv run python -m tools.fuzz minimize FILE --out FILE [--selfhost ../bin/kursal]

Program `i` of a campaign is made from seed `SEED * 1_000_003 + i`, so a campaign is deterministic
given its seed and count (the order findings are reported in depends on scheduling). Findings go to
OUT/findings/<class>-<hash>/ (the program, the minimized program, the compilers' messages), and a summary
of every run to OUT/summary.json. Run campaigns from reference-compiler/, in a memory-capped scope
(see README.md, "Fuzzing")."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from tools.fuzz.gen import PRELUDE, generate
from tools.fuzz.mutate import Mutator, load_corpus
from tools.fuzz.oracle import TARGET, Outcome, manifest, run_case, snapshot_reference
from tools.fuzz.reduce import minimize

SEED_STRIDE = 1_000_003


def program(seed: int, mode: str) -> tuple[str, dict[str, str]]:
    """(how it was made, the package) for one seed."""
    if mode == "mix":
        mode = "gen" if seed % 3 else "mutate"
    if mode == "gen":
        return "gen", generate(seed)
    return "mutate", Mutator(seed).package()


def one(args: tuple[int, str, str, Optional[str], bool]) -> tuple[int, str, dict[str, str], Outcome, float]:
    seed, mode, work_root, selfhost, execute = args
    try:
        made, files = program(seed, mode)
    except Exception as error:  # noqa: BLE001 - a generator bug skips the seed, and is counted
        return seed, "generator-error", {}, Outcome(stage=f"generator: {error!r}"), 0.0
    work = Path(work_root) / f"w{os.getpid()}"
    started = time.monotonic()
    out = run_case(files, work, Path(selfhost) if selfhost else None, execute)
    return seed, made, files, out, time.monotonic() - started


def keep(found: Path, seed: int, made: str, files: dict[str, str], out: Outcome) -> tuple[Path, bool]:
    """Save a finding under its signature; (its directory, whether the signature is new)."""
    key = hashlib.sha256(out.signature().encode()).hexdigest()[:10]
    directory = found / f"{out.bug}-{key}"
    new = not directory.exists()
    sample = directory / f"seed{seed}"
    write_case(sample, files)
    (sample / "detail.txt").write_text(f"{made} seed {seed}\n{out.signature()}\n\n{out.detail}\n")
    if new:
        (directory / "signature.txt").write_text(out.signature() + "\n")
    return directory, new


def campaign(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    found = out_dir / "findings"
    found.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    selfhost = None
    if args.selfhost:  # a copy, so that rebuilding the compiler meanwhile does not change the campaign
        selfhost = str(out_dir / "kursal")
        shutil.copy(args.selfhost, selfhost)
    snapshot_reference(out_dir / "reference")
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    stats: dict[str, int] = {"programs": 0, "accepted": 0, "checked": 0, "ran": 0}
    by_made: dict[str, list[int]] = {}
    signatures: dict[str, int] = {}
    stages: dict[str, int] = {}
    new_dirs: list[Path] = []
    seeds = [args.seed * SEED_STRIDE + i for i in range(args.count)]
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        pending = set()
        it = iter(seeds)
        def submit() -> bool:
            if deadline is not None and time.monotonic() > deadline:
                return False
            seed = next(it, None)
            if seed is None:
                return False
            pending.add(pool.submit(one, (seed, args.mode, str(work), selfhost, args.execute)))
            return True
        for _ in range(args.jobs * 2):
            submit()
        while pending:
            done = next(as_completed(pending))
            pending.remove(done)
            submit()
            seed, made, files, out, elapsed = done.result()
            stats["programs"] += 1
            stats["accepted"] += out.accepted
            stats["checked"] += out.checked
            stats["ran"] += out.ran
            counts = by_made.setdefault(made, [0, 0, 0])
            counts[0] += 1
            counts[1] += out.accepted
            counts[2] += out.checked
            if not out.accepted and out.error:
                reason = normalize_stage(out.error)
                stages[reason] = stages.get(reason, 0) + 1
                if stages[reason] <= 3:  # a few rejected programs of each kind, to look at
                    sample = out_dir / "rejected" / hashlib.sha256(reason.encode()).hexdigest()[:10] / str(seed)
                    write_case(sample, files)
                    (sample / "error.txt").write_text(out.error + "\n")
            if out.bug:
                directory, new = keep(found, seed, made, files, out)
                signatures[out.signature()] = signatures.get(out.signature(), 0) + 1
                if new:
                    new_dirs.append(directory)
                    print(f"NEW {out.signature()}  (seed {seed}, {made})", flush=True)
            if stats["programs"] % 100 == 0:
                print(f"{stats['programs']} programs, {stats['accepted']} accepted, {stats['checked']} checked, "
                      f"{len(signatures)} distinct findings", flush=True)
    summary = {"stats": stats, "by_mode": by_made, "findings": signatures,
               "rejections": dict(sorted(stages.items(), key=lambda kv: -kv[1])[:40])}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    if args.minimize:
        for directory in new_dirs:
            sample = sorted(p for p in directory.iterdir() if p.is_dir())[0]
            minimize_dir(sample, directory / "min", selfhost, args.execute)
    return 0


def normalize_stage(error: str) -> str:
    from tools.fuzz.oracle import normalize
    return normalize(error)


def write_case(directory: Path, files: dict[str, str]) -> None:
    """A program's files under `directory`, its target (if not the directory) in a file named `@target`."""
    for rel, text in files.items():
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_text(text)


def read_case(path: Path) -> dict[str, str]:
    """A program `write_case` wrote, or a single .kur module made into a package with an entry if it has
    `main`."""
    if path.is_dir():
        found = {str(p.relative_to(path)): p.read_text() for p in path.rglob("*") if p.suffix in (".kur", ".toml")}
        if (path / TARGET).exists():
            found[TARGET] = (path / TARGET).read_text().strip()
        return found
    text = path.read_text()
    return {"kur.toml": manifest("main" if "fn main(" in text else None), "lib.kur": text}


def minimize_dir(sample: Path, target: Path, selfhost: Optional[str], execute: bool) -> None:
    files = read_case(sample)
    work = target.parent / "minwork"
    reduced, out = minimize(files, work, Path(selfhost) if selfhost else None, execute)
    target.mkdir(parents=True, exist_ok=True)
    write_case(target, reduced)
    (target / "detail.txt").write_text(f"{out.signature()}\n\n{out.detail}\n")
    shutil.rmtree(work, ignore_errors=True)
    print(f"minimized {sample} -> {target / 'lib.kur'} ({len(reduced.get('lib.kur', ''))} bytes)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.fuzz", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a campaign")
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--count", type=int, default=200)
    run.add_argument("--minutes", type=float, default=None, help="stop submitting programs after this long")
    run.add_argument("--mode", choices=("gen", "mutate", "mix"), default="mix")
    run.add_argument("--out", required=True)
    run.add_argument("--selfhost", help="the self-hosted compiler binary to compare with")
    run.add_argument("--execute", action="store_true", help="compile the C and run it under sanitizers")
    run.add_argument("--minimize", action="store_true", help="minimize the first program of each new finding")
    run.add_argument("--corpus", help="mutate the test packages listed in this file too (see `corpus`)")
    run.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))
    corpus = sub.add_parser("corpus", help="write the packages the reference's tests lower, to mutate")
    corpus.add_argument("--out", required=True)
    show = sub.add_parser("show", help="print the program made from one seed")
    show.add_argument("--seed", type=int, required=True)
    show.add_argument("--mode", choices=("gen", "mutate", "mix"), default="gen")
    one_p = sub.add_parser("one", help="run one program and print its outcome")
    one_p.add_argument("path")
    one_p.add_argument("--selfhost")
    one_p.add_argument("--execute", action="store_true")
    one_p.add_argument("--prelude", action="store_true", help="put the generator's prelude before the module")
    mini = sub.add_parser("minimize", help="minimize a program, keeping its bug signature")
    mini.add_argument("path")
    mini.add_argument("--out", required=True)
    mini.add_argument("--selfhost")
    mini.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    sys.setrecursionlimit(10000)
    if args.command == "run":
        if args.corpus:
            load_corpus(Path(args.corpus))
        return campaign(args)
    if args.command == "corpus":
        sys.setrecursionlimit(100000)
        from tests.test_compiler import lowering_cases
        cases = lowering_cases()
        Path(args.out).write_text(json.dumps(cases))
        print(f"{len(cases)} packages, {sum(not c['lower'].startswith('error:') for c in cases)} lowered")
        return 0
    if args.command == "show":
        _, files = program(args.seed, args.mode)
        print(files["lib.kur"])
        return 0
    if args.command == "one":
        work = Path(os.environ.get("TMPDIR", "/tmp")).resolve() / f"kursal-fuzz-one-{os.getpid()}"
        case = read_case(Path(args.path))
        if args.prelude:
            case["lib.kur"] = PRELUDE + "\n" + case["lib.kur"]
        out = run_case(case, work, Path(args.selfhost).resolve() if args.selfhost else None, args.execute)
        shutil.rmtree(work, ignore_errors=True)
        print(json.dumps({"accepted": out.accepted, "checked": out.checked, "stage": out.stage, "error": out.error,
                          "bug": out.bug, "signature": out.signature(), "ran": out.ran}, indent=2))
        if out.detail:
            print(out.detail)
        return 1 if out.bug else 0
    if args.command == "minimize":
        minimize_dir(Path(args.path), Path(args.out), str(Path(args.selfhost).resolve()) if args.selfhost else None,
                     args.execute)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
