"""Pure scalar-literal contract shared by source lowering and core assertions."""
import math
import re
import struct

from .core import FLOATS, INTEGERS, LITERAL_BITS, Prim


def valid_literal(ty: Prim, text: str) -> bool:
    if ty.name == "bool":
        return text in ("true", "false")
    if ty.name in FLOATS:
        if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text) is None:
            return False
        try:
            value = float(text)
            if not math.isfinite(value):
                return False
            if ty.name == "f32":
                bits = int.from_bytes(struct.pack(">f", value), "big")
                return bits & 0x7f800000 != 0x7f800000
            return True
        except (ValueError, OverflowError):
            return False
    if ty.name not in INTEGERS or re.fullmatch(r"0|-?[1-9][0-9]*", text) is None:
        return False
    try:
        value = int(text, 10)
    except ValueError:
        return False
    bits = LITERAL_BITS[ty.name]
    if ty.name.startswith("i"):
        return -(1 << (bits - 1)) <= value < 1 << (bits - 1)
    return 0 <= value < 1 << bits
