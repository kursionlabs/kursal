"""Packages and names: load the package graph, assign each source file its
module, and resolve every type and value path to what it names.

Each resolved node gets a `ref` attribute:
  ("decl", Decl)              a declaration (struct, enum, tuple, fn, extern, trait, const, method)
  ("variant", Decl, name)     an enum variant
  ("trait_method", Decl, name)  a trait's method, `Trait::method`
  ("assoc", param, name)      a member of a type parameter, found by inference
  ("param", name)             a type parameter in scope
  ("local", name)             a local value
  ("prim", name)              a primitive type
  ("builtin", name)           a name the compiler gives meaning: `Copy`, `Exposed`, `Never`, and what
                              `builtin::name` names in package core (`Brand`, `fresh`, ...)
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Iterator, Sequence, Set
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal, Never, cast, Optional
from pathlib import Path
from . import syntax as S
from .sexpr import KursError, Position, located
from .core import ModuleId

PRIMS = {"i8", "i16", "i32", "i64", "isize", "u8", "u16", "u32", "u64", "usize", "f32", "f64", "bool"}
# Always in scope, like the primitives: the type `Never` and the traits.
NEVER = "Never"
BUILTIN_TRAITS = {"Copy", "Exposed"}
# What `builtin::name` names. Only package `core` writes such a path, and it exports each builtin from
# the module it belongs to (`export builtin::NonNull;` in core::ptr); everyone else imports it from there.
BUILTIN_TYPES = {"Brand", "NonNull"}
BUILTIN_VALUES = {"fresh", "static_brand", "sizeof", "alloca", "enum_variant", "fence", "load_volatile", "store_volatile",
                  "trap"}
# The builtin types an impl may give methods, as it may a primitive: `impl NonNull[T]` in core::ptr.
OWNED_BUILTINS = {"NonNull"}
CORE = "core"
# Resolution, and then signatures, report at most this many errors, in source order, and how many more they found.
ERROR_LIMIT = 20
LIB = Path(os.environ.get("KURSAL_LIB") or Path(__file__).resolve().parents[2] / "lib")


class ResolveError(KursError):
    pass


@dataclass(eq=False)
class Decl:
    kind: str              # struct enum tuple fn extern trait const method
    module: ModuleId
    name: str              # a method's name is `Target::method`
    node: NamedItem
    impl: Optional[S.Impl] = None    # a method's or associated const's Impl

    def __repr__(self) -> str:
        return f"Decl({self.kind} {self.module.package}:{'::'.join(self.module.path)}::{self.name})"


@dataclass
class Package:
    id: int
    name: str
    root: Path             # its directory
    source_root: Path
    deps: dict[str, int]             # alias -> package id
    entry: Optional[S.NamePath] = None    # an executable's entry path
    files: dict[ModuleId, Path] = field(default_factory=dict)  # ModuleId -> source path
    entry_decl: Optional[Decl] = None
    manifest: Optional[Path] = None    # its kur.toml, or its one .kur file
    link_flags: list[str] = field(default_factory=list)  # C compiler arguments after the source files
    c_sources: list[Path] = field(default_factory=list)  # package-relative C sources linked into executables


@dataclass
class Scope:
    """One module's namespace."""
    module: ModuleId
    decls: dict[str, Decl] = field(default_factory=dict)     # name -> Decl
    imports: dict[str, S.NamePath] = field(default_factory=dict)   # alias -> path
    import_pos: dict[str, Position] = field(default_factory=dict)  # alias -> where its `use` names it
    globs: list[S.NamePath] = field(default_factory=list)     # paths
    exports: set[str] = field(default_factory=set)
    items: list[S.Item] = field(default_factory=list)


type NamedItem = S.Struct | S.EnumDecl | S.TupleDecl | S.Fn | S.Extern | S.TraitDecl | S.Const
type Target = Decl | ModuleId | S.VariantRef | S.TraitMethodRef | S.BuiltinRef
type LookupKey = (
    tuple[Literal["path"], ModuleId, S.NamePath]
    | tuple[Literal["import"], ModuleId, str]
    | tuple[Literal["export"], ModuleId, str]
)


def fail(message: str, pos: Optional[Position] = None) -> Never:
    raise ResolveError(message, pos)


# ------------------------------------------------------------ the graph --

def _table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(f"{where}: expected a table")
    entries = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for key, item in entries.items():
        if not isinstance(key, str):
            fail(f"{where}: expected a string key")
        result[key] = item
    return result


def _string(table: dict[str, object], manifest: Path, key: str, default: Optional[str] = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        fail(f"{manifest}: manifest {key!r} must be a string")
    return value


def _strings(table: dict[str, object], manifest: Path, key: str) -> list[str]:
    values = table.get(key, [])
    if not isinstance(values, list) or any(not isinstance(value, str) or not value or "\x00" in value for value in values):
        fail(f"{manifest}: manifest {key!r} must be an array of nonempty strings without NUL")
    return cast(list[str], values)


def _manifest(path: Path) -> dict[str, object]:
    text = path.read_text()
    try:
        # tomllib returns dynamic values. Validate them as objects at this boundary.
        document = cast(object, tomllib.loads(text))
    except tomllib.TOMLDecodeError as error:
        # Its message ends `(at line N, column M)`, or `(at end of document)`: the last line.
        where = str(error).rsplit("(at line ", 1)
        line = int(where[1].split(",")[0]) if len(where) == 2 else text.replace("\r\n", "\n").count("\n") + 1
        fail(f"{path}:{line}: invalid manifest")
    return _table(document, str(path))


def _exists(path: Path) -> bool:
    """Whether `path` names anything. An error other than its absence (ENOENT, ENOTDIR) stops resolution:
    the file may be there."""
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as error:
        fail(f"cannot read {path} (error {error.errno})")
    return True


def load(path: str | Path) -> list[Package]:
    """The package graph rooted at a package directory, its kur.toml, or one .kur file. A single file
    depends on the standard library `std` when one is installed (KURSAL_LIB)."""
    path = Path(path).resolve()
    packages: list[Package] = []
    by_dir: dict[Path, int] = {}

    def add(manifest: Path, name: Optional[str] = None, source_root: Optional[Path] = None,
            deps: Optional[dict[str, Path]] = None, entry: Optional[S.NamePath] = None) -> int:
        root = manifest.parent
        if root in by_dir and manifest.suffix == ".toml":
            return by_dir[root]
        package = Package(len(packages), name or root.name, root, source_root or root, {}, entry, manifest=manifest)
        packages.append(package)
        if manifest.suffix == ".toml":
            by_dir[root] = package.id
            config = _manifest(manifest)
            info = _table(config.get("package", {}), f"{manifest}: package")
            unknown = set(config) - {"package", "dependencies"} | set(info) - {"name", "kind", "source_root", "entry", "link_flags", "c_sources"}
            if unknown:
                fail(f"{manifest}: unknown manifest keys {sorted(unknown)}")
            package.name = _string(info, manifest, "name")
            package.source_root = (root / _string(info, manifest, "source_root", "src")).resolve()
            package.link_flags = _strings(info, manifest, "link_flags")
            package.c_sources = [(root / source).resolve() for source in _strings(info, manifest, "c_sources")]
            kind = _string(info, manifest, "kind", "library")
            if kind not in ("library", "executable"):
                fail(f"{manifest}: manifest 'kind' must be \"library\" or \"executable\"")
            if kind == "executable":
                package.entry = tuple(p for p in _string(info, manifest, "entry").split("::") if p != "package")
            deps = {alias: (root / _string(_table(spec, f"{manifest}: {alias}"), manifest, "path")).resolve()
                    for alias, spec in _table(config.get("dependencies", {}), f"{manifest}: dependencies").items()}
        for alias, target in (deps or {}).items():
            manifest_path = target if target.suffix == ".toml" else target / "kur.toml"
            package.deps[alias] = add(manifest_path)
        return package.id

    if path.suffix == ".kur":
        items = S.parse(path.read_text(), str(path))
        main = any(isinstance(i, S.Fn) and i.name == "main" for i in items)
        std = {"std": LIB / "std"} if _exists(LIB / "std" / "kur.toml") else {}
        add(path, path.stem, path.parent, std, ("main",) if main else None)
        packages[0].files[ModuleId(0)] = path
    else:
        add(path if path.suffix == ".toml" else path / "kur.toml")
    for package in packages:
        if not package.files:
            package.files = discover(package)
            if ModuleId(package.id) not in package.files:
                fail(f"package {package.name} has no lib.kur in {package.source_root}")
    check_acyclic(packages)
    return packages


def discover(package: Package) -> dict[ModuleId, Path]:
    """Each .kur file under the source root, by the module its path assigns."""
    files = {}
    for directory, subdirs, names in os.walk(package.source_root):
        here = Path(directory)
        if here != package.source_root and _exists(here / "kur.toml"):
            subdirs.clear()
            continue
        subdirs.sort()
        for name in sorted(names):
            if not name.endswith(".kur"):
                continue
            rel = (here / name).relative_to(package.source_root).with_suffix("").parts
            path = rel[:-1] if rel[-1] == "lib" else rel
            module = ModuleId(package.id, tuple(path))
            if module in files:
                fail(f"two files define module {'::'.join(path)} of {package.name}")
            files[module] = here / name
    return files


def check_acyclic(packages: Sequence[Package]) -> None:
    state: dict[int, int] = {}

    def visit(package_id: int) -> None:
        if state.get(package_id) == 1:
            fail("dependency cycle through package " + packages[package_id].name)
        if state.get(package_id) is None:
            state[package_id] = 1
            for d in packages[package_id].deps.values():
                visit(d)
            state[package_id] = 2
    for p in range(len(packages)):
        visit(p)


# ------------------------------------------------------------ namespaces --

class Resolver:
    def __init__(self, packages: list[Package]) -> None:
        self.packages = packages
        self.scopes: dict[ModuleId, Scope] = {}
        self.children: dict[ModuleId, dict[str, ModuleId]] = {}
        self.instances: list[tuple[ModuleId, S.InstanceDecl]] = []          # (ModuleId, InstanceDecl)
        self.attached: dict[tuple[Optional[Decl | str], str], list[Decl]] = {}           # (Decl or prim name, member) -> [Decl]
        self.cache: dict[LookupKey, Target] = {}
        self.active: set[LookupKey] = set()
        self.globbing: set[tuple[ModuleId, S.NamePath]] = set()
        # The errors reported so far, each with where it sorts in source order: 0 for a manifest's and 1 more
        # than its module's place among the modules for a source file's, then its position, then the order it
        # was found in.
        self.errors: dict[str, tuple[int, int, int, int]] = {}
        self.order: dict[ModuleId, int] = {}
        for package in packages:
            self.children.setdefault(ModuleId(package.id), {})
            for module, file in package.files.items():
                self.order[module] = len(self.order)
                self.collect(module, S.parse(file.read_text(), str(file)))
                for k in range(len(module.path)):
                    parent = ModuleId(module.package, module.path[:k])
                    self.children.setdefault(parent, {})[module.path[k]] = parent.child(module.path[k])
                    self.children.setdefault(parent.child(module.path[k]), {})
        for scope in self.scopes.values():
            self.attach(scope)

    def collect(self, module: ModuleId, items: list[S.Item]) -> None:
        scope = self.scopes[module] = Scope(module, items=items)
        kinds = {S.Struct: "struct", S.EnumDecl: "enum", S.TupleDecl: "tuple", S.Fn: "fn",
                 S.Extern: "extern", S.TraitDecl: "trait", S.Const: "const"}
        for item in items:
            match item:
                case S.Module():
                    self.report(ResolveError("package sources do not declare 'module'", item.pos), module)
                case S.Use():
                    self.use(scope, (), item.tree, item.export)
                case S.InstanceDecl():
                    self.instances.append((module, item))
                case S.Struct() | S.EnumDecl() | S.TupleDecl() | S.Fn() | S.Extern() | S.TraitDecl() | S.Const():
                    with self.reporting(module, item.pos):
                        self.declare(scope, Decl(kinds[type(item)], module, item.name, item))

    def declare(self, scope: Scope, decl: Decl) -> None:
        if decl.name in scope.decls or decl.name in scope.imports:
            fail(f"duplicate name {decl.name}", decl.node.pos)
        scope.decls[decl.name] = decl

    def use(self, scope: Scope, prefix: S.NamePath, tree: S.UseTree, export: bool) -> None:
        """The names a `use` tree imports; its errors are at where the tree starts."""
        names, star, children, alias, start = tree
        path = prefix + names
        with self.reporting(scope.module, start):
            if path[0] == "builtin":
                self.builtin_package(scope.module)
            if star:
                scope.globs.append(path)
            elif children:
                for child in children:
                    self.use(scope, path, child, export)
            else:
                name = alias or path[-1]
                if alias and alias.endswith("!") != path[-1].endswith("!"):
                    # Still imported under the alias, so that its uses report nothing more.
                    self.report(ResolveError(f"the alias {alias} of {path[-1]} must "
                                             f"{'' if path[-1].endswith('!') else 'not '}end with !", start), scope.module)
                if export and path == (name,):  # `export name;` publishes an existing name
                    scope.exports.add(name)
                    return
                if name in ("package", "builtin") or name in BUILTIN_TRAITS:
                    fail(f"'{name}' is compiler-sealed")
                if name in scope.imports or name in scope.decls:
                    fail(f"duplicate import {name} in {scope.module}")
                scope.imports[name] = path
                scope.import_pos[name] = start
                if export:
                    scope.exports.add(name)

    def attach(self, scope: Scope) -> None:
        """Methods and associated consts are declared by their impl's module
        and attached to the impl's target type for `Target::name` lookup."""
        for item in scope.items:
            if not isinstance(item, S.Impl):
                continue
            target = item.type
            if not isinstance(target, S.TName):
                self.report(ResolveError("an impl target must be a named type", item.pos), scope.module)
                continue
            owner: Optional[Decl | str]
            params = item.forall.params if item.forall else ()
            if len(target.path) == 1 and target.path[0] in PRIMS:
                owner = target.path[0]
            elif len(target.path) == 1 and target.path[0] in params:
                owner = None  # a blanket impl: its methods are found by method calls only
            else:
                owner = self.impl_owner(scope.module, target)
            for member in item.items:
                kind = "method" if isinstance(member, S.Fn) else "const"
                decl = Decl(kind, scope.module, f"{target.path[-1]}::{member.name}", member, item)
                with self.reporting(scope.module, member.pos):
                    self.declare(scope, decl)
                    self.attached.setdefault((owner, member.name), []).append(decl)

    def impl_owner(self, module: ModuleId, target: S.TName) -> Optional[Decl | str]:
        """What `impl target` gives methods. When `target` names nothing but its other spelling names a type,
        the error is reported and the methods go to that type, so that their uses report nothing more."""
        try:
            found = self.lookup(module, target.path, target.pos)
        except ResolveError as error:
            self.report(error, module)
            try:
                found = self.lookup(module, (*target.path[:-1], respelling(target.path[-1])))
            except ResolveError:
                return None
            return found if isinstance(found, Decl) else None
        if isinstance(found, tuple) and found[0] == "builtin" and found[1] in OWNED_BUILTINS:
            return found[1]
        if isinstance(found, Decl):
            return found
        self.report(ResolveError("an impl target must name a type declaration", target.pos), module)
        return None

    # ---- path lookup
    def file(self, module: ModuleId) -> Path:
        return self.packages[module.package].files[module]

    @contextmanager
    def located(self, module: ModuleId, pos: Optional[Position]) -> Iterator[None]:
        """Errors raised inside name `module`'s file and, when they have no position, `pos`."""
        try:
            yield
        except KursError as error:
            raise located(error, self.file(module), pos) from None

    @contextmanager
    def reporting(self, module: ModuleId, pos: Optional[Position]) -> Iterator[None]:
        """A resolution error raised inside is reported, in `module`'s file and, when it has no position, at
        `pos`, and the rest of the block is skipped."""
        try:
            yield
        except ResolveError as error:
            self.report(error, module, pos)

    def report(self, error: KursError, module: ModuleId, pos: Optional[Position] = None,
               file: Optional[Path] = None) -> None:
        """Keeps `error` for `stop`, in `module`'s file (or `file`) and at `pos` when it has no position.
        An error reported twice is kept once."""
        found = located(error, file or self.file(module), pos)
        line, col = found.pos or (0, 0)
        self.errors.setdefault(str(found), (0 if file else self.order[module] + 1, line, col, len(self.errors)))

    def stop(self, kind: type[KursError]) -> None:
        """Raises the errors reported, in source order, as a `kind`: the first ERROR_LIMIT, and how many more
        there are."""
        if not self.errors:
            return
        found = sorted(self.errors, key=self.errors.__getitem__)
        more = len(found) - ERROR_LIMIT
        shown = found[:ERROR_LIMIT] + ([f"and {more} more error{'s' if more > 1 else ''}"] if more > 0 else [])
        raise kind("\n".join(shown))

    def lookup(self, module: ModuleId, path: S.NamePath, pos: Optional[Position] = None) -> Target:
        """What `path` names from `module`: a Decl or a ModuleId."""
        try:
            return self.solve(("path", module, tuple(path)))
        except ResolveError as e:
            if e.pos is None and pos is not None:
                raise ResolveError(str(e), pos) from None
            raise

    def solve(self, key: LookupKey) -> Target:
        if key in self.cache:
            return self.cache[key]
        if key in self.active:
            fail(f"import cycle at {key[2]}")
        self.active.add(key)
        try:
            if key[0] == "import":
                value = self.resolve_path(key[1], self.scopes[key[1]].imports[key[2]], importing=key[2])
            elif key[0] == "export":
                value = self.resolve_path(key[1], (key[2],))
            else:
                value = self.resolve_path(key[1], key[2])
        finally:
            self.active.discard(key)
        self.cache[key] = value
        return value

    def resolve_path(self, module: ModuleId, path: S.NamePath, importing: Optional[str] = None,
                     hint: bool = True) -> Target:
        """What `path` names from `module`; with `hint`, a first name that names nothing is reported with
        what its other spelling names (`respelled`)."""
        first, rest = path[0], path[1:]
        scope = self.scopes[module]
        target: Optional[Target]
        if first == "builtin":
            return self.builtin(module, path)
        if first == "package":
            target = ModuleId(module.package)
        elif first in scope.decls:
            target = scope.decls[first]
        elif first in scope.imports and first != importing:
            try:
                target = self.solve(("import", module, first))
            except ResolveError as error:
                misspelled = self.misspelled_import(module, first) if hint else ""
                if not misspelled:
                    raise
                raise ResolveError(error.message + misspelled, error.pos) from None
        else:
            target = self.root_member(module, first) or self.glob(module, first)
            if target is None:
                fail(f"unknown name {'::'.join(path)}{self.unknown_hint(module, first, importing) if hint else ''}")
        for k, name in enumerate(rest):
            if isinstance(target, Decl):
                return self.associated(target, "::".join(rest[k:]))
            if isinstance(target, tuple) and target[0] == "builtin" and target[1] in OWNED_BUILTINS:
                return self.attached_to(target[1], "::".join(rest[k:]))
            if not isinstance(target, ModuleId):
                fail(f"{'::'.join(path[:k + 1])} is not a module")
            target = self.member(module, target, name)
        return target

    def builtin(self, module: ModuleId, path: S.NamePath) -> S.BuiltinRef:
        """`builtin::name`, which only package core writes."""
        self.builtin_package(module)
        if len(path) != 2 or path[1] not in BUILTIN_TYPES | BUILTIN_VALUES:
            fail(f"unknown builtin {'::'.join(path)}")
        return ("builtin", path[1])

    def builtin_package(self, module: ModuleId) -> None:
        if self.packages[module.package].name != CORE:
            fail("only package core names builtins; import them from core")

    def attached_to(self, owner: str, name: str) -> Decl:
        """`Owner::name` of a primitive or builtin type: a method or const an impl of it declares."""
        found = self.attached.get((owner, name))
        if not found:
            fail(f"unknown {owner}::{name}")
        return found[0]

    def root_member(self, module: ModuleId, name: str) -> Optional[Decl | ModuleId]:
        package = self.packages[module.package]
        root = ModuleId(module.package)
        if name in package.deps:
            return ModuleId(package.deps[name])
        if name in self.children[root]:
            return self.children[root][name]
        return self.scopes[root].decls.get(name) if root in self.scopes else None

    def glob(self, module: ModuleId, name: str) -> Optional[Target]:
        for path in self.scopes[module].globs:
            if (module, path) in self.globbing:
                continue                # the glob's own path is being resolved: it cannot name itself
            self.globbing.add((module, path))
            try:
                source = self.resolve_path(module, path)
            finally:
                self.globbing.discard((module, path))
            if not isinstance(source, ModuleId):
                fail(f"{'::'.join(path)} is not a module")
            try:
                return self.member(module, source, name, hint=False)
            except ResolveError:
                continue
        return None

    def member(self, caller: ModuleId, module: ModuleId, name: str, hint: bool = True) -> Target:
        """`module::name`, seen from `caller`: its own declarations; within one
        package also its children and imports; across packages only exports. With `hint`, as
        `resolve_path`."""
        scope = self.scopes.get(module)
        if scope and name in scope.decls:
            return scope.decls[name]
        if caller.package == module.package:
            if name in self.children.get(module, {}):
                return self.children[module][name]
            if scope and name in scope.imports:
                return self.solve(("import", module, name))
            if scope:
                found = self.glob(module, name)
                if found is not None:
                    return found
        elif scope and name in scope.exports:
            return self.solve(("export", module, name))
        respelled = self.respelled(name, lambda other: self.member(caller, module, other, hint=False)) if hint else ""
        fail(f"unknown or private member {name} of {'::'.join(module.path) or self.packages[module.package].name}"
             f"{respelled}")

    @staticmethod
    def respelled(name: str, find: Callable[[str], Target]) -> str:
        """For a name that names nothing: what its other spelling, with or without the `!` of a linear
        type, names, when it names something."""
        other = respelling(name)
        try:
            find(other)
        except ResolveError:
            return ""
        return spelled_as(other)

    def unknown_hint(self, module: ModuleId, name: str, importing: Optional[str]) -> str:
        """For a first name that names nothing in `module`: what its other spelling names there, or, when
        that is an import that names nothing, what the import names respelled (`misspelled_import`)."""
        other = respelling(name)
        if other in self.scopes[module].imports and other != importing:
            misspelled = self.misspelled_import(module, other)
            if misspelled:
                return spelled_as(respelling(self.scopes[module].imports[other][-1])) + misspelled
        return self.respelled(name, lambda other: self.resolve_path(module, (other,), hint=False))

    def misspelled_import(self, module: ModuleId, alias: str) -> str:
        """For an import that names nothing: where its `use` names it, when the import's last name respelled,
        with or without the `!` of a linear type, names something."""
        path = self.scopes[module].imports[alias]
        try:
            self.resolve_path(module, (*path[:-1], respelling(path[-1])), importing=alias, hint=False)
        except ResolveError:
            return ""
        line, col = self.scopes[module].import_pos[alias]
        return f", imported as {path[-1]} at {self.file(module)}:{line}:{col}"

    def associated(self, owner: Decl, name: str) -> Decl | S.VariantRef | S.TraitMethodRef:
        """`Owner::name`: an enum variant, or a method or const attached to `Owner`."""
        if isinstance(owner.node, S.EnumDecl) and name in (v for v, _ in owner.node.variants):
            return ("variant", owner, name)
        if isinstance(owner.node, S.TraitDecl) and name in (m.name for m in owner.node.methods):
            return ("trait_method", owner, name)
        found = self.attached.get((owner, name), [])
        own = [d for d in found if d.module == owner.module] or found
        if len(own) != 1:
            fail(f"{'unknown' if not own else 'ambiguous'} associated name {owner.name}::{name}")
        return own[0]

    def library(self, module: ModuleId, home: str, path: S.NamePath, std_path: S.NamePath) -> Target:
        """A library item the compiler itself refers to (Slice for string
        literals, Buffer and Format for f-strings, Ordering for atomics):
        through a direct dependency on its home library, else through std,
        else in the module's own package."""
        deps = self.packages[module.package].deps
        root = home if home in deps else "std" if "std" in deps else "package"
        return self.lookup(module, (root, *(std_path if root == "std" else path)))


def respelling(name: str) -> str:
    """`name` with the `!` of a linear type added or removed."""
    return name.removesuffix("!") if name.endswith("!") else name + "!"


def spelled_as(name: str) -> str:
    """The end of the message for a name whose other spelling, `name`, names something."""
    return f"; the linear type is {name}" if name.endswith("!") else f"; {name} is not a linear type"


# ---------------------------------------------------------- references --

class Names:
    """Resolve every path in every item of every module."""

    def __init__(self, resolver: Resolver) -> None:
        self.r = resolver

    def run(self) -> Resolver:
        for module, scope in self.r.scopes.items():
            self.module = module
            for item in scope.items:
                self.item_pos = item.pos
                with self.r.located(module, item.pos):
                    self.item(item, frozenset())
        self.r.stop(ResolveError)
        return self.r

    @contextmanager
    def reporting(self) -> Iterator[None]:
        """A resolution error raised inside is reported, at the item's position when it has none of its own,
        and the walk goes on after the block."""
        with self.r.reporting(self.module, self.item_pos):
            yield

    def params(self, forall: Optional[S.Forall]) -> frozenset[str]:
        return frozenset(p for p in (forall.params + forall.determined if forall else ()) if not p.startswith("'"))

    def premises(self, forall: Optional[S.Forall], types: Set[str]) -> None:
        for trait, args in (forall.premises if forall else ()):
            with self.reporting():
                trait_ref: S.DeclRef | tuple[Literal["builtin"], str] = (
                    ("builtin", trait[0]) if trait in (("Copy",), ("Exposed",)) else self.decl(trait, "trait", args.pos))
                args.ref = trait_ref
            for a in args.args:
                self.type(a, types)

    def item(self, item: S.Item, types: Set[str]) -> None:
        if isinstance(item, (S.Use, S.Module)):
            return
        forall = item.forall
        types = types | self.params(forall)
        self.premises(forall, types)
        match item:
            case S.Struct(fields=fields):
                for _, t in fields:
                    self.type(t, types)
            case S.TupleDecl(elems=elems):
                for t in elems:
                    self.type(t, types)
            case S.EnumDecl(variants=variants):
                for _, payload in variants:
                    for t in payload:
                        self.type(t, types)
            case S.Fn():
                self.fn(item, types)
            case S.Extern(params=params, ret=ret):
                for p in params:
                    assert p.type is not None
                    self.type(p.type, types)
                if ret is not None:
                    self.type(ret, types)
            case S.Const(type=ty, value=value):
                self.type(ty, types)
                self.expr(value, types, frozenset())
            case S.Impl(type=ty, items=members):
                self.type(ty, types)
                for member in members:
                    self.item(member, types)
            case S.TraitDecl(methods=methods, forall=forall):
                trait_types = types | ({"Self"} if not forall else frozenset())
                for m in methods:
                    self.fn(m, trait_types)
            case S.InstanceDecl(type=ty, methods=methods):
                self.type(ty, types, trait=True)
                for m in methods:
                    self.item(m, types)

    def fn(self, fn: S.Fn | S.Extern, types: Set[str]) -> None:
        types = types | self.params(fn.forall) if isinstance(fn, S.Fn) else types
        if isinstance(fn, S.Fn):
            self.premises(fn.forall, types)
        for p in fn.params:
            if p.type is not None:
                self.type(p.type, types)
        if fn.ret is not None:
            self.type(fn.ret, types)
        if isinstance(fn, S.Fn) and fn.body is not None:
            self.expr(fn.body, types, frozenset(p.name for p in fn.params))

    def decl(self, path: S.NamePath, kind: Optional[str] = None, pos: Optional[Position] = None) -> S.DeclRef:
        target = self.r.lookup(self.module, path, pos)
        if not isinstance(target, Decl) or kind and target.kind != kind:
            fail(f"{'::'.join(path)} is not a {kind or 'declaration'}", pos)
        return ("decl", target)

    # ---- types
    def type(self, type_expr: S.Type, types: Set[str], trait: bool = False) -> None:
        match type_expr:
            case S.TName(path=path, args=args):
                name = path[0] if len(path) == 1 else None
                if name is not None and name in types:
                    type_expr.ref = ("param", name)
                elif name in PRIMS:
                    type_expr.ref = ("prim", name)
                elif name == NEVER:
                    type_expr.ref = ("builtin", name)
                else:
                    with self.reporting():
                        type_expr.ref = self.named_type(path, trait, type_expr.pos)
                for a in args:
                    self.type(a, types)
            case S.TPtr(inner=inner):
                self.type(inner, types)
            case S.TTuple(elems=elems):
                for e in elems:
                    self.type(e, types)
            case S.TArray(elem=elem, length=length):
                self.type(elem, types)
                self.expr(length, types, frozenset())
            case S.TFn(params=params, ret=ret):
                for _, p in params:
                    self.type(p, types)
                self.type(ret, types)
            case S.TClosure(env=env, params=params, ret=ret):
                for _, p in params:
                    self.type(p, types)
                self.type(ret, types)
                if env is not None:
                    self.type(env, types)
            case S.TExists(body=body):
                self.type(body, types)
            case S.TForeach(binder=binder, source=source, body=body):
                self.type(source, types)
                self.type(body, types | {binder})
            case S.TForall(params=params, body=body):
                self.type(body, types | {p for p in params if not p.startswith("'")})

    def named_type(self, path: S.NamePath, trait: bool, pos: Optional[Position]) -> S.DeclRef | S.BuiltinRef:
        """What the type, or with `trait` the trait, written `path` at `pos` names."""
        builtin = None if trait else self.builtin_type(path, pos)
        if builtin is not None:
            return builtin
        ref = self.decl(path, "trait" if trait else None, pos)
        if not trait and ref[1].kind not in ("struct", "enum", "tuple"):
            fail(f"{'::'.join(path)} is not a type", pos)
        return ref

    def builtin_type(self, path: S.NamePath, pos: Optional[Position]) -> Optional[S.BuiltinRef]:
        """What `path` names when that is a builtin: a builtin type, else an error."""
        target = self.r.lookup(self.module, path, pos)
        if not (isinstance(target, tuple) and target[0] == "builtin"):
            return None
        if target[1] not in BUILTIN_TYPES:
            fail(f"{'::'.join(path)} is not a type", pos)
        return ("builtin", target[1])

    # ---- expressions
    def expr(self, expr: S.Expr, types: Set[str], values: Set[str]) -> None:
        """Resolve `expr`; returns nothing. `values` are the locals in scope."""
        def walk(node: S.Expr) -> None:
            self.expr(node, types, values)
        match expr:
            case S.Path():
                self.path(expr, types, values)
            case S.Block():
                self.block(expr, types, values)
            case S.StructLit(path=path, fields=fields, pos=pos):
                with self.reporting():
                    path.ref = self.decl(path.names, "struct", pos)
                self.segment_args(path, types)
                for _, v in fields:
                    if v is not None:
                        walk(v)
                for name, v in fields:
                    if v is None and name not in values:
                        with self.reporting():
                            fail(f"unknown local {name}", pos)
            case S.Match(scrutinee=scrutinee, arms=arms):
                walk(scrutinee)
                for arm in arms:
                    bound = set()
                    for case in arm.cases:
                        if case.kind == "variant":
                            assert case.path is not None
                            with self.reporting():
                                self.variant(case.path)
                            self.segment_args(case.path, types)
                            bound |= {b for b in case.binders or () if b != "_"}
                    self.expr(arm.body, types, values | bound)
            case S.Closure(params=params, ret=ret, body=body):
                for _, _, t in params:
                    if t is not None:
                        self.type(t, types)
                if ret is not None:
                    self.type(ret, types)
                self.expr(body, types, values | {name for _, name, _ in params})
            case S.Foreach(source=source, binder=binder, body=body, name=name):
                walk(source)
                self.expr(body, types | ({binder} if binder else set()), values | {name})
            case S.Cast(value=value, type=ty):
                walk(value)
                self.type(ty, types)
            case S.While(cond=cond, body=body):
                walk(cond)
                walk(body)
            case S.For(iterator=iterator, body=body, name=name):
                walk(iterator)
                self.expr(body, types, values | {name})
            case S.Lit():
                pass
            case _:
                for child in children(expr):
                    walk(child)

    def block(self, block: S.Block, types: Set[str], values: Set[str]) -> None:
        for s in block.stmts:
            values = self.stmt(s, types, values)
        if block.tail is not None:
            self.expr(block.tail, types, values)

    def stmt(self, stmt: S.Statement, types: Set[str], values: Set[str]) -> Set[str]:
        match stmt:
            case S.Let(pattern=pattern, type=ty, value=value):
                self.expr(value, types, values)
                if ty is not None:
                    self.type(ty, types)
                return values | bound(pattern)
            case S.Var(name=name, type=ty, value=value):
                self.expr(value, types, values)
                if ty is not None:
                    self.type(ty, types)
                return values | {name}
            case S.Const(name=name, type=ty, value=value):
                self.type(ty, types)
                self.expr(value, types, values)
                return values | {name}
            case S.Assign(target=target, value=value):
                self.expr(target, types, values)
                self.expr(value, types, values)
            case S.ExprStmt(value=value):
                self.expr(value, types, values)
        return values

    def segment_args(self, path: S.Path, types: Set[str]) -> None:
        for _, args in path.segments:
            for a in args:
                self.type(a, types)

    def path(self, expr: S.Path, types: Set[str], values: Set[str]) -> None:
        names = expr.names
        self.segment_args(expr, types)
        if len(names) == 1 and names[0] in values:
            expr.ref = ("local", names[0])
        elif len(names) == 2 and names[0] in PRIMS:
            found = self.r.attached.get((names[0], names[1]))
            with self.reporting():
                if not found:
                    fail(f"unknown {names[0]}::{names[1]}", expr.pos)
                expr.ref = ("decl", found[0])
        elif len(names) == 2 and names[0] in types:
            expr.ref = ("assoc", names[0], names[1])  # a member of a type parameter, found by inference
        else:
            with self.reporting():
                target = self.r.lookup(self.module, names, expr.pos)
                if isinstance(target, ModuleId):
                    fail(f"{'::'.join(names)} is a module, not a value", expr.pos)
                expr.ref = target if isinstance(target, tuple) else ("decl", target)

    def variant(self, path: S.Path) -> None:
        target = self.r.lookup(self.module, path.names, path.pos)
        if not (isinstance(target, tuple) and target[0] == "variant"):
            fail(f"{'::'.join(path.names)} is not an enum variant", path.pos)
        path.ref = target


def bound(pattern: S.Pattern) -> set[str]:
    match pattern:
        case S.PBind(name=name):
            return {name}
        case S.PTuple(elems=elems) | S.PArray(elems=elems):
            return set().union(*(bound(p) for p in elems))
        case S.PStruct(fields=fields, rest=rest):
            return set().union(*(bound(p) for _, p in fields)) | ({rest} if rest else set())
    return set()


def children(expr: S.Expr) -> Sequence[S.Expr]:
    """The subexpressions of the expression forms with no binding structure."""
    match expr:
        case S.Tuple(elems=elems) | S.Array(elems=elems):
            return elems
        case S.FStr(parts=parts):
            return [p for p in parts if not isinstance(p, str)]
        case S.If(cond=cond, then=then, other=other):
            return [cond, then] + ([other] if other is not None else [])
        case S.Return(value=value):
            return [value] if value is not None else []
        case S.Pack(value=value):
            return [value]
        case S.Binop(left=left, right=right):
            return [left, right]
        case S.Unary(operand=operand):
            return [operand]
        case S.UnsafeStore(pointer=pointer, value=value):
            return [pointer, value]
        case S.Field(value=value) | S.Index(value=value) | S.Try(value=value):
            return [value]
        case S.Call(callee=callee, args=args):
            return [callee, *args]
    raise KursError(f"no resolution rule for {type(expr).__name__}")


def resolve(path: str | Path) -> Resolver:
    """The resolved package graph rooted at `path`."""
    resolver = Resolver(load(path))
    for package in resolver.packages:
        if package.entry:
            try:
                target = resolver.lookup(ModuleId(package.id), package.entry)
                if not isinstance(target, Decl) or target.kind != "fn" or target.module.package != package.id:
                    fail("an executable's entry must be a function of its own package")
                package.entry_decl = target
            except ResolveError as error:
                resolver.report(error, ModuleId(package.id), file=package.manifest)
        break
    return Names(resolver).run()
