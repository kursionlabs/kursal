"""Type-directed generation of Kursal programs that the front end should accept.

The generator writes a fixed prelude of declarations (linear and `Copy` structs and enums, a generic
linear cell, a branded tag, consumers, lenders, a trait and its instances), then random helper functions
and a `main`. Each body is generated statement by statement over an environment that tracks, for each
local, its type and whether it holds a value: a linear value is moved exactly once on every path, a
place parameter is full again at every return, branches end in the same state (the generator consumes in
one branch what the other consumed), a loop body leaves the linear state it found (it may consume a `var`
and assign it again, or lend outer values, but not move them), and an early `return` consumes everything
live first. So most programs are valid, and the ones the front end accepts are where the checker must
agree with it."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from typing import Optional

PRELUDE = """\
use core::cap::{Brand, fresh};

exposed struct P { a: u32, b: u32 }
exposed struct T! { n: u32 }
exposed struct Bx! { t: T!, n: u32 }
exposed enum E! { A(T!), B(u32) }
exposed enum C { X(u32), Y }
forall U. exposed struct Cell! { v: U }
forall U. exposed enum Opt { Some(U), None }
forall 'k. exposed struct Tag! { marker: Brand['k], n: u32 }
forall 'k. exposed struct Tick { marker: Brand['k] }

fn mk_t(n: u32) -> T! { T!.{ n } }
fn eat_t(t: T!) -> u32 { let { n } = t; n }
fn peek_t(&t: T!) -> u32 { t.n }
fn bump_t(&t: T!) -> u32 { let { n } = t; t = T!.{ n: n + 1u32 }; n }
fn eat_bx(b: Bx!) -> u32 { let { t, n } = b; eat_t(t) + n }
fn peek_bx(&b: Bx!) -> u32 { peek_t(&b.t) + b.n }
fn eat_e(e: E!) -> u32 { match e { E!::A(t) => eat_t(t), E!::B(n) => n } }
fn eat_cell(c: Cell![T!]) -> u32 { let { v } = c; eat_t(v) }
fn eat_opt(o: Opt[T!]) -> u32 { match o { Opt::Some(t) => eat_t(t), Opt::None => 0u32 } }
fn eat_pair(p: (u32, T!)) -> u32 { let (a, t) = p; a + eat_t(t) }
forall 'k. fn eat_tag(t: Tag!['k]) -> u32 { let { marker, n } = t; n }
forall 'k. fn peek_tag(&t: Tag!['k]) -> u32 { t.n }
fn mint(n: u32) -> exists 'a. Tag!['a] { let exists 'a. w = fresh(); pack 'a. Tag!.{ marker: w, n } }
forall 'k. fn bump_tag(&t: Tag!['k]) -> u32 { let { marker, n } = t; t = Tag!.{ marker, n: n + 1u32 }; n }
forall 'k. fn eat_opt_tag(o: Opt[Tag!['k]]) -> u32 { match o { Opt::Some(t) => eat_tag(t), Opt::None => 0u32 } }
forall 'k. fn eat_pair_tag(p: (u32, Tag!['k])) -> u32 { let (a, t) = p; a + eat_tag(t) }
forall 'k. fn eat_cell_tag(c: Cell![Tag!['k]]) -> u32 { let { v } = c; eat_tag(v) }
forall 'k. fn tick(k: Tick['k], n: u32) -> u32 { let { marker } = k; n + 1u32 }
fn two_t(&a: T!, &b: T!) -> u32 { peek_t(&a) + bump_t(&b) }
fn three_t(&a: T!, &b: T!, &c: T!) -> u32 { peek_t(&a) + peek_t(&b) + peek_t(&c) }
forall U. fn id(x: U) -> U { x }
forall U. fn swap_in(&p: U, x: U) -> U { let old = p; p = x; old }
forall U where Copy[U]. fn dup(x: U) -> (U, U) { (x, x) }
forall U, V. fn apply(x: U, f: closure(U) -> V) -> V { f(x) }

impl T! {
    fn get(&self) -> u32 { self.n }
    fn into_n(self) -> u32 { let { n } = self; n }
}

forall Self. trait Weigh {
    fn weigh(self: Self) -> u32;
}
instance Weigh[T!] {
    fn weigh(self: T!) -> u32 { eat_t(self) }
}
instance Weigh[P] {
    fn weigh(self: P) -> u32 { self.a + self.b }
}
forall U where Weigh[U]. fn weigh_it(x: U) -> u32 { x.weigh() }
"""

# The types the generator makes values of; a `Tag!` is branded, made only where a brand is open.
COPY_TYPES = ["u32", "u32", "u32", "bool", "P", "C"]
LINEAR_TYPES = ["T!", "T!", "T!", "Bx!", "E!", "Cell![T!]", "Opt[T!]", "(u32, T!)"]


def linear(ty: str) -> bool:
    return ty.endswith("!") or "!" in ty or ty.startswith("closure!")


def payload(ty: str) -> Optional[str]:
    """The linear payload type of a container type: `Opt[L]`, `(u32, L)`, `Cell![L]`."""
    for head, tail in (("Opt[", "]"), ("(u32, ", ")"), ("Cell![", "]")):
        if ty.startswith(head) and ty.endswith(tail):
            return ty[len(head):-len(tail)]
    return None


def brand_of(ty: str) -> Optional[str]:
    i = ty.find("Tag!['")
    return ty[i + 6:ty.index("]", i)] if i >= 0 else None


@dataclass
class Var:
    name: str
    ty: str
    live: bool = True
    mutable: bool = False
    place: bool = False      # a place parameter: full again at every return
    frozen: bool = False     # an outer linear value inside a loop body: lent, never moved
    lendable: bool = True


class Env:
    def __init__(self) -> None:
        self.vars: dict[str, Var] = {}
        self.brands: list[str] = []
        self.diverged = False

    def copy(self) -> Env:
        e = Env()
        e.vars = {k: replace(v) for k, v in self.vars.items()}
        e.brands = list(self.brands)
        e.diverged = self.diverged
        return e

    def live(self, ty: Optional[str] = None, movable: bool = False) -> list[Var]:
        return [v for v in self.vars.values() if v.live and (ty is None or v.ty == ty)
                and not (movable and v.frozen)]


@dataclass
class Fn:
    name: str
    params: list[tuple[str, str, bool]]   # (name, type, place)
    ret: str
    brand: Optional[str] = None


class Gen:
    def __init__(self, seed: int, size: int = 3) -> None:
        self.r = random.Random(seed)
        self.size = size
        self.counter = 0
        self.fns: list[Fn] = []
        self.ret = "u32"

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def chance(self, p: float) -> bool:
        return self.r.random() < p

    def witness(self, env: Env, brand: str) -> Optional[str]:
        return next((v.name for v in env.live() if v.ty == f"Brand['{brand}]"), None)

    def makeable(self, ty: str, env: Env) -> bool:
        """Whether a value of `ty` can be made here: a branded one needs its brand's witness."""
        brand = brand_of(ty)
        return brand is None or self.witness(env, brand) is not None

    def linear_types(self, env: Env) -> list[str]:
        out = list(LINEAR_TYPES)
        for v in env.live():
            if v.ty.startswith("Brand['"):
                tag = f"Tag!{v.ty[5:]}"
                out += [tag, tag, f"Opt[{tag}]", f"(u32, {tag})", f"Cell![{tag}]"]
        return out

    def eat_fn(self, ty: str) -> str:
        return "eat_t" if ty == "T!" else "eat_tag"

    # ------------------------------------------------------------------ expressions --

    def u32(self, env: Env, depth: int) -> str:
        r = self.r
        if depth <= 0:
            choices = ["lit", "var", "lit"]
        else:
            choices = ["lit", "var", "var", "add", "field", "eat", "eat", "lend", "lend", "if", "match_c",
                       "block", "call", "method", "weigh", "closure", "match_opt", "apply", "tag"]
        kind = r.choice(choices)
        if kind == "var":
            vs = env.live("u32")
            if vs:
                return r.choice(vs).name
        if kind == "add":
            return f"{self.u32(env, depth - 1)} + {self.u32(env, depth - 1)}"
        if kind == "field":
            ps = env.live("P")
            if ps:
                return f"{r.choice(ps).name}.{r.choice('ab')}"
        if kind == "eat":
            vs = [v for v in env.live(movable=True) if linear(v.ty) and not v.ty.startswith("closure")]
            if vs:
                return self.consume(r.choice(vs), env)
            return f"eat_t({self.expr('T!', env, depth - 1)})"
        if kind == "lend":
            got = self.lend(env)
            if got:
                return got
        if kind == "if":
            cond = self.boolean(env, depth - 1)
            a, b = self.branches(env, depth - 1, "u32")
            return f"(if {cond} {a} else {b})"
        if kind == "match_c":
            return f"({self.match_c(env, depth - 1, 'u32')})"
        if kind == "match_opt":
            if self.chance(0.5):
                got = self.match_place(env, depth - 1, "u32")
                if got:
                    return f"({got})"
            return f"({self.match_linear(env, depth - 1, 'u32')})"
        if kind == "block":
            return f"({self.block(env, depth - 1, 'u32')})"
        if kind == "call":
            got = self.call(env, depth - 1, "u32")
            if got:
                return got
        if kind == "method":
            ts = env.live("T!")
            if ts:
                v = r.choice(ts)
                if self.chance(0.5):
                    return f"&{v.name}.get()"
                if not v.frozen:
                    v.live = False
                    return f"{v.name}.into_n()"
        if kind == "weigh":
            if self.chance(0.5):
                return f"weigh_it(P.{{ a: {self.u32(env, 0)}, b: {self.u32(env, 0)} }})"
            return f"weigh_it({self.expr('T!', env, depth - 1)})"
        if kind == "closure":
            vs = env.live("T!", movable=True)
            if vs:
                v = r.choice(vs)
                v.live = False
                return f"apply({self.u32(env, 0)}, |y: u32| y + eat_t({v.name}))"
            return f"apply({self.u32(env, 0)}, |y: u32| y + 1u32)"
        if kind == "apply":
            return f"apply({self.expr('T!', env, depth - 1)}, |t: T!| eat_t(t))"
        if kind == "tag":
            tags = [v for v in env.live() if v.ty.startswith("Tag!")]
            if tags:
                return f"peek_tag(&{r.choice(tags).name})"
        return f"{r.randrange(0, 20)}u32"

    def lend(self, env: Env) -> Optional[str]:
        """A call that lends a live local and leaves it full."""
        r = self.r
        options = []
        for v in env.live():
            if not v.lendable:
                continue
            if v.ty == "T!":
                options += [f"peek_t(&{v.name})", f"bump_t(&{v.name})", f"&{v.name}.get()"]
            elif v.ty == "Bx!":
                options += [f"peek_bx(&{v.name})", f"peek_t(&{v.name}.t)", f"bump_t(&{v.name}.t)"]
            elif v.ty == "(u32, T!)":
                options += [f"peek_t(&{v.name}.1)", f"bump_t(&{v.name}.1)"]
            elif v.ty == "Cell![T!]":
                options += [f"peek_t(&{v.name}.v)", f"bump_t(&{v.name}.v)", f"&{v.name}.v.get()"]
            elif v.ty.startswith("Tag!"):
                options += [f"peek_tag(&{v.name})", f"bump_tag(&{v.name})"]
            elif v.ty.startswith("(u32, Tag!"):
                options += [f"peek_tag(&{v.name}.1)", f"bump_tag(&{v.name}.1)"]
            elif v.ty.startswith("Cell![Tag!"):
                options += [f"peek_tag(&{v.name}.v)", f"bump_tag(&{v.name}.v)"]
        return r.choice(options) if options else None

    def t_places(self, env: Env) -> list[str]:
        """The `T!` places that may be lent here: locals, and fields of locals that hold one."""
        out = []
        for v in env.live():
            if not v.lendable:
                continue
            if v.ty == "T!":
                out.append(v.name)
            elif v.ty == "Bx!":
                out.append(f"{v.name}.t")
            elif v.ty == "(u32, T!)":
                out.append(f"{v.name}.1")
            elif v.ty == "Cell![T!]":
                out.append(f"{v.name}.v")
        return out

    def boolean(self, env: Env, depth: int) -> str:
        r = self.r
        kind = r.choice(["lit", "cmp", "cmp", "not", "and", "var"] if depth > 0 else ["lit", "cmp", "var"])
        if kind == "var":
            vs = env.live("bool")
            if vs:
                return r.choice(vs).name
        if kind == "cmp":
            return f"{self.u32(env, max(depth - 1, 0))} {r.choice(['<', '==', '!=', '>='])} {self.u32(env, 0)}"
        if kind == "not":
            return f"!({self.boolean(env, depth - 1)})"
        if kind == "and":
            # The right operand may not run: it must not move anything.
            left = self.boolean(env, depth - 1)
            right = self.pure_bool(env)
            return f"({left} {r.choice(['&&', '||'])} {right})"
        return r.choice(["true", "false"])

    def pure_bool(self, env: Env) -> str:
        r = self.r
        lent = self.lend(env) if self.chance(0.5) else None
        left = lent or (r.choice(env.live("u32")).name if env.live("u32") else f"{r.randrange(5)}u32")
        return f"{left} < {r.randrange(1, 9)}u32"

    def expr(self, ty: str, env: Env, depth: int) -> str:
        r = self.r
        if ty == "u32":
            return self.u32(env, depth)
        if ty == "bool":
            return self.boolean(env, depth)
        movable = env.live(ty, movable=True)
        if movable and self.chance(0.35):
            v = r.choice(movable)
            v.live = not linear(ty)
            return v.name
        if depth > 0 and self.chance(0.2):
            got = self.call(env, depth - 1, ty)
            if got:
                return got
        if depth > 0 and linear(ty) and self.chance(0.15):
            cond = self.boolean(env, depth - 1)
            a, b = self.branches(env, depth - 1, ty)
            return f"(if {cond} {a} else {b})"
        if depth > 0 and self.chance(0.1):
            return f"({self.block(env, depth - 1, ty)})"
        if depth > 0 and ty == "T!" and self.chance(0.15):
            vs = env.live("T!")
            if vs:
                v = r.choice(vs)
                if v.lendable and (v.mutable or v.place):
                    return f"swap_in(&{v.name}, {self.expr('T!', env, depth - 1)})"
        if ty == "P":
            return f"P.{{ a: {self.u32(env, depth - 1)}, b: {self.u32(env, depth - 1)} }}"
        if ty == "C":
            return f"C::X.({self.u32(env, depth - 1)})" if self.chance(0.6) else "C::Y"
        if ty == "T!":
            choice = r.randrange(4)
            if choice == 0:
                return f"mk_t({self.u32(env, depth - 1)})"
            if choice == 1:
                return f"id(T!.{{ n: {self.u32(env, depth - 1)} }})"
            if choice == 2 and depth > 0:
                cells = env.live("Cell![T!]", movable=True)
                if cells:
                    c = r.choice(cells)
                    c.live = False
                    return f"{{ let {{ v }} = {c.name}; v }}"
            return f"T!.{{ n: {self.u32(env, depth - 1)} }}"
        if ty == "Bx!":
            return f"Bx!.{{ t: {self.expr('T!', env, depth - 1)}, n: {self.u32(env, depth - 1)} }}"
        if ty == "E!":
            if self.chance(0.6):
                return f"E!::A.({self.expr('T!', env, depth - 1)})"
            return f"E!::B.({self.u32(env, depth - 1)})"
        inner = payload(ty)
        if inner and ty.startswith("Cell!["):
            return f"Cell!.{{ v: {self.expr(inner, env, depth - 1)} }}"
        if inner and ty.startswith("Opt["):
            return f"Opt::Some.({self.expr(inner, env, depth - 1)})" if self.chance(0.7) else f"{ty}::None"
        if inner and ty.startswith("(u32, "):
            return f"({self.u32(env, depth - 1)}, {self.expr(inner, env, depth - 1)})"
        if ty.startswith("Tag!"):
            brand = brand_of(ty)
            assert brand is not None
            witness = self.witness(env, brand)
            assert witness is not None
            cells = env.live(f"Cell![{ty}]", movable=True)
            if cells and depth > 0 and self.chance(0.3):
                c = r.choice(cells)
                c.live = False
                return f"{{ let {{ v }} = {c.name}; v }}"
            if depth > 0 and self.chance(0.2):
                return f"id(Tag!.{{ marker: {witness}, n: {self.u32(env, depth - 1)} }})"
            return f"Tag!.{{ marker: {witness}, n: {self.u32(env, depth - 1)} }}"
        raise ValueError(ty)

    def consume(self, v: Var, env: Env) -> str:
        """A u32 expression that moves `v`."""
        v.live = False
        ty = v.ty
        if ty == "T!":
            return self.r.choice([f"eat_t({v.name})", f"{v.name}.into_n()", f"weigh_it({v.name})",
                                  f"apply({v.name}, |t: T!| eat_t(t))", f"eat_t(id({v.name}))"])
        if ty == "Bx!":
            return f"eat_bx({v.name})"
        if ty == "E!":
            return f"eat_e({v.name})"
        if ty == "Cell![T!]":
            return f"eat_cell({v.name})"
        if ty == "Opt[T!]":
            return f"eat_opt({v.name})"
        if ty == "(u32, T!)":
            return f"eat_pair({v.name})"
        if ty.startswith("Tag!"):
            return self.r.choice([f"eat_tag({v.name})", f"eat_tag(id({v.name}))"])
        if ty.startswith("Opt[Tag!"):
            return f"eat_opt_tag({v.name})"
        if ty.startswith("(u32, Tag!"):
            return f"eat_pair_tag({v.name})"
        if ty.startswith("Cell![Tag!"):
            return f"eat_cell_tag({v.name})"
        if ty.startswith("closure!"):
            return f"{v.name}(1u32)"
        raise ValueError(ty)

    def call(self, env: Env, depth: int, ty: str) -> Optional[str]:
        """A call of a helper returning `ty`, its place arguments lent from live locals."""
        fns = [f for f in self.fns if f.ret == ty and f.brand is None]
        if not fns:
            return None
        f = self.r.choice(fns)
        args = []
        lent: list[Var] = []
        for name, pty, place in f.params:
            if place:
                vs = [v for v in env.live(pty) if v.lendable and v not in lent]
                if not vs:
                    return None
                v = self.r.choice(vs)
                lent.append(v)
                args.append(f"&{v.name}")
            else:
                args.append(self.expr(pty, env, depth))
        # Place arguments must stay full while the others are evaluated: give up if one moved.
        if any(not v.live for v in lent):
            return None
        return f"{f.name}({', '.join(args)})"

    def branches(self, env: Env, depth: int, ty: str) -> tuple[str, str]:
        """Two blocks of type `ty`, each from `env`, balanced: `env` ends as both do."""
        ea, eb = env.copy(), env.copy()
        sa, ta = self.stmts(ea, depth), self.expr(ty, ea, depth)
        sb, tb = self.stmts(eb, depth), self.expr(ty, eb, depth)
        oa, ob = self.own(ea, env), self.own(eb, env)
        pa, pb = self.join(env, [ea, eb])
        return self.wrap(sa, ta, oa + pa), self.wrap(sb, tb, ob + pb)

    def own(self, arm: Env, env: Env) -> list[str]:
        """Consume the arm's own locals, which go out of scope at its end."""
        post: list[str] = []
        if arm.diverged:
            for name in [n for n in arm.vars if n not in env.vars]:
                del arm.vars[name]
            return post
        self.close(arm, set(env.vars), post)
        return post

    def join(self, env: Env, arms: list[Env]) -> list[list[str]]:
        """Balance the arms: a local live in some arm and not in another is consumed where it is live,
        after the arm's value is computed. The statements that do so, for each arm."""
        posts: list[list[str]] = [[] for _ in arms]
        live = [i for i, a in enumerate(arms) if not a.diverged]
        if not live:
            env.diverged = True
            return posts
        for name, v in env.vars.items():
            states = [arms[i].vars[name].live for i in live]
            if all(states) or not any(states):
                v.live = states[0]
                continue
            for i in live:
                if arms[i].vars[name].live:
                    posts[i].append(self.consume(arms[i].vars[name], arms[i]) + ";")
            v.live = False
        return posts

    def wrap(self, stmts: list[str], tail: str, post: Optional[list[str]] = None) -> str:
        """A block: `stmts`, then `tail` as its value, with `post` run after the value is computed."""
        if post:
            z = self.fresh("z")
            return "{ " + " ".join([*stmts, f"let {z} = {tail};", *post]) + f" {z} }}"
        if not stmts:
            return f"{{ {tail} }}"
        return "{ " + " ".join(stmts) + f" {tail} }}"

    def block(self, env: Env, depth: int, ty: str) -> str:
        inner = env.copy()
        before = set(env.vars)
        stmts = self.stmts(inner, depth)
        tail = self.expr(ty, inner, depth)
        post: list[str] = []
        self.close(inner, before, post)
        for name in env.vars:
            env.vars[name].live = inner.vars[name].live
        env.diverged = inner.diverged
        return self.wrap(stmts, tail, post)

    def close(self, env: Env, outer: set[str], stmts: list[str]) -> None:
        """Consume what the block's own locals still hold; they go out of scope."""
        for name, v in list(env.vars.items()):
            if name not in outer:
                if v.live and linear(v.ty):
                    stmts.append(self.consume(v, env) + ";")
                del env.vars[name]

    def match_c(self, env: Env, depth: int, ty: str) -> str:
        cs = env.live("C")
        scrutinee = self.r.choice(cs).name if cs else self.expr("C", env, depth)
        n = self.fresh("x")
        ea, eb = env.copy(), env.copy()
        ea.vars[n] = Var(n, "u32")
        sa, ta = self.stmts(ea, depth), self.expr(ty, ea, depth)
        sb, tb = self.stmts(eb, depth), self.expr(ty, eb, depth)
        del ea.vars[n]
        oa, ob = self.own(ea, env), self.own(eb, env)
        pa, pb = self.join(env, [ea, eb])
        a, b = self.wrap(sa, ta, oa + pa), self.wrap(sb, tb, ob + pb)
        if self.chance(0.3):
            return f"match {scrutinee} {{ C::X({n}) => {a}, _ => {b} }}"
        return f"match {scrutinee} {{ C::X({n}) => {a}, C::Y => {b} }}"

    def match_linear(self, env: Env, depth: int, ty: str) -> str:
        """A match on an `E!` or `Opt[T!]`, moving it; the payload is consumed in its arm."""
        kinds = ["E!", "Opt[T!]"] + [v.ty for v in env.live(movable=True) if v.ty.startswith("Opt[Tag!")]
        kind = self.r.choice(kinds)
        vs = env.live(kind, movable=True)
        if vs:
            v = self.r.choice(vs)
            v.live = False
            scrutinee = v.name
        else:
            scrutinee = self.expr(kind, env, depth)
        n, m = self.fresh("t"), self.fresh("n")
        ea, eb = env.copy(), env.copy()
        ea.vars[n] = Var(n, payload(kind) or "T!")
        if kind == "E!":
            eb.vars[m] = Var(m, "u32")
        sa, ta = self.stmts(ea, depth), self.expr(ty, ea, depth)
        sb, tb = self.stmts(eb, depth), self.expr(ty, eb, depth)
        own = [self.own(ea, env), self.own(eb, env)]
        pa, pb = self.join(env, [ea, eb])
        a, b = self.wrap(sa, ta, own[0] + pa), self.wrap(sb, tb, own[1] + pb)
        if kind == "E!":
            return f"match {scrutinee} {{ E!::A({n}) => {a}, E!::B({m}) => {b} }}"
        return f"match {scrutinee} {{ Opt::Some({n}) => {a}, Opt::None => {b} }}"

    def match_place(self, env: Env, depth: int, ty: str) -> Optional[str]:
        """`match &x { ... }` on a live `E!` or `Opt[T!]` local: the payload binder is a place in `x`. An
        arm may lend it, move it out and put a value back, or move it out and store a whole new value
        of another variant into `x`."""
        vs = [v for v in env.live() if (v.ty == "E!" or v.ty.startswith("Opt[")) and v.lendable
              and self.makeable(v.ty, env)]
        if not vs:
            return None
        x = self.r.choice(vs)
        can_store = (x.mutable or x.place) and not x.frozen
        t, m = self.fresh("t"), self.fresh("n")
        arms: list[tuple[Env, list[str], str, list[str]]] = []
        for k in range(2):
            arm = env.copy()
            arm.vars[x.name].lendable = False
            arm.vars[x.name].frozen = True
            alias = None
            if k == 0:
                alias = Var(t, payload(x.ty) or "T!", place=True, mutable=True)
            elif x.ty == "E!":
                alias = Var(m, "u32", place=True, mutable=True)
            if alias:
                arm.vars[alias.name] = alias
            stmts = self.stmts(arm, depth)
            tail = self.expr(ty, arm, depth) if not arm.diverged else ""
            post: list[str] = []
            if not arm.diverged:
                if alias and not arm.vars[alias.name].live:
                    if can_store and self.chance(0.5):
                        value = f"E!::B.({self.u32(arm, 0)})" if x.ty == "E!" else f"{x.ty}::None"
                        post.append(f"{x.name} = {value};")
                    else:
                        post.append(f"{alias.name} = {self.expr(alias.ty, arm, 0)};")
                elif k == 1 and can_store and x.ty != "E!" and self.chance(0.4):
                    post.append(f"{x.name} = Opt::Some.({self.expr(payload(x.ty) or 'T!', arm, 0)});")
            if alias:
                del arm.vars[alias.name]
            arms.append((arm, stmts, tail, post))
        owns = [self.own(arm, env) for arm, _, _, _ in arms]
        joins = self.join(env, [arm for arm, _, _, _ in arms])
        texts = []
        for (arm, stmts, tail, post), own, joined in zip(arms, owns, joins):
            texts.append("{ " + " ".join(stmts) + " }" if arm.diverged else self.wrap(stmts, tail, post + own + joined))
        if x.ty == "E!":
            return f"match &{x.name} {{ E!::A({t}) => {texts[0]}, E!::B({m}) => {texts[1]} }}"
        return f"match &{x.name} {{ Opt::Some({t}) => {texts[0]}, Opt::None => {texts[1]} }}"

    # ------------------------------------------------------------------- statements --

    def stmts(self, env: Env, depth: int, top: bool = False) -> list[str]:
        """Statements of a block; a function body has more of them than a nested block."""
        out: list[str] = []
        if top:
            count = self.r.randrange(2, 3 * self.size)
        else:
            count = self.r.randrange(0, self.size) if depth > 0 else self.r.randrange(0, 2)
        for _ in range(count):
            if env.diverged:
                break
            out.append(self.stmt(env, depth))
        return out

    def stmt(self, env: Env, depth: int) -> str:
        r = self.r
        kind = r.choice(["let", "let", "var", "var", "consume", "lend", "lend", "assign", "rebuild", "while",
                         "while", "while", "if", "if_return", "if_return", "match", "brand", "brand", "closure",
                         "tuple", "block", "swap", "match_place", "match_place", "match_place", "lend_loop",
                         "place_closure", "mint", "lend_many"])
        if kind == "match_place" and depth > 0:
            text = self.match_place(env, depth - 1, "u32")
            if text:
                name = self.fresh("r")
                env.vars[name] = Var(name, "u32")
                return f"let {name} = {text};"
        if kind == "lend_loop":
            # `bump_t` returns the value before it adds one, so the loop ends.
            vs = [v for v in env.live("T!") if v.lendable]
            if vs:
                v = r.choice(vs)
                inner = env.copy()
                for u in inner.vars.values():
                    if linear(u.ty):
                        u.frozen = True
                inner.vars[v.name].lendable = False
                before = set(inner.vars)
                body = self.stmts(inner, depth - 1)
                if not inner.diverged:
                    self.close(inner, before, body)
                    for name, u in env.vars.items():
                        if u.live and not inner.vars[name].live and (u.mutable or u.place):
                            body.append(f"{name} = {self.expr(u.ty, inner, 0)};")
                cond = r.choice([f"bump_t(&{v.name}) < {r.randrange(1, 30)}u32",
                                 f"&{v.name}.get() < {r.randrange(1, 30)}u32 && bump_t(&{v.name}) < 40u32"])
                return f"while {cond} {{ {' '.join(body)} }}"
        if kind == "place_closure":
            vs = [v for v in env.live("T!") if v.lendable]
            if vs:
                v = r.choice(vs)
                name, call = self.fresh("c"), self.fresh("r")
                env.vars[call] = Var(call, "u32")
                k = self.u32(env, 0)
                body = r.choice(["peek_t(&t) + k", "bump_t(&t) + k", "{ let n = eat_t(t); t = mk_t(n + k); n }"])
                return f"let {name} = |&t: T!, k: u32| {body}; let {call} = {name}(&{v.name}, {k}) + {name}(&{v.name}, 1u32);"
        if kind == "lend_many":
            # Two or three `T!` places in one call, some of them parts of places, maybe the same one:
            # the front end must reject overlapping or entangled lends, or the checker will.
            paths = self.t_places(env)
            if paths:
                count = r.choice([2, 2, 3])
                if self.chance(0.7) and len(paths) >= count:
                    chosen = r.sample(paths, count)   # apart, unless parts of one place
                else:
                    chosen = [r.choice(paths) for _ in range(count)]
                name = self.fresh("r")
                env.vars[name] = Var(name, "u32")
                callee = "two_t" if count == 2 else "three_t"
                return f"let {name} = {callee}({', '.join('&' + p for p in chosen)});"
        if kind == "mint":
            brand, tag = self.fresh("b"), self.fresh("g")
            value = self.u32(env, depth - 1)
            env.vars[tag] = Var(tag, f"Tag!['{brand}]")
            return f"let exists '{brand}. {tag} = mint({value});"
        if kind in ("let", "var"):
            ty = r.choice(COPY_TYPES + self.linear_types(env))
            name = self.fresh("v")
            value = self.expr(ty, env, depth)
            annotate = f": {ty}" if self.chance(0.2) or "None" in value else ""
            env.vars[name] = Var(name, ty, mutable=kind == "var")
            return f"{kind} {name}{annotate} = {value};"
        if kind == "consume":
            vs = [v for v in env.live(movable=True) if linear(v.ty)]
            if vs:
                text = self.consume(r.choice(vs), env)
                if self.chance(0.5):
                    name = self.fresh("r")
                    env.vars[name] = Var(name, "u32")
                    return f"let {name} = {text};"
                return f"{text};"
        if kind == "lend":
            got = self.lend(env)
            if got:
                name = self.fresh("r")
                env.vars[name] = Var(name, "u32")
                return f"let {name} = {got};"
        if kind == "assign":
            vs = [v for v in env.vars.values() if (v.mutable or v.place) and not v.frozen
                  and (not v.live or not linear(v.ty)) and not v.ty.startswith(("closure", "Brand"))
                  and self.makeable(v.ty, env)]
            if vs:
                v = r.choice(vs)
                value = self.expr(v.ty, env, depth)
                v.live = True
                return f"{v.name} = {value};"
        if kind == "rebuild":
            vs = [v for v in env.live("Bx!", movable=True) if v.mutable or v.place]
            if vs:
                v = r.choice(vs)
                t, n = self.fresh("t"), self.fresh("n")
                inner = env.copy()
                inner.vars[v.name].live = False
                inner.vars[v.name].lendable = False
                inner.vars[t] = Var(t, "T!", mutable=False)
                inner.vars[n] = Var(n, "u32")
                body = [s for s in self.stmts(inner, depth - 1) if not inner.diverged]
                if inner.diverged:
                    return self.stmt(env, depth)
                parts = [f"let {{ t: {t}, n: {n} }} = {v.name};", *body]
                if not inner.vars[t].live:
                    parts.append(f"{v.name} = Bx!.{{ t: mk_t({n}), n: {n} }};")
                else:
                    parts.append(f"{v.name} = Bx!.{{ t: {t}, n: {n} + 1u32 }};")
                    inner.vars[t].live = False
                for name in env.vars:
                    if name != v.name:
                        env.vars[name].live = inner.vars[name].live
                return " ".join(parts)
        if kind == "while" and depth > 0:
            return self.loop(env, depth)
        if kind == "if" and depth > 0:
            cond = self.boolean(env, depth - 1)
            a, b = self.branches(env, depth - 1, "u32")
            name = self.fresh("r")
            env.vars[name] = Var(name, "u32")
            return f"let {name} = if {cond} {a} else {b};"
        if kind == "if_return":
            cond = self.boolean(env, depth - 1)
            inner = env.copy()
            body = self.stmts(inner, 0) if depth > 0 else []
            if inner.diverged:
                return f"if {cond} {{ {' '.join(body)} }}"
            return f"if {cond} {{ {' '.join(body + self.exit(inner, depth))} }}"
        if kind == "match" and depth > 0:
            name = self.fresh("r")
            text = self.match_linear(env, depth - 1, "u32") if self.chance(0.6) else self.match_c(env, depth - 1, "u32")
            env.vars[name] = Var(name, "u32")
            return f"let {name} = {text};"
        if kind == "brand":
            brand, w = self.fresh("b"), self.fresh("w")
            tag = self.fresh("g")
            env.brands.append(brand)
            env.vars[w] = Var(w, f"Brand['{brand}]")
            ty = f"Tag!['{brand}]"
            value = f"Tag!.{{ marker: {w}, n: {self.u32(env, depth - 1)} }}"
            env.vars[tag] = Var(tag, ty, mutable=self.chance(0.3))
            extra = ""
            if self.chance(0.5):
                k = self.fresh("k")
                extra = f" let {k} = tick(Tick.{{ marker: {w} }}, {self.u32(env, 0)});"
                env.vars[k] = Var(k, "u32")
            kw = "var" if env.vars[tag].mutable else "let"
            return f"let exists '{brand}. {w} = fresh(); {kw} {tag} = {value};{extra}"
        if kind == "closure":
            vs = env.live("T!", movable=True)
            name = self.fresh("c")
            if vs and self.chance(0.7):
                v = r.choice(vs)
                v.live = False
                env.vars[name] = Var(name, "closure!", lendable=False)
                return f"let {name} = |y: u32| y + eat_t({v.name});"
            env.vars[name] = Var(name, "closure", lendable=False)
            k = self.u32(env, 0)
            call = self.fresh("r")
            env.vars[call] = Var(call, "u32")
            return f"let {name} = |y: u32| y + 1u32; let {call} = {name}({k}) + {name}(2u32);"
        if kind == "tuple":
            a, b = self.fresh("a"), self.fresh("t")
            pair = r.choice([t for t in self.linear_types(env) if t.startswith("(u32, ")])
            value = self.expr(pair, env, depth)
            env.vars[a] = Var(a, "u32")
            env.vars[b] = Var(b, payload(pair) or "T!")
            return f"let ({a}, {b}) = {value};"
        if kind == "block" and depth > 0:
            name = self.fresh("r")
            text = self.block(env, depth - 1, "u32")
            env.vars[name] = Var(name, "u32")
            return f"let {name} = {text};"
        if kind == "swap":
            vs = [v for v in env.live() if (v.ty == "T!" or v.ty.startswith("Tag!")) and v.lendable
                  and (v.mutable or v.place) and self.makeable(v.ty, env)]
            if vs:
                v = r.choice(vs)
                old = self.fresh("o")
                value = self.expr(v.ty, env, depth - 1)
                if not v.live:
                    return self.stmt(env, depth)
                env.vars[old] = Var(old, v.ty)
                return f"let {old} = swap_in(&{v.name}, {value});"
        name = self.fresh("r")
        value = self.u32(env, depth)
        env.vars[name] = Var(name, "u32", mutable=True)
        return f"var {name} = {value};"

    def loop(self, env: Env, depth: int) -> str:
        """`while i < N { ... }` whose body leaves the outer linear state as it found it: an outer `var`
        it cycles is consumed and assigned again; other outer linear values are only lent."""
        i = self.fresh("i")
        bound = self.r.randrange(0, 4)
        body_env = env.copy()
        cycled = [v for v in body_env.live(movable=True) if linear(v.ty) and v.mutable
                  and not v.ty.startswith("closure") and self.makeable(v.ty, body_env)]
        cycled = [v for v in cycled if self.chance(0.5)]
        for v in body_env.vars.values():
            if linear(v.ty) and v not in cycled:
                v.frozen = True
        before = set(body_env.vars)
        body_env.vars[i] = Var(i, "u32", mutable=True, frozen=True)
        parts: list[str] = []
        for v in cycled:
            parts.append(self.consume(v, body_env) + ";")
        parts += self.stmts(body_env, depth - 1)
        if not body_env.diverged:
            for v in cycled:
                if not body_env.vars[v.name].live:
                    parts.append(f"{v.name} = {self.expr(v.ty, body_env, 0)};")
                    body_env.vars[v.name].live = True
            # An outer var assigned in the body must be full at its end, as at its start.
            for name, v in env.vars.items():
                inner = body_env.vars[name]
                if v.live and not inner.live:
                    if (v.mutable or v.place) and self.makeable(v.ty, body_env):
                        parts.append(f"{name} = {self.expr(v.ty, body_env, 0)};")
                        inner.live = True
                elif not v.live and inner.live and linear(v.ty):
                    parts.append(self.consume(inner, body_env) + ";")
            self.close(body_env, before | {i}, parts)
            parts.append(f"{i} = {i} + 1u32;")
        env.vars[i] = Var(i, "u32", mutable=True)
        return f"var {i} = 0u32; while {i} < {bound}u32 {{ {' '.join(parts)} }}"

    def exit(self, env: Env, depth: int) -> list[str]:
        """Consume everything live, fill every place parameter, and return."""
        out = []
        for v in env.vars.values():
            if v.place and not v.live:
                out.append(f"{v.name} = {self.expr(v.ty, env, 0)};")
                v.live = True
        for v in list(env.vars.values()):
            if v.live and linear(v.ty) and not v.place:
                out.append(self.consume(v, env) + ";")
        out.append(f"return {self.ret_value(env)};")
        env.diverged = True
        return out

    def ret_value(self, env: Env) -> str:
        if self.ret == "i32":
            return f"{self.u32(env, 0)} as i32"
        return self.expr(self.ret, env, 0)

    # -------------------------------------------------------------------- functions --

    def function(self, name: str, depth: int) -> str:
        r = self.r
        params: list[tuple[str, str, bool]] = []
        brand = self.fresh("q") if self.chance(0.2) else None
        for _ in range(r.randrange(0, 4)):
            place = self.chance(0.4)
            ty = r.choice(COPY_TYPES + LINEAR_TYPES)
            params.append((self.fresh("p"), ty, place))
        if brand:
            params.append((self.fresh("p"), f"Tag!['{brand}]", self.chance(0.5)))
        ret = r.choice(["u32", "u32", "T!", "Bx!", "Opt[T!]", "P"])
        f = Fn(name, params, ret, brand)
        env = Env()
        for pname, ty, place in params:
            env.vars[pname] = Var(pname, ty, place=place, mutable=place, frozen=place and ty.startswith("Tag!"))
        self.ret = ret
        body = self.stmts(env, depth, top=True)
        text = self.finish(env, body, ret, depth)
        self.fns.append(f)
        header = f"forall '{brand}. " if brand else ""
        ps = ", ".join(f"{'&' if place else ''}{n}: {t}" for n, t, place in params)
        return f"{header}fn {name}({ps}) -> {ret} {{\n    {text}\n}}\n"

    def finish(self, env: Env, body: list[str], ret: str, depth: int) -> str:
        if env.diverged:
            return "\n    ".join(body)
        tail = self.expr(ret, env, depth) if ret != "i32" else f"({self.u32(env, depth)} % 100u32) as i32"
        post: list[str] = []
        for v in list(env.vars.values()):
            if v.live and linear(v.ty) and not v.place:
                post.append(self.consume(v, env) + ";")
        for v in env.vars.values():
            if v.place and not v.live:
                post.append(f"{v.name} = {self.expr(v.ty, env, 0)};")
        if post:
            z = self.fresh("z")
            return "\n    ".join([*body, f"let {z} = {tail};", *post, z])
        return "\n    ".join(body + [tail])

    def program(self) -> str:
        parts = [PRELUDE]
        for _ in range(self.r.randrange(1, 4)):
            parts.append(self.function(self.fresh("h"), self.r.randrange(1, self.size + 1)))
        self.ret = "i32"
        env = Env()
        body = self.stmts(env, self.size, top=True)
        parts.append(f"fn main() -> i32 {{\n    {self.finish(env, body, 'i32', self.size)}\n}}\n")
        return "\n".join(parts)


def generate(seed: int, size: int = 3) -> dict[str, str]:
    """The package of the program for `seed`: its manifest and its one module."""
    from tools.fuzz.oracle import manifest
    return {"kur.toml": manifest("main"), "lib.kur": Gen(seed, size).program()}
