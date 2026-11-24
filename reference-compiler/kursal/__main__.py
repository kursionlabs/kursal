"""python3 -m kursal FILE.kurs [--emit-c | --emit-kurs] [--entry NAME] [-o OUT]

Checks serialized KURS, printing nothing when it is sound; with --emit-c or --emit-kurs, also prints
its C or its readable form. An error goes to stderr, with status 1; a misused command line has status 2.
"""

from collections.abc import Sequence
import argparse
import sys
from typing import Optional

from .cgen import emit_c
from .check import check_program
from .kurs import parse_program
from .readable import emit_kurs
from .sexpr import KursError


class Arguments(argparse.Namespace):
    file: str
    emit_c: bool
    emit_kurs: bool
    entry: Optional[str]
    o: Optional[str]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m kursal", description="Check KURS text, or compile it to C.")
    parser.add_argument("file")
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument("--emit-c", action="store_true", help="emit C instead of only checking")
    output_mode.add_argument("--emit-kurs", action="store_true", help="emit readable KURS (input remains S-expressions)")
    parser.add_argument("--entry", help="the function entered from C main")
    parser.add_argument("-o", metavar="OUTPUT")
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        with open(args.file, "rb") as f:
            data = f.read()
    except OSError as e:
        return failure(f"cannot read {args.file} (error {e.errno})")
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return failure(f"{args.file} is not UTF-8 text")
    try:
        program = parse_program(text)
        checker = check_program(program)
        if args.emit_c:
            output = emit_c(program, checker, args.entry)
        elif args.emit_kurs:
            output = emit_kurs(program)
        else:
            return 0
    except KursError as e:
        return failure(str(e))
    if args.o:
        try:
            with open(args.o, "w") as f:
                f.write(output)
        except OSError as e:
            return failure(f"cannot write {args.o} (error {e.errno})")
    else:
        sys.stdout.write(output)
    return 0


def failure(message: str) -> int:
    """Reports `message` and gives the status of a program that fails: 1."""
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
