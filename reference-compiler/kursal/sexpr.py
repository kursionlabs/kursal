"""S-expression reader for KURS text: atoms, lists, `;` comments, strings."""

from __future__ import annotations

from typing import Optional

type Position = tuple[int, int]


ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", "'": "'", '"': '"'}


class KursError(Exception):
    def __init__(self, message: str, pos: Optional[Position] = None) -> None:
        super().__init__(f"{pos[0]}:{pos[1]}: {message}" if pos else message)
        self.message, self.pos = message, pos


def located(error: KursError, file: object, pos: Optional[Position]) -> KursError:
    """`error`, naming `file` and, when it has no position of its own, the position `pos` of the item it is
    in."""
    if str(error).startswith(str(file)):
        return error
    where = error.pos or pos
    prefix = f"{file}:{where[0]}:{where[1]}" if where else str(file)
    located_error = type(error)(f"{prefix}: {error.message}")
    located_error.message, located_error.pos = error.message, where
    return located_error


class Atom(str):
    pos: Optional[Position] = None


class Str(Atom):
    """A double-quoted string literal."""


class List(list["Form"]):
    pos: Optional[Position] = None


type Form = Atom | List


def _atom(cls: type[Atom], text: str, pos: Position) -> Atom:
    atom = cls(text)
    atom.pos = pos
    return atom


def read(text: str) -> List:
    """Every top-level form of `text`."""
    stack, i, n, line, line_start = [List()], 0, len(text), 1, 0
    while i < n:
        c, pos = text[i], (line, i - line_start + 1)
        if c == "\n":
            line, line_start = line + 1, i + 1
        if c in " \t\r\n":
            i += 1
        elif c == ";":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "(":
            form = List()
            form.pos = pos
            stack[-1].append(form)
            stack.append(form)
            i += 1
        elif c == ")":
            if len(stack) == 1:
                raise KursError("unbalanced ')'", pos)
            stack.pop()
            i += 1
        elif c == '"':
            j, chars = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and text[j + 1:j + 2] == "x":
                    chars.append(chr(int(text[j + 2:j + 4], 16)))
                    j += 4
                elif text[j] == "\\":
                    if text[j + 1:j + 2] not in ESCAPES:
                        raise KursError("unknown escape in a string literal", pos)
                    chars.append(ESCAPES[text[j + 1]])
                    j += 2
                else:
                    chars.append(text[j])
                    j += 1
            if j >= n:
                raise KursError("unterminated string literal", pos)
            stack[-1].append(_atom(Str, "".join(chars), pos))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n();"':
                j += 1
            stack[-1].append(_atom(Atom, text[i:j], pos))
            i = j
    if len(stack) != 1:
        raise KursError("unclosed '('", stack[-1].pos)
    return stack[0]
