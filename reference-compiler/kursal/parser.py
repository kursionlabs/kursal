"""A hand-written recursive-descent parser for Kursal source, building the AST of `syntax.py`.

The grammar is LL(1): the current token alone decides every choice. The parser never looks further
ahead and never backtracks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Never, Optional
from bisect import bisect_right

from . import syntax as S
from .sexpr import KursError, Position

KEYWORDS = frozenset(
    "use export module struct tuple enum fn extern impl trait instance const forall where as exposed unsafe "
    "closure exists foreach of in for while if else match let var return pack sizeof true false self _".split())

SCAN = re.compile(r"""
  (?P<ws>\s+|//[^\n]*|/\*(?:.|\n)*?\*/)
 |(?P<FSTRING_START>f")
 |(?P<BYTE>b'(?:\\x[0-9a-fA-F]{2}|\\.|[^'\\])')
 |(?P<STRING>"(?:\\.|[^"\\])*")
 |(?P<BRAND>'[A-Za-z0-9_]+)
 |(?P<FLOAT>[0-9][0-9_]*\.[0-9][0-9_]*(?:f32|f64)?|[0-9][0-9_]*(?:f32|f64))
 |(?P<INT>(?:0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|[0-9][0-9_]*)(?:i8|i16|i32|i64|isize|u8|u16|u32|u64|usize)?)
 |(?P<TUPLE_INDEX>\.[0-9]+)
 |(?P<NAME>[^\W\d]\w*)
 |(?P<OP>\.\.=|\.\{|\.\(|\.\.|::|->|=>|==|!=|<=|>=|<<|>>|\|\||&&|[-+*/%&|^!<>=.,;:()\[\]{}?])
""", re.VERBOSE)


@dataclass(slots=True)
class Tok:
    kind: str
    text: str
    line: int
    col: int
    start: int
    end: int


# ------------------------------------------------------------------ lexer --

class Lexer:
    """Tokens of a whole file. After `f"` the text is scanned in a mode of its own: FSTRING_TEXT up to a `{`
    (FSTRING_OPEN; `{{` and `}}` stand for the brace), then ordinary tokens up to the matching `}`
    (FSTRING_CLOSE; braces nest, so an interpolation may hold braces and f-strings of its own), then
    more text, until the closing quote (FSTRING_END). A text token is always produced, possibly empty."""

    def __init__(self, text: str, filename: str) -> None:
        self.text, self.filename, self.i = text, filename, 0
        self.lines = [0] + [m.end() for m in re.finditer("\n", text)]

    def loc(self, offset: int) -> Position:
        line = bisect_right(self.lines, offset)
        return line, offset - self.lines[line - 1] + 1

    def tok(self, kind: str, text: str, start: int, end: int) -> Tok:
        return Tok(kind, text, *self.loc(start), start, end)

    def run(self) -> list[Tok]:
        out: list[Tok] = []
        self.expression(out, nested=False)
        out.append(self.tok("EOF", "", len(self.text), len(self.text)))
        return out

    def fail(self, offset: int) -> Never:
        line, col = self.loc(offset)
        raise KursError(f"{self.filename}:{line}:{col}: syntax error")

    def expression(self, out: list[Tok], nested: bool) -> Optional[tuple[int, int]]:
        """Tokens up to the end of the text; in an interpolation, up to its closing `}`, whose span is returned."""
        text, depth = self.text, 0
        while True:
            m = SCAN.match(text, self.i)
            if m is None:
                if self.i < len(text):
                    self.fail(self.i)
                if nested:
                    raise KursError("unterminated f-string interpolation")
                return None
            start, self.i, kind = m.start(), m.end(), m.lastgroup
            assert kind is not None  # Every scanner alternative has a named group.
            if kind == "ws":
                continue
            word = m.group()
            if kind == "FSTRING_START":
                out.append(self.tok(kind, word, start, self.i))
                self.fstring(out)
                continue
            if kind == "NAME" and word in KEYWORDS:
                kind = word
            elif kind == "OP":
                kind = word
                if nested and word in ("{", ".{"):
                    depth += 1
                elif nested and word == "}":
                    if depth == 0:
                        return start, self.i
                    depth -= 1
            out.append(self.tok(kind, word, start, self.i))

    def fstring(self, out: list[Tok]) -> None:
        text, i = self.text, self.i
        while True:
            chunk, begin = [], i
            while True:
                if i >= len(text):
                    raise KursError("unterminated f-string")
                ch = text[i]
                if ch == "\\":
                    width = 4 if text[i + 1:i + 2] == "x" else 2
                    chunk.append(text[i:i + width])
                    i += width
                elif ch in "{}" and text[i + 1:i + 2] == ch:
                    chunk.append(ch)
                    i += 2
                elif ch == "}":
                    line, col = self.loc(begin)
                    raise KursError(f"{line}:{col}: a lone '}}' in an f-string")
                elif ch in '{"':
                    break
                else:
                    chunk.append(ch)
                    i += 1
            out.append(self.tok("FSTRING_TEXT", "".join(chunk), begin, i))
            if text[i] == '"':
                out.append(self.tok("FSTRING_END", '"', i, i + 1))
                self.i = i + 1
                return
            out.append(self.tok("FSTRING_OPEN", "{", i, i + 1))
            self.i = i + 1
            closing = self.expression(out, nested=True)
            assert closing is not None  # A nested scan returns its closing brace or raises.
            start, end = closing
            out.append(self.tok("FSTRING_CLOSE", "}", start, end))
            i = self.i


# ----------------------------------------------------------------- parser --

BLOCK_LIKE = frozenset(("{", "if", "match", "while", "for", "foreach"))
EXPR_START = BLOCK_LIKE | frozenset((
    "INT", "FLOAT", "BYTE", "STRING", "FSTRING_START", "true", "false", "NAME", "self", "(", "[", "sizeof",
    "-", "!", "*", "&", "unsafe", "|", "||", "return", "pack"))
LEVELS = (("||",), ("&&",), ("==", "!=", "<=", ">=", "<", ">"), ("|",), ("^",), ("&",), ("<<", ">>"),
          ("+", "-"), ("*", "/", "%"))
COMPARE = 2
STATEMENTS = (S.Let, S.Var, S.Assign, S.ExprStmt, S.Const)


class Parser:
    def __init__(self, tokens: list[Tok], filename: str) -> None:
        self.t, self.i, self.filename = tokens, 0, filename

    # ---- tokens
    @property
    def k(self) -> str:
        return self.t[self.i].kind

    def pos(self) -> Position:
        t = self.t[self.i]
        return (t.line, t.col)

    def next(self) -> Tok:
        tok = self.t[self.i]
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def eat(self, kind: str) -> Optional[Tok]:
        if self.k == kind:
            return self.next()
        return None

    def expect(self, kind: str) -> Tok:
        if self.k != kind:
            self.fail()
        return self.next()

    def fail(self) -> Never:
        t = self.t[self.i]
        raise KursError(f"{self.filename}:{t.line}:{t.col}: syntax error")

    def list_until[T](self, close: str, item: Callable[[], T]) -> list[T]:
        """`item ("," item)* ","? close`, possibly empty."""
        out = []
        while self.k != close:
            out.append(item())
            if not self.eat(","):
                break
        self.expect(close)
        return out

    def name(self) -> str:
        return self.expect("NAME").text

    # ---- items
    def file(self) -> list[S.Item]:
        items = []
        while self.k != "EOF":
            items.append(self.item())
        return items

    def item(self) -> S.Item:
        forall = self.forall() if self.k == "forall" else None
        decl = self.item_body()
        if forall:
            decl.forall = forall
        return decl

    def brands(self) -> tuple[str, ...]:
        """`BRAND ("," BRAND)* "."`: nonempty, with no trailing comma."""
        brands = [self.expect("BRAND").text]
        while self.eat(","):
            brands.append(self.expect("BRAND").text)
        self.expect(".")
        return tuple(brands)

    def forall_param(self) -> str:
        if self.k not in ("NAME", "BRAND"):
            self.fail()
        return self.next().text

    def forall(self) -> S.Forall:
        self.expect("forall")
        params, determined, premises = [self.forall_param()], [], []
        while self.eat(","):
            params.append(self.forall_param())
        if self.eat("->"):
            determined = [self.forall_param()]
            while self.eat(","):
                determined.append(self.forall_param())
        if self.eat("where"):
            premises = [self.premise()]
            while self.eat(","):
                premises.append(self.premise())
        self.expect(".")
        return S.Forall(tuple(params), tuple(determined), tuple(premises))

    def premise(self) -> tuple[S.NamePath, S.TypeArgs]:
        path = self.path()
        self.expect("[")
        args = self.type_args()
        self.expect("]")
        return (path, args)

    def item_body(self) -> S.Item:
        k, start = self.k, self.pos()
        if k == "use" or k == "export":
            self.next()
            tree = self.use_tree()
            self.expect(";")
            return S.Use(tree, k == "export", start)
        if k == "module":
            self.next()
            names = [self.name()]
            while self.eat("::"):
                names.append(self.name())
            self.expect(";")
            return S.Module(tuple(names), start)
        if k in ("exposed", "struct", "tuple", "enum"):
            return self.aggregate()
        if k == "unsafe":
            self.next()
            if self.k == "instance":
                return self.instance(start, unsafe=True)
            return self.fn_decl(start, unsafe=True)
        if k == "instance":
            return self.instance(start)
        if k == "fn":
            return self.fn_decl(start)
        if k == "extern":
            return self.extern()
        if k == "impl":
            return self.impl()
        if k == "trait":
            self.next()
            name = self.name()
            self.expect("{")
            methods = []
            while self.k != "}":
                methods.append(self.fn_decl())
            self.next()
            return S.TraitDecl(name, tuple(methods), None, start)
        if k == "const":
            return self.const_decl()
        self.fail()

    def use_tree(self) -> S.UseTree:
        names, star = [self.name()], False
        children: tuple[S.UseTree, ...] = ()
        alias: Optional[str] = None
        while self.eat("::"):
            if self.k == "NAME":
                names.append(self.next().text)
            elif self.eat("*"):
                star = True
                break
            elif self.eat("{"):
                children = tuple(self.list_until("}", self.use_tree))
                break
            else:
                self.fail()
        if self.eat("as"):
            alias = self.name()
        return (tuple(names), star, children, alias)

    def path(self) -> S.NamePath:
        names = [self.name()]
        while self.eat("::"):
            names.append(self.name())
        return tuple(names)

    def aggregate(self) -> S.Struct | S.TupleDecl | S.EnumDecl:
        start, exposed = self.pos(), None
        if self.eat("exposed"):
            scope = None
            if self.eat("("):
                scope = self.path()
                self.expect(")")
            exposed = S.Exposed(scope)
        kind = self.k
        if kind not in ("struct", "tuple", "enum"):
            self.fail()
        self.next()
        name = self.name()
        linear = bool(self.eat("!"))
        if kind == "struct":
            self.expect("{")
            fields = self.list_until("}", self.field)
            return S.Struct(name, linear, tuple(fields), exposed, None, start)
        if kind == "tuple":
            self.expect("(")
            elems = self.list_until(")", self.type)
            self.expect(";")
            return S.TupleDecl(name, linear, tuple(elems), exposed, None, start)
        self.expect("{")
        return S.EnumDecl(name, linear, tuple(self.list_until("}", self.variant)), exposed, None, start)

    def field(self) -> tuple[str, S.Type]:
        name = self.name()
        self.expect(":")
        return (name, self.type())

    def variant(self) -> tuple[str, tuple[S.Type, ...]]:
        name, payload = self.name(), []
        if self.eat("("):
            payload = [self.type()]
            while self.eat(","):
                if self.k == ")":
                    break
                payload.append(self.type())
            self.expect(")")
        return (name, tuple(payload))

    def params(self) -> list[S.Param]:
        out: list[S.Param] = []
        while self.k != ")":
            lend = bool(self.eat("&"))
            if not out and self.eat("self"):                 # `self` only first
                out.append(S.Param("self", self.type() if self.eat(":") else None, lend))
            else:
                name = self.name()
                self.expect(":")
                out.append(S.Param(name, self.ptype(), lend))
            if not self.eat(","):
                break
        return out

    def fn_header(self) -> tuple[str, tuple[S.Param, ...], Optional[S.Type]]:
        """`fn NAME "(" params? ")" ("->" type)?`, shared by definitions and externs."""
        self.expect("fn")
        name = self.name()
        self.expect("(")
        params = tuple(self.params())
        self.expect(")")
        return name, params, self.type() if self.eat("->") else None

    def fn_decl(self, start: Optional[Position] = None, unsafe: bool = False) -> S.Fn:
        """`"unsafe"? fn_header (";" | block)`; `start` and `unsafe` are given when the caller read the `unsafe`."""
        if start is None:
            start, unsafe = self.pos(), bool(self.eat("unsafe"))
        name, params, ret = self.fn_header()
        body = None if self.eat(";") else self.block()
        return S.Fn(name, params, ret, body, unsafe, None, start)

    def extern(self) -> S.Extern:
        start = self.pos()
        self.expect("extern")
        abi = S.unescape(self.expect("STRING").text[1:-1])
        name, params, ret = self.fn_header()
        self.expect(";")
        return S.Extern(abi, name, params, ret, start)

    def const_decl(self) -> S.Const:
        start = self.pos()
        self.expect("const")
        name = self.name()
        self.expect(":")
        ty = self.type()
        self.expect("=")
        value = self.expr()
        self.expect(";")
        return S.Const(name, ty, value, None, start)

    def impl(self) -> S.Impl:
        start = self.pos()
        self.expect("impl")
        ty = self.type()
        self.expect("{")
        decls = []
        while self.k != "}":
            forall = self.forall() if self.k == "forall" else None
            decl = self.const_decl() if self.k == "const" else self.fn_decl()
            decl.forall = forall
            decls.append(decl)
        self.next()
        return S.Impl(ty, tuple(decls), None, start)

    def instance(self, start: Position, unsafe: bool = False) -> S.InstanceDecl:
        self.expect("instance")
        ty = self.type()
        self.expect("{")
        methods = []
        while self.k != "}":
            methods.append(self.fn_decl())
        self.next()
        return S.InstanceDecl(ty, tuple(methods), unsafe, None, start)

    # ---- types
    def type(self, parameter: bool = False) -> S.Type:
        """A type; with `parameter`, the type of a parameter, which may also be a `closure(...)` type whose
        environment is left unnamed."""
        k = self.k
        if k == "NAME":
            return self.named_type()
        if k == "*":
            self.next()
            return S.TPtr(self.type())
        if k == "!":
            self.next()
            return S.TNever()
        if k == "(":
            self.next()
            if self.eat(")"):
                return S.TTuple(())
            first = self.type()
            if self.eat(")"):
                return first
            self.expect(",")
            return S.TTuple((first, *self.list_until(")", self.type)))
        if k == "[":
            self.next()
            elem = self.type()
            self.expect(";")
            length = self.expr()
            self.expect("]")
            return S.TArray(elem, length)
        if k == "unsafe" or k == "fn":
            unsafe = bool(self.eat("unsafe"))
            self.expect("fn")
            self.expect("(")
            params = self.fn_params()
            self.expect("->")
            return S.TFn(tuple(params), self.type(), unsafe)
        if k == "closure":
            self.next()
            env = None
            if self.eat("["):
                env = self.type()
                self.expect("]")
            elif not parameter:
                self.fail()
            return self.closure_rest(env)
        if k == "exists":
            self.next()
            return S.TExists(self.brands(), self.type())
        if k == "foreach":
            self.next()
            indirect = bool(self.eat("*"))
            binder = self.name()
            self.expect("of")
            source = self.type()
            self.expect("=>")
            return S.TForeach(binder, indirect, source, self.type())
        if k == "forall":
            self.next()
            quantified = [self.forall_param()]
            while self.eat(","):
                quantified.append(self.forall_param())
            self.expect(".")
            return S.TForall(tuple(quantified), self.type())
        self.fail()

    def closure_rest(self, env: Optional[S.Type]) -> S.TClosure:
        self.expect("(")
        params = self.fn_params()
        self.expect("->")
        return S.TClosure(env, tuple(params), self.type())

    def ptype(self) -> S.Type:
        return self.type(parameter=True)

    def fn_params(self) -> list[tuple[bool, S.Type]]:
        """The slots of a `fn(...)` or `closure(...)` type, after its `(`; the `)` is consumed."""
        return self.list_until(")", self.fn_param)

    def fn_param(self) -> tuple[bool, S.Type]:
        if self.eat("&"):
            if self.k not in ("NAME", "_"):
                self.fail()
            self.next()
            self.expect(":")
            return (True, self.type())
        return (False, self.ptype())

    def named_type(self) -> S.TName:
        path = self.path()
        linear = bool(self.eat("!"))
        args = S.TypeArgs((), 0)
        if self.eat("["):
            args = self.type_args()
            self.expect("]")
        return S.TName(path, linear, args.args, args.determined)

    def type_arg(self) -> S.Type:
        if self.k == "BRAND":
            return S.TBrand(self.next().text)
        if self.eat("_"):
            return S.TIgnored()
        return self.type()

    def type_args(self) -> S.TypeArgs:
        """`a, b` or `a, b -> c`, with an optional trailing comma; ends before the `]`."""
        args, split = [self.type_arg()], None
        while True:
            if self.eat(","):
                if self.k == "]":
                    break
                args.append(self.type_arg())
            elif self.k == "->" and split is None:
                self.next()
                split = len(args)
                args.append(self.type_arg())
            else:
                break
        return S.TypeArgs(tuple(args), 0 if split is None else len(args) - split)

    # ---- statements
    def block(self) -> S.Block:
        start = self.pos()
        self.expect("{")
        items: list[S.Statement] = []
        tail: Optional[S.Expr] = None
        while self.k != "}":
            item = self.statement()
            if isinstance(item, STATEMENTS):
                items.append(item)
            else:
                tail = item
        self.next()
        return S.Block(tuple(items), tail, start)

    def statement(self) -> S.Statement | S.Expr:
        """A statement; or, when the block ends right after it, a bare expression (its tail)."""
        k, start = self.k, self.pos()
        e: S.Expr
        if k == "let":
            return self.let_stmt()
        if k == "var":
            self.next()
            name = self.name()
            ty = self.type() if self.eat(":") else None
            self.expect("=")
            value = self.expr()
            self.expect(";")
            return S.Var(name, ty, value, start)
        if k == "const":
            return self.const_decl()
        if k in BLOCK_LIKE:
            e = self.block_like()
            if not self.eat(";") and self.k == "}" and not isinstance(e, (S.While, S.For)):
                return e
            return S.ExprStmt(e, start)
        if k == "unsafe":
            self.next()
            star = self.pos()
            if self.eat("*"):
                target = self.unary()
                if self.eat("="):                             # a store: `unsafe *p = v;`
                    value = self.expr()
                    self.expect(";")
                    return S.ExprStmt(S.Unary("unsafe", S.UnsafeStore(target, value, start), start), start)
                operand: S.Expr = S.Unary("*", target, star)
            else:
                operand = self.unary()
            e = self.oexpr(S.Unary("unsafe", operand, start))
        else:
            e = self.expr()
        if self.eat("="):
            if not assignable(e):
                self.fail()
            value = self.expr()
            self.expect(";")
            return S.Assign(e, value, start)
        if self.eat(";"):
            return S.ExprStmt(e, start)
        if self.k == "}" and not isinstance(e, S.Return):
            return e
        self.fail()

    def let_stmt(self) -> S.Let:
        start = self.pos()
        self.expect("let")
        brands = self.brands() if self.eat("exists") else ()
        pattern = self.pattern()
        ty = self.type() if self.eat(":") else None
        self.expect("=")
        value = self.expr()
        self.expect(";")
        return S.Let(pattern, ty, value, brands, start)

    def pattern(self) -> S.Pattern:
        k = self.k
        if k == "NAME":
            return S.PBind(self.next().text)
        if k == "_":
            self.next()
            return S.PWild()
        if k == "(":
            self.next()
            if self.eat(")"):
                return S.PTuple(())
            first = self.pattern()
            self.expect(",")
            return S.PTuple((first, *self.list_until(")", self.pattern)))
        if k == "[":
            self.next()
            first = self.pattern()
            elems = [first]
            while self.eat(","):
                if self.k == "]":
                    break
                elems.append(self.pattern())
            self.expect("]")
            return S.PArray(tuple(elems))
        if k == "{":
            self.next()
            fields, rest = self.braced(self.field_pat)
            return S.PStruct(tuple(fields), rest)
        self.fail()

    def field_pat(self) -> tuple[str, S.Pattern]:
        name = self.name()
        return (name, self.pattern() if self.eat(":") else S.PBind(name))

    def braced[T](self, item: Callable[[], T]) -> tuple[list[T], Optional[str]]:
        """After a `{`: `item ("," item)* ("," ..NAME)? ","?` or just `..NAME`, then `}`; (items, spread)."""
        items, spread = [], None
        if self.k == "..":
            spread = self.spread()
        elif self.k != "}":
            items.append(item())
            while self.eat(","):
                if self.k == "}":
                    break
                if self.k == "..":
                    spread = self.spread()
                    self.eat(",")
                    break
                items.append(item())
        self.expect("}")
        return items, spread

    def spread(self) -> str:
        self.expect("..")
        return self.name()

    # ---- expressions
    def expr(self) -> S.Expr:
        """A whole expression: an operator expression, or a block-like, closure, `return` or `pack` form."""
        k = self.k
        if k in BLOCK_LIKE:
            return self.block_like()
        if k == "|" or k == "||":
            return self.closure()
        if k == "return":
            start = self.pos()
            self.next()
            return S.Return(self.expr() if self.k in EXPR_START else None, start)
        if k == "pack":
            start = self.pos()
            self.next()
            return S.Pack(self.brands(), self.expr(), start)
        return self.oexpr()

    def block_like(self) -> S.Block | S.If | S.Match | S.While | S.For | S.Foreach:
        k, start = self.k, self.pos()
        if k == "{":
            return self.block()
        self.next()
        if k == "if":
            cond = self.oexpr()
            then = self.block()
            other: Optional[S.Block | S.If] = None
            if self.eat("else"):
                if self.k == "{":
                    other = self.block()
                elif self.k == "if":
                    nested = self.block_like()
                    assert isinstance(nested, S.If)
                    other = nested
                else:
                    self.fail()
            return S.If(cond, then, other, start)
        if k == "match":
            return self.match_rest(start)
        if k == "while":
            cond = self.oexpr()
            return S.While(cond, self.block(), start)
        if k == "for":
            name = self.name()
            self.expect("in")
            iterator = self.oexpr()
            return S.For(name, iterator, self.block(), start)
        name = self.name()                                    # foreach
        binder = self.name() if self.eat(":") else None
        self.expect("of")
        source = self.oexpr()
        return S.Foreach(name, binder, source, self.block(), start)

    def match_rest(self, start: Position) -> S.Match:
        scrutinee = self.oexpr()
        self.expect("{")
        arms = []
        while self.k != "}":
            at = self.pos()
            if self.eat("_"):                                 # the wildcard arm is the last one
                self.expect("=>")
                body = self.expr()
                self.eat(",")
                arms.append(S.Arm((S.Case("wild", pos=at),), body, at))
                break
            cases = [self.case()]
            while self.eat("|"):
                cases.append(self.case())
            self.expect("=>")
            block_like = self.k in BLOCK_LIKE                 # its comma is optional; any other body needs one
            body = self.expr()                                # unless the arm is last (and is not a `return`)
            if not (self.eat(",") or block_like or self.k == "}" and not isinstance(body, S.Return)):
                self.fail()
            arms.append(S.Arm(tuple(cases), body, at))
        self.expect("}")
        return S.Match(scrutinee, tuple(arms), start)

    def case(self) -> S.Case:
        start = self.pos()
        if self.k in ("INT", "BYTE", "-"):
            low = self.lit_value()
            high = self.lit_value() if self.eat("..=") else low
            return S.Case("value", None, None, low, high, start)
        path = self.expr_path()
        binders = None
        if self.eat("("):
            binders = self.list_until(")", self.binder)
        return S.Case("variant", path, tuple(binders) if binders else None, pos=start)

    def binder(self) -> str:
        if self.k not in ("NAME", "_"):
            self.fail()
        return self.next().text

    def lit_value(self) -> S.Lit:
        negative = bool(self.eat("-"))
        if self.k not in ("INT", "BYTE"):
            self.fail()
        value = self.literal()
        if negative:
            assert isinstance(value.value, int)
            value.value = -value.value
        return value

    def closure(self) -> S.Closure:
        start, params = self.pos(), []
        if not self.eat("||"):
            self.expect("|")
            while self.k != "|":
                lend = bool(self.eat("&"))
                name = self.name()
                params.append((lend, name, self.ptype() if self.eat(":") else None))
                if not self.eat(","):
                    break
            self.expect("|")
        if self.eat("->"):
            ret = self.type()
            return S.Closure(tuple(params), ret, self.block(), start)
        return S.Closure(tuple(params), None, self.expr(), start)

    def oexpr(self, first: Optional[S.Expr] = None) -> S.Expr:
        return self.level(0, first)

    def level(self, precedence: int, first: Optional[S.Expr] = None) -> S.Expr:
        if precedence == len(LEVELS):
            return self.cast(first)
        start = first.pos if first is not None else self.pos()
        left = self.level(precedence + 1, first)
        ops = LEVELS[precedence]
        while self.k in ops:
            op = self.next().text
            left = S.Binop(op, left, self.level(precedence + 1), start)
            if precedence == COMPARE:                                  # comparisons do not chain
                break
        return left

    def cast(self, first: Optional[S.Expr]) -> S.Expr:
        start = first.pos if first is not None else self.pos()
        left = first if first is not None else self.unary()
        while self.eat("as"):
            left = S.Cast(left, self.type(), start)
        return left

    def unary(self) -> S.Expr:
        k, start = self.k, self.pos()
        if k in ("-", "!", "*", "&", "unsafe"):
            self.next()
            return S.Unary(k, self.unary(), start)
        return self.postfix()

    def postfix(self) -> S.Expr:
        start = self.pos()
        e, after_index = self.atom(), False
        while True:
            k = self.k
            if k == ".":
                self.next()
                e, after_index = S.Field(e, self.name(), start), False
            elif k == "(":
                self.next()
                e, after_index = S.Call(e, tuple(self.list_until(")", self.expr)), start), False
            elif k == "?":
                self.next()
                e, after_index = S.Try(e, start), False
            elif k == "TUPLE_INDEX" and not after_index:      # `p.0.1` is written `(p.0).1`
                e, after_index = S.Index(e, int(self.next().text[1:]), start), True
            else:
                return e

    def atom(self) -> S.Expr:
        k, start = self.k, self.pos()
        if k in ("INT", "FLOAT", "BYTE", "STRING", "true", "false"):
            return self.literal()
        if k == "FSTRING_START":
            self.next()
            parts: list[str | S.Expr] = [S.unescape(self.expect("FSTRING_TEXT").text)]
            while self.eat("FSTRING_OPEN"):
                parts.append(self.expr())
                self.expect("FSTRING_CLOSE")
                parts.append(S.unescape(self.expect("FSTRING_TEXT").text))
            self.expect("FSTRING_END")
            return S.FStr(tuple(parts), start)
        if k == "(":
            self.next()
            if self.eat(")"):
                return S.Tuple((), start)
            first = self.expr()
            if self.eat(")"):
                return first
            self.expect(",")
            return S.Tuple((first, *self.list_until(")", self.expr)), start)
        if k == "[":
            self.next()
            elems, fill = [], False
            if self.k != "]":
                elems.append(self.expr())
                while self.eat(","):
                    if self.k == "]":
                        break
                    if self.eat(".."):
                        fill = True
                        self.eat(",")
                        break
                    elems.append(self.expr())
            self.expect("]")
            return S.Array(tuple(elems), fill, start)
        if k == "sizeof":
            self.next()
            self.expect("[")
            ty = self.type()
            self.expect("]")
            self.expect("(")
            self.expect(")")
            return S.Sizeof(ty, start)
        path = self.expr_path()
        if self.eat(".{"):
            fields, spread = self.braced(self.field_init)
            return S.StructLit(path, tuple(fields), spread, start)
        if self.eat(".("):
            return S.Call(path, tuple(self.list_until(")", self.expr)), start)
        return path

    def field_init(self) -> tuple[str, Optional[S.Expr]]:
        name = self.name()
        return (name, self.expr() if self.eat(":") else None)

    def expr_path(self) -> S.Path:
        start, segments = self.pos(), []
        while True:
            if self.k not in ("NAME", "self"):
                self.fail()
            name = self.next().text
            args: tuple[S.Type, ...] = ()
            linear = bool(self.eat("!"))
            if self.eat("["):
                args = self.type_args().args
                self.expect("]")
            segments.append((name, linear, args))
            if not self.eat("::"):
                return S.Path(tuple(segments), start)

    def literal(self) -> S.Lit:
        if self.k not in ("INT", "FLOAT", "BYTE", "STRING", "true", "false"):
            self.fail()
        tok = self.next()
        k, text, p = tok.kind, tok.text, (tok.line, tok.col)
        if k == "INT":
            digits, suffix = S.split_suffix(text, S.INT_SUFFIXES)
            digits = digits.replace("_", "")
            base = {"0x": 16, "0o": 8, "0b": 2}.get(digits[:2], 10)
            value = int(digits[2:] if base != 10 else digits, base)
            if value >= 1 << 64:   # no integer type holds it; its magnitude must fit 64 bits
                raise KursError(f"{self.filename}:{tok.line}:{tok.col}: integer literal out of range")
            return S.Lit("int", value, suffix, p)
        if k == "FLOAT":
            digits, suffix = S.split_suffix(text, ("f32", "f64"))
            return S.Lit("float", digits.replace("_", ""), suffix, p)
        if k == "BYTE":
            return S.Lit("int", ord(S.unescape(text[2:-1])), "u8", p)
        if k == "STRING":
            return S.Lit("str", S.unescape(text[1:-1]), None, p)
        return S.Lit("bool", k == "true", None, p)


def assignable(e: S.Expr) -> bool:
    """The left side of `=`: a name, then fields and tuple elements (`x`, `self.n`, `(p.0).1`)."""
    if isinstance(e, S.Path):
        return len(e.segments) == 1 and not e.segments[0][1] and not e.segments[0][2]
    return isinstance(e, (S.Field, S.Index)) and assignable(e.value)


def parse(text: str, filename: str = "<input>") -> list[S.Item]:
    p = Parser(Lexer(text, filename).run(), filename)
    return p.file()


def parse_expr(text: str, filename: str = "<expression>") -> S.Expr:
    p = Parser(Lexer(text, filename).run(), filename)
    e = p.expr()
    p.expect("EOF")
    return e
