"""Symbol names in the Itanium `_ZN...E` nested-name form (`kurs::path::h<hash>`, as `c++filt`
shows them), where the hash is of the specialization's logical identity.

This is a codegen convention, not a C++ ABI or a stable linking contract.
`type_key` normalizes bound names; nominal names and free brands remain distinct. A
specialization's type arguments are keyed `unbranded`, so code made for them does not depend on
their brands.
"""
from __future__ import annotations

import hashlib
import re

from . import core as C
from .types import foreach_binders, map_children


def encode(parts: tuple[str, ...]) -> str:
    """Length framing avoids ambiguity without recursively escaping nested keys."""
    return "".join(f"{len(part)}:{part}" for part in parts)


BRAND = C.Skolem("_")


def unbranded(ty: C.Type) -> C.Type:
    """`ty` with every brand, free or bound, written as the one brand `_`.

    A brand has no representation, and coherence never tells two instance heads apart by brands
    alone, so specializations whose type arguments differ only in brands make the same C."""
    if isinstance(ty, C.Skolem):
        return BRAND
    return map_children(ty, unbranded)


def type_key(ty: C.Type, scope: tuple[str, ...] = ()) -> str:
    def key(t: C.Type) -> str:
        return type_key(t, scope)

    def bound(name: str) -> str:
        return str(scope[::-1].index(name))

    parts: tuple[str, ...]
    match ty:
        case C.Skolem(label=n):
            parts = ("bound-brand", bound(n)) if n in scope else ("brand", n)
        case C.Named(name=n, args=()) if n in scope:
            parts = ("bound-type", bound(n))
        case C.Named(name=n, args=args):
            parts = ("named", n, *(key(t) for t in args))
        case C.Prim(name=n):
            parts = ("prim", n)
        case C.Never():
            parts = ("never",)
        case C.Ignored():
            parts = ("ignored",)
        case C.Tuple(elems=elems):
            parts = ("tuple", *(key(t) for t in elems))
        case C.Struct(fields=fields) | C.Enum(variants=fields):
            parts = (type(ty).__name__, *(encode((n, key(t))) for n, t in fields))
        case C.Array(elem=t, length=n):
            parts = ("array", str(n), key(t))
        case C.Ptr(inner=t) | C.Brand(brand=t):
            parts = (type(ty).__name__, key(t))
        case C.Exists(binders=names, body=t):
            parts = ("exists", str(len(names)), type_key(t, scope + names))
        case C.Foreach():
            parts = ("foreach", str(ty.indirect), str(len(foreach_binders(ty))), key(ty.source),
                     type_key(ty.body, scope + foreach_binders(ty)))
        case C.Fn():
            inner = scope + tuple(p.name for p in ty.forall)
            parts = ("fn", str(ty.unsafe), encode(tuple(map(str, sorted(ty.places)))),
                     encode(tuple(p.kind for p in ty.forall)),
                     encode(tuple(type_key(t, inner) for t in ty.params)), type_key(ty.ret, inner))
        case _:
            raise TypeError(f"cannot mangle type {ty!r}")
    return encode(parts)


def path_segments(name: str) -> tuple[str, ...]:
    """Decode linker segments for display, then escape bytes into C identifiers."""
    parts = []
    for part in name.split("::"):
        if re.fullmatch(r"[md](?:[0-9a-f]{2})+", part):
            try:
                part = bytes.fromhex(part[1:]).decode()
            except ValueError:
                pass
        # Escape underscores too, so punctuation cannot alias literal escapes.
        parts.append("".join(chr(b) if 48 <= b <= 57 or 65 <= b <= 90 or 97 <= b <= 122
                             else f"_{b:02x}" for b in part.encode()) or "_empty")
    return tuple(parts)


def mangle(path: tuple[str, ...], identity: str, salt: int = 0) -> str:
    digest = hashlib.sha256(encode(("kurs", identity, str(salt))).encode()).hexdigest()[:16]
    parts = ("kurs", *path, "h" + digest)
    return "_ZN" + "".join(f"{len(p)}{p}" for p in parts) + "E"
