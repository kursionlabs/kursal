"""The standard library at run time: small programs that use it, built with the C runtime and run, each
checking its own results; and programs that misuse it, rejected."""

import os
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import IO, Callable, Optional

from kursal.cc import FLAGS, RUNTIME, c_compiler, cc_command
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures

MANIFEST = '[package]\nname="t"\nkind="executable"\nentry="package::main"\nsource_root="."\n'
# Counters a program built with `counted=True` reads through its own externs: the calls of `malloc` and
# `realloc` and the bytes they were asked for, and the calls of `read`, made by the program's own C and
# the library's (the linker sends them through these wrappers), not by the C library internally.
COUNTERS = """
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>
void *__real_malloc(size_t size);
void *__real_realloc(void *block, size_t size);
ssize_t __real_read(int fd, void *buf, size_t count);
static uint64_t allocations, allocated, reads;
void *__wrap_malloc(size_t size) { allocations++; allocated += size; return __real_malloc(size); }
void *__wrap_realloc(void *block, size_t size) { allocations++; allocated += size; return __real_realloc(block, size); }
ssize_t __wrap_read(int fd, void *buf, size_t count) { reads++; return __real_read(fd, buf, count); }
uint64_t probe_allocations(void) { return allocations; }
uint64_t probe_allocated(void) { return allocated; }
uint64_t probe_reads(void) { return reads; }
"""
COUNTED_LINK = ("-Wl,--wrap=malloc,--wrap=realloc,--wrap=read",)


# Programs that read and write files and standard input through `Input!` and `Output!`, by name: each
# one's source, its standard input and what it prints, exiting 0. tests/test_compiler.py builds them with
# the self-hosted compiler too.
IO_PROGRAMS: dict[str, tuple[str, str, str]] = {
    "input over a file": ('''
    use std::buffer::Buffer!;
    use std::fmt::Writer;
    use std::fs::write;
    use std::io::{Error, File!, Input!, print};
    use std::option::{Option, Result};
    use std::process::trap;
    use std::str::StaticRead;
    fn shown(&out: Buffer!, line: Result[Option[Buffer!], Error]) {
        out = match line {
            Result::Ok(found) => match found {
                Option::Some(text) => out.write_byte(b'[').put(text).write_byte(b']'),
                Option::None => out.put("end"),
            },
            Result::Err(e) => out.put("error ").put(e.errno),
        };
    }
    fn done(input: Input!) -> Buffer! {
        match input.finish() {
            Result::Ok(rest) => rest,
            Result::Err(e) => trap(),
        }
    }
    fn main() -> i32 {
        var sr = StaticRead::new();
        let made = write(&sr, "in.txt", Buffer!::new().put("first\\nsecond\\nthird line\\nrest of it"));
        var out = Buffer!::new();
        var input = match File!::open_read(&sr, "in.txt") { Result::Ok(f) => f.input(), Result::Err(e) => trap(), };
        shown(&out, &input.read_line());
        var chunk = Buffer!::with_capacity(0usize);
        let some = (&input.read(&chunk, 3usize)).unwrap_or(9usize);
        let none = (&input.read(&chunk, 0usize)).unwrap_or(9usize);
        let exact = (&input.read_exact(&chunk, 5usize)).unwrap_or(9usize);
        out = out.put(f" {some} {none} {exact} ").put(chunk).put(" ");
        shown(&out, &input.read_line());
        out = match input.read_to_end() {
            Result::Ok(text) => out.put(" (").put(text).put(") "),
            Result::Err(e) => out.put(" error ").put(e.errno),
        };
        var second = match File!::open_read(&sr, "in.txt") { Result::Ok(f) => f.input(), Result::Err(e) => trap(), };
        shown(&out, &second.read_line());
        out = out.put(" ahead ").put(done(second)).put(" ");
        let emptied = write(&sr, "empty.txt", Buffer!::new());
        var last = match File!::open_read(&sr, "empty.txt") { Result::Ok(f) => f.input(), Result::Err(e) => trap(), };
        shown(&out, &last.read_line());
        var empty = Buffer!::with_capacity(0usize);
        let end = (&last.read(&empty, 4usize)).unwrap_or(9usize);
        let short = (&last.read_exact(&empty, 4usize)).unwrap_or(9usize);
        empty.destroy();
        var left = done(last);
        let n = &left.len();
        left.destroy();
        out = out.put(f" {end} {short} {n}");
        let failed = match File!::open_read(&sr, ".") {
            Result::Err(e) => 0i32,
            Result::Ok(dir) => {
                var dir = dir.input();
                var bytes = Buffer!::new().put("kept");
                let read = &dir.read(&bytes, 4usize);
                done(dir).destroy();
                out = out.put(" ").put(bytes);
                match read {
                    Result::Ok(n) => 0i32,
                    Result::Err(e) => e.errno,
                }
            },
        };
        print(out.put(f" {failed}"));
        0
    }''', "", "[first] 3 0 5 second\nt [hird line] (rest of it) [first] ahead second\nthird line\nrest of it end 0 0 0 kept 21"),
    "output over a file": ('''
    use std::buffer::Buffer!;
    use std::fmt::Writer;
    use std::fs::read;
    use std::io::{Error, File!, Output!, print};
    use std::option::Result;
    use std::process::trap;
    use std::str::StaticRead;
    fn large() -> Buffer! {
        var big = Buffer!::new();
        var i = 0usize;
        while i < 8192usize {
            big = big.write_byte(b'x');
            i = i + 1usize;
        }
        big
    }
    fn errno(r: Result[(), Error]) -> i32 {
        match r {
            Result::Ok(nothing) => 0i32,
            Result::Err(e) => e.errno,
        }
    }
    fn full() -> Output! {
        var sr = StaticRead::new();
        match File!::create(&sr, "/dev/full") {
            Result::Ok(f) => f.output(),
            Result::Err(e) => trap(),
        }
    }
    fn main() -> i32 {
        var sr = StaticRead::new();
        let out = match File!::create(&sr, "out.txt") { Result::Ok(f) => f.output(), Result::Err(e) => trap(), };
        let written = errno(out.put("one ").write(&sr, "two ").put(f"{3i32} ").put(large()).put(" end").finish());
        let back = match read(&sr, "out.txt") {
            Result::Ok(text) => text,
            Result::Err(e) => trap(),
        };
        let small = errno(full().put("full").finish());
        let big = errno(full().put(large()).put("after").finish());
        print(back.put(f" {written} {small} {big}"));
        0
    }''', "", "one two 3 " + "x" * 8192 + " end 0 28 28"),
    "input over stdin": ('''
    use std::buffer::Buffer!;
    use std::fmt::Writer;
    use std::io::{Error, Input!, print, stdin};
    use std::process::trap;
    use std::option::{Option, Result};
    fn shown(&out: Buffer!, line: Result[Option[Buffer!], Error]) {
        out = match line {
            Result::Ok(found) => match found {
                Option::Some(text) => out.write_byte(b'[').put(text).write_byte(b']'),
                Option::None => out.put("end"),
            },
            Result::Err(e) => out.put("error ").put(e.errno),
        };
    }
    fn done(input: Input!) -> Buffer! {
        match input.finish() {
            Result::Ok(rest) => rest,
            Result::Err(e) => trap(),
        }
    }
    fn errno(r: Result[Input!, Error]) -> i32 {
        match r {
            Result::Ok(input) => { done(input).destroy(); 0i32 },
            Result::Err(e) => e.errno,
        }
    }
    fn main() -> i32 {
        var out = Buffer!::new();
        var input = match stdin() { Result::Ok(i) => i, Result::Err(e) => trap(), };
        shown(&out, &input.read_line());
        shown(&out, &input.read_line());
        let again = errno(stdin());
        var chunk = Buffer!::new();
        let some = (&input.read(&chunk, 1usize)).unwrap_or(9usize);
        let exact = (&input.read_exact(&chunk, 2usize)).unwrap_or(9usize);
        out = out.put(f" {some} {exact} ").put(chunk);
        shown(&out, &input.read_line());
        out = out.put(" ahead ").put(done(input));
        var second = match stdin() { Result::Ok(i) => i, Result::Err(e) => trap(), };
        shown(&out, &second.read_line());
        done(second).destroy();
        let third = match stdin() { Result::Ok(i) => i, Result::Err(e) => trap(), };
        let none = match third.read_to_end() { Result::Ok(text) => { var text = text; let n = &text.len(); text.destroy(); n }, Result::Err(e) => 9usize };
        var fourth = match stdin() { Result::Ok(i) => i, Result::Err(e) => trap(), };
        shown(&out, &fourth.read_line());
        done(fourth).destroy();
        let printed = print(out);
        if again == 16i32 && none == 0usize { 0 } else { 4 }
    }''', "a\n\nb\ncd\ne\nlast", "[a][] 1 2 b\nc[d] ahead e\nlastendend"),
}


def siphash13(seed: tuple[int, int], words: list[int]) -> int:
    """SipHash-1-3 under the key `seed` of the 64-bit `words`, each as its 8 little-endian bytes."""
    mask = (1 << 64) - 1

    def rotl(x: int, r: int) -> int:
        return ((x << r) | (x >> (64 - r))) & mask

    def sip_round(v: list[int]) -> None:
        v[0] = (v[0] + v[1]) & mask; v[1] = rotl(v[1], 13) ^ v[0]; v[0] = rotl(v[0], 32)
        v[2] = (v[2] + v[3]) & mask; v[3] = rotl(v[3], 16) ^ v[2]
        v[0] = (v[0] + v[3]) & mask; v[3] = rotl(v[3], 21) ^ v[0]
        v[2] = (v[2] + v[1]) & mask; v[1] = rotl(v[1], 17) ^ v[2]; v[2] = rotl(v[2], 32)

    k0, k1 = seed
    v = [k0 ^ 0x736f6d6570736575, k1 ^ 0x646f72616e646f6d, k0 ^ 0x6c7967656e657261, k1 ^ 0x7465646279746573]
    for m in [*words, ((8 * len(words)) % 256) << 56]:
        v[3] ^= m
        sip_round(v)
        v[0] ^= m
    v[2] ^= 0xff
    for _ in range(3):
        sip_round(v)
    return v[0] ^ v[1] ^ v[2] ^ v[3]


def package(directory: Path, source: str, deps: tuple[str, ...]) -> None:
    manifest = MANIFEST + "".join(f'[dependencies.{d}]\npath="{LIB / d}"\n' for d in deps)
    (directory / "kur.toml").write_text(manifest)
    (directory / "lib.kur").write_text(source)


def lowered(directory: Path):
    sys.setrecursionlimit(20000)
    resolver = resolve(directory)
    sigs = signatures(resolver)
    program = lower(resolver, sigs)
    return resolver, sigs, program


class StdRuntimeTests(unittest.TestCase):
    def run_program(self, source: str, deps: tuple[str, ...] = ("std",), stdin: str = "",
                    args: tuple[str, ...] = (), preexec: Optional[Callable[[], None]] = None,
                    stdout: Optional[IO[str]] = None, stdin_fd: Optional[int] = None, counted: bool = False,
                    ) -> subprocess.CompletedProcess[str]:
        """Builds `source` as a package with `deps` and runs it; the completed process. With `counted`, the
        program is linked with COUNTERS."""
        with tempfile.TemporaryDirectory(prefix="kursal-std-") as base:
            directory = Path(base)
            package(directory, source, deps)
            resolver, sigs, program = lowered(directory)
            c_file = directory / "main.c"
            c_file.write_text(emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl), hosted=True))
            executable = directory / "main"
            sources, link = [str(c_file), *map(str, RUNTIME)], ()
            if counted:
                (directory / "counters.c").write_text(COUNTERS)
                sources, link = [*sources, str(directory / "counters.c")], COUNTED_LINK
            built = subprocess.run(cc_command(sources, str(executable), *link), capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr[:500])
            source_in: dict[str, object] = {"input": stdin} if stdin_fd is None else {"stdin": stdin_fd}
            return subprocess.run([str(executable), *args], **source_in,  # type: ignore[call-overload]
                                  stdout=stdout if stdout is not None else subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=60, cwd=directory, preexec_fn=preexec)

    def expect(self, source: str, status: int = 0, stdout: str = "", **kwargs) -> None:
        result = self.run_program(source, **kwargs)
        self.assertEqual((result.stdout, result.returncode), (stdout, status), result.stderr[:500])

    def expect_program(self, source: str, stdin: str, stdout: str) -> None:
        self.expect(source, stdout=stdout, stdin=stdin)

    def reject(self, source: str, message: str, deps: tuple[str, ...] = ("std",)) -> None:
        with tempfile.TemporaryDirectory(prefix="kursal-std-") as base:
            directory = Path(base)
            package(directory, source, deps)
            with self.assertRaisesRegex(KursError, message):
                resolver, sigs, program = lowered(directory)
                check_program(program)

    # ------------------------------------------------------------ alloc::owned --

    def test_adopt_value_writes_the_value_it_adopts(self):
        self.expect('''
            use std::heap::HeapAllocator;
            use std::owned::Allocation!;
            fn main() -> i32 {
                var exists 'r. (record, region) = Allocation!::adopt_value(HeapAllocator::alloc(8usize), 41u64);
                let old = &region.replace(1u64);
                HeapAllocator::drop(record.release(region));
                if old == 41u64 { 0 } else { 1 }
            }''')

    def test_adopt_value_aborts_on_a_block_too_small(self):
        result = self.run_program('''
            use std::heap::HeapAllocator;
            use std::owned::Allocation!;
            fn main() -> i32 {
                var exists 'r. (record, region) = Allocation!::adopt_value(HeapAllocator::alloc(4usize), 41u64);
                let old = &region.replace(1u64);
                HeapAllocator::drop(record.release(region));
                0
            }''')
        self.assertLess(result.returncode, 0)

    def test_owned_and_vec_round_trips(self):
        self.expect('''
            use std::owned::Owned!;
            use std::collections::vec::Vec!;
            use std::buffer::Buffer!;
            fn main() -> i32 {
                var exists 'a. (record, region) = Owned!::new(7u32).open();
                let seven = &region.replace(8u32);
                let eight = record.close(region).into_value();
                var exists 'v. (tag, v) = Vec!::new[u8]();
                let n = &v.len();
                let empty = &v.is_empty();
                v.destroy(tag);
                if seven == 7u32 && eight == 8u32 && n == 0usize && empty { 0 } else { 1 }
            }''')

    def test_fallible_constructors_report_exhaustion(self):
        # Under a 256 MiB address-space limit, a 1 GiB buffer and a vec of four 128 MiB slots are refused;
        # the constructors that fit build containers that work.
        def limit() -> None:
            resource.setrlimit(resource.RLIMIT_AS, (256 << 20, 256 << 20))
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::collections::hashmap::{HashMap!, OwnedMap!};
            use std::collections::vec::{OwnedVec!, Vec!};
            use std::io::print;
            use std::option::Result;
            fn forget(k: u32, v: u32) -> () { () }
            fn main() -> i32 {
                let big_buffer = match Buffer!::try_with_capacity(1usize << 30usize) {
                    Result::Ok(b) => { b.destroy(); "built" },
                    Result::Err(e) => "refused",
                };
                let big_vec = match Vec!::try_new[[u64; 16777216]]() {
                    Result::Ok(package) => {
                        var exists 'v. (tag, v) = package;
                        v.destroy(tag);
                        "built"
                    },
                    Result::Err(e) => "refused",
                };
                let buffer = match Buffer!::try_with_capacity(2usize) {
                    Result::Ok(b) => { var b = b.put("abc"); let n = &b.len(); b.destroy(); n },
                    Result::Err(e) => 0usize,
                };
                let vec = match Vec!::try_new[u64]() {
                    Result::Ok(package) => {
                        var exists 'v. (tag, v) = package;
                        var exists 'w. (tag, v) = v.push(tag, 7u64);
                        let n = &v.len();
                        v.destroy(tag);
                        n
                    },
                    Result::Err(e) => 0usize,
                };
                let map = match HashMap!::try_new[u32, u32](std::process::hash_seed()) {
                    Result::Ok(package) => {
                        var exists 'm. (tag, m) = package;
                        var exists 'n. (tag, m, old) = m.insert(tag, 1u32, 2u32);
                        let n = &m.len();
                        m.destroy(tag);
                        n
                    },
                    Result::Err(e) => 0usize,
                };
                let owned_vec = match OwnedVec!::try_new[u32]() {
                    Result::Ok(v) => { var v = v; &v.push(3u32); &v.push(4u32); let n = &v.len(); v.destroy(); n },
                    Result::Err(e) => 0usize,
                };
                let owned_map = match OwnedMap!::try_new[u32, u32](std::process::hash_seed()) {
                    Result::Ok(m) => { var m = m; let old = &m.insert(1u32, 2u32); let n = &m.len(); m.destroy_with(forget); n },
                    Result::Err(e) => 0usize,
                };
                print(f"{big_buffer} {big_vec} {buffer} {vec} {map} {owned_vec} {owned_map}");
                0
            }''', stdout="refused refused 3 1 1 2 1", preexec=limit)

    # -------------------------------------------------------- alloc::hashmap --

    def test_remove_in_rebrands_and_removes(self):
        self.expect('''
            use std::collections::hashmap::{HashMap!, OwnedMap!};
            use std::option::Option;
            fn drop_entry(key: u32, value: u32) {}
            fn main() -> i32 {
                var cx = ();
                var exists 'm. (tag, map) = HashMap!::new[u32, u32](std::process::hash_seed());
                var exists 'a. (tag, map, old) = map.insert_in(tag, &cx, 1u32, 10u32);
                var exists 'b. (tag, map, removed) = map.remove_in(tag, &cx, 1u32);
                let again = match &map.get_in(&tag, &cx, 1u32) { Option::Some(r) => 1i32, Option::None => 0i32 };
                let got = match removed { Option::Some(v) => v, Option::None => 0u32 };
                map.destroy_with(tag, drop_entry);
                var owned = OwnedMap!::new[u32, u32](std::process::hash_seed());
                let none = &owned.insert_in(&cx, 2u32, 20u32);
                let twenty = match &owned.remove_in(&cx, 2u32) { Option::Some(v) => v, Option::None => 0u32 };
                let left = &owned.len();
                owned.destroy_with(drop_entry);
                if again == 0i32 && got == 10u32 && twenty == 20u32 && left == 0usize { 0 } else { 1 }
            }''')

    # ---------------------------------------------------------------- thread --

    def test_trampoline_is_unsafe(self):
        self.reject('''
            use std::thread::trampoline;
            use std::heap::HeapAllocator;
            fn main() -> i32 {
                let raw = HeapAllocator::alloc(8usize).into_raw();
                let result = trampoline[(), u32](raw);
                0
            }''', "calling an unsafe function needs unsafe")

    def test_spawn_returns_a_zero_size_result(self):
        self.expect('''
            use std::thread;
            use std::option::Result;
            fn main() -> i32 {
                match thread::spawn(|| -> () { () }) {
                    Result::Err(failed) => {
                        let (f, error) = failed;
                        f();
                        1
                    },
                    Result::Ok(t) => {
                        t.join();
                        0
                    },
                }
            }''')

    @unittest.skipIf(os.geteuid() == 0, "the process limit does not bind root")
    def test_failed_spawn_hands_the_closure_back(self):
        def no_more_processes() -> None:
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        self.expect('''
            use std::thread;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::option::Result;
            fn main() -> i32 {
                let owned = Buffer!::new().put("abc");
                match thread::spawn(|| -> usize { var b = owned; let n = &b.len(); b.destroy(); n }) {
                    Result::Err(failed) => {
                        let (f, error) = failed;
                        if f() == 3usize && error.errno > 0i32 { 7 } else { 1 }
                    },
                    Result::Ok(t) => {
                        t.join();
                        2
                    },
                }
            }''', status=7, preexec=no_more_processes)

    # ----------------------------------------------------------------- arena --

    def test_arena_aligns_addresses(self):
        self.expect('''
            use std::arena::Arena!;
            fn main() -> i32 {
                var exists 'k. (tag, arena) = Arena!::new(std::process::hash_seed());
                var bad = 0i32;
                var i = 0usize;
                while i < 5000usize {
                    let b = &arena.alloc(&tag, 1u8);
                    let w = &arena.alloc(&tag, 2u64);
                    if w.address() % 8usize != 0usize || w.copy(&tag) != 2u64 || b.copy(&tag) != 1u8 {
                        bad = 1i32;
                    }
                    i = i + 1usize;
                }
                arena.destroy(tag);
                bad
            }''')

    def test_linear_values_stay_out_of_arenas(self):
        self.reject('''
            use std::arena::Domain!;
            use std::collections::list::List;
            use std::buffer::Buffer!;
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let list = List::empty().cons(&cx, Buffer!::new());
                cx.destroy();
                0
            }''', "type argument must satisfy Copy")

    # -------------------------------------------------------------------- fs --

    def test_files_and_directories_by_text(self):
        # Paths are text in an arena, read in place; each step's failure has its own status.
        self.expect('''
            use std::arena::Domain!;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::io::{Error, File!};
            use std::fs::{FileKind, canonicalize, metadata, mkdir, path_exists, read_dir, read_to_arena, remove, rename, write};
            use std::option::{Option, Result};
            use std::str::Str;
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                var cx = cx;
                let dir = Str::from_static(&cx, "d.dir/");
                let dir = dir.take(5usize);
                let a = Buffer!::new().put("d.dir/a").save(&cx);
                let b = Buffer!::new().put("d.dir/b").save(&cx);
                let none = Error.{ errno: 0i32 };
                var status = 0i32;
                if mkdir(&cx, dir).is_err() { status = 1; }
                if mkdir(&cx, dir).err().unwrap_or(none).errno != 17i32 { status = 2; }
                if write(&cx, a, Buffer!::new().put("abc")).is_err() { status = 3; }
                if rename(&cx, a, b).is_err() || path_exists(&cx, a).unwrap_or(true) || !path_exists(&cx, b).unwrap_or(false) { status = 4; }
                match read_to_arena(&cx, b) {
                    Result::Ok(text) => if !text.is(&cx, "abc") { status = 5; },
                    Result::Err(e) => { status = 5; },
                }
                match metadata(&cx, b) {
                    Result::Ok(m) => match m.kind { FileKind::File => if m.size != 3u64 { status = 6; }, _ => { status = 6; } },
                    Result::Err(e) => { status = 6; },
                }
                match File!::open_read(&cx, b) {
                    Result::Ok(f) => {
                        var f = f;
                        if (&f.file_size()).unwrap_or(0u64) != 3u64 { status = 6; }
                        let closed = f.close();
                    },
                    Result::Err(e) => { status = 6; },
                }
                match metadata(&cx, dir) {
                    Result::Ok(m) => match m.kind { FileKind::Directory => (), _ => { status = 7; } },
                    Result::Err(e) => { status = 7; },
                }
                match canonicalize(&cx, b) {
                    Result::Ok(path) => {
                        let full = path.save(&cx);
                        if !full.starts_with(&cx, Str::from_static(&cx, "/")) || !full.ends_with(&cx, Str::from_static(&cx, "/d.dir/b")) { status = 8; }
                    },
                    Result::Err(e) => { status = 8; },
                }
                var names = 0usize;
                match read_dir(&cx, dir) {
                    Result::Ok(d) => {
                        var d = d;
                        var going = true;
                        while going {
                            match &d.next() {
                                Result::Ok(next) => match next {
                                    Option::Some(entry) => {
                                        let { name, is_directory } = entry;
                                        let n = name.save(&cx);
                                        if !n.is(&cx, "b") || is_directory { status = 9; }
                                        names = names + 1usize;
                                    },
                                    Option::None => { going = false; },
                                },
                                Result::Err(e) => { going = false; status = 9; },
                            }
                        }
                        let closed = d.close();
                    },
                    Result::Err(e) => { status = 9; },
                }
                if names != 1usize { status = 10; }
                if remove(&cx, dir).err().unwrap_or(none).errno != 39i32 { status = 11; }
                if remove(&cx, b).is_err() || remove(&cx, dir).is_err() || path_exists(&cx, dir).unwrap_or(true) { status = 12; }
                if metadata(&cx, dir).err().unwrap_or(none).errno != 2i32 { status = 13; }
                let nul = Buffer!::new().put("a").write_byte(0u8).put("b").save(&cx);
                if mkdir(&cx, nul).err().unwrap_or(none).errno != 22i32 { status = 14; }
                cx.destroy();
                status
            }''')

    # ------------------------------------------------------------------- env --

    def test_environment_variables(self):
        os.environ["KURSAL_TEST_VARIABLE"] = "set=yes"
        try:
            self.expect('''
                use std::env::variable;
                use std::io::print;
                use std::option::Option;
                use std::str::StaticRead;
                fn main() -> i32 {
                    var sr = StaticRead::new();
                    match variable(&sr, "KURSAL_TEST_VARIABLE") {
                        Option::Some(value) => { print(value); },
                        Option::None => (),
                    }
                    match variable(&sr, "KURSAL_TEST_UNSET_VARIABLE") {
                        Option::Some(value) => { value.destroy(); 1 },
                        Option::None => match variable(&sr, "KURSAL=TEST") {
                            Option::Some(value) => { value.destroy(); 2 },
                            Option::None => 0,
                        },
                    }
                }''', stdout="set=yes")
        finally:
            del os.environ["KURSAL_TEST_VARIABLE"]

    # --------------------------------------------------------------- process --

    def test_args_are_the_entry_parameter(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::io::print;
            use std::option::Option;
            use std::process::Args;
            fn main(args: Args) -> i32 {
                var out = Buffer!::new();
                var i = 1usize;
                while i < args.len() {
                    match args.bytes(i) {
                        Option::Some(arg) => { out = out.put(arg).put("|"); },
                        Option::None => (),
                    }
                    i = i + 1usize;
                }
                print(out);
                if args.bytes(args.len()).is_none() { args.len() as i32 } else { 99 }
            }''', status=4, stdout="one|two words||", args=("one", "two words", ""))

    def test_each_entry_shape_makes_its_exit_status(self):
        """`fn main()`, `-> i32` and `-> Result[(), Error]`, each with and without `Args`: the exit status
        and stderr of each, and the report of an `Err`."""
        imports = '''
            use std::io::{Error, File!, print};
            use std::option::Result;
            use std::process::Args;
            use std::str::StaticRead;
        '''
        missing = '''
                var read = StaticRead::new();
                let file = File!::open_read(&read, "missing")?;
                file.close()'''
        cases = (
            ('fn main() { print("unit"); }', (), "unit", 0, ""),
            ('fn main(args: Args) { if args.len() == 2usize { print("two"); } }', ("x",), "two", 0, ""),
            ("fn main() -> i32 { 7 }", (), "", 7, ""),
            ("fn main(args: Args) -> i32 { args.len() as i32 }", ("x", "y"), "", 3, ""),
            ('fn main() -> Result[(), Error] { print("ok") }', (), "ok", 0, ""),
            (f"fn main() -> Result[(), Error] {{{missing}\n}}", (), "", 1,
             "error: No such file or directory (error 2)\n"),
            ("fn main(args: Args) -> Result[(), Error] { Result::Err.(Error.{ errno: args.len() as i32 + 11 }) }",
             ("x",), "", 1, "error: Permission denied (error 13)\n"),
        )
        for source, args, stdout, status, stderr in cases:
            with self.subTest(source=source):
                result = self.run_program(imports + source, args=args)
                self.assertEqual((result.stdout, result.returncode, result.stderr), (stdout, status, stderr))

    def test_an_entry_of_another_shape_is_rejected(self):
        for source in ("fn main(argc: i32, argv: *(*u8)) -> i32 { 0 }\n", "fn main() -> u32 { 0 }\n",
                       "use std::io::Error;\nuse std::option::Result;\nfn main() -> Result[u32, Error] { Result::Ok.(0) }\n"):
            with self.subTest(source=source), tempfile.TemporaryDirectory(prefix="kursal-std-") as base:
                directory = Path(base)
                package(directory, source, ("std",))
                resolver, sigs, program = lowered(directory)
                with self.assertRaises(KursError) as raised:
                    emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl))
                self.assertEqual(str(raised.exception),
                                 "backend: the entry t::main must be `fn()`, `fn() -> i32` or "
                                 "`fn() -> Result[(), std::io::Error]`, each with or without a parameter "
                                 "`args: std::process::Args`, and safe, not generic and without premises")

    def test_run_reports_exit_signal_and_spawn_errors(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::fs::write;
            use std::io::print;
            use std::option::Result;
            use std::process::{Command!, Exit};
            use std::str::StaticRead;
            fn outcome(command: Command!) -> Buffer! {
                match command.run() {
                    Result::Ok(exit) => match exit {
                        Exit::Code(c) => Buffer!::new().put("code ").put(c),
                        Exit::Signal(s) => Buffer!::new().put("signal ").put(s),
                    },
                    Result::Err(e) => Buffer!::new().put("errno ").put(e.errno),
                }
            }
            fn main() -> i32 {
                var sr = StaticRead::new();
                let made = write(&sr, "not-executable", Buffer!::new().put("#!/bin/sh"));
                print(outcome(Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, "exit 7")).put(","));
                print(outcome(Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, "kill -15 $$")).put(","));
                print(outcome(Command!::new(&sr, "no-such-program-for-kursal")).put(","));
                print(outcome(Command!::new(&sr, "./not-executable")).put(","));
                print(outcome(Command!::new(&sr, "true").arg(&sr, "a\\x00b")));
                Command!::new(&sr, "never").arg(&sr, "run").destroy();
                0
            }''', stdout="code 7,signal 15,errno 2,errno 13,errno 22")

    def test_spawned_children_are_polled_and_waited_for(self):
        self.expect('''
            use std::io::print;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::option::Result;
            use std::process::{Child!, Command!, Exit, Polled!, available_parallelism, trap};
            use std::str::StaticRead;
            fn started(command: Command!) -> Child! {
                match command.spawn() {
                    Result::Ok(child) => child,
                    Result::Err(e) => trap(),
                }
            }
            fn code(exit: Exit) -> usize {
                match exit {
                    Exit::Code(c) => c as usize,
                    Exit::Signal(s) => 1000usize + s as usize,
                }
            }
            fn main() -> i32 {
                var sr = StaticRead::new();
                let slow = started(Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, "sleep 0.2; exit 3"));
                let quick = started(Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, "exit 4"));
                var polls = 0usize;
                var state = Polled!::Running.(slow);
                var polling = true;
                while polling {
                    state = match state {
                        Polled!::Running(child) => match child.try_wait() {
                            Result::Ok(p) => {
                                polls = polls + 1usize;
                                p
                            },
                            Result::Err(e) => trap(),
                        },
                        Polled!::Ended(exit) => {
                            polling = false;
                            Polled!::Ended.(exit)
                        },
                    };
                }
                let slow_code = match state {
                    Polled!::Running(child) => trap(),
                    Polled!::Ended(exit) => code(exit),
                };
                let quick_code = match quick.wait() {
                    Result::Ok(exit) => code(exit),
                    Result::Err(e) => trap(),
                };
                let missing = match Command!::new(&sr, "no-such-program-for-kursal").spawn() {
                    Result::Ok(child) => { let ended = child.wait(); 0usize },
                    Result::Err(e) => e.errno as usize,
                };
                print(Buffer!::new().put(slow_code).put(",").put(quick_code).put(",")
                    .put(missing).put(if polls > 1usize { ",polled" } else { ",ended at once" })
                    .put(if available_parallelism() > 0usize { ",cpus" } else { ",none" }));
                0
            }''', stdout="3,4,2,polled,cpus")

    def test_output_captures_both_streams_and_the_exit(self):
        # A child writing 1 MiB to each of its standard output and error, the second first, fills one pipe
        # while this process would wait on the other: both are read as they come.
        self.expect('''
            use std::buffer::Buffer!;
            use std::io::print;
            use std::option::Result;
            use std::process::{Captured!, Command!, Exit};
            use std::str::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                let script = "head -c 1048576 /dev/zero >&2; head -c 1048576 /dev/zero; echo out; echo err >&2; exit 3";
                match Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, script).output() {
                    Result::Ok(captured) => {
                        var { exit, stdout, stderr } = captured;
                        let code = match exit { Exit::Code(c) => c, Exit::Signal(s) => 0i32 - s };
                        let out = &stdout.len();
                        let err = &stderr.len();
                        let success = exit.success();
                        let sizes = f"{code} {out} {err} {success}";
                        stdout.destroy();
                        stderr.destroy();
                        print(sizes);
                        0
                    },
                    Result::Err(e) => 1,
                }
            }''', stdout="3 1048580 1048580 false")

    def test_commands_take_their_streams_environment_and_directory(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::fs::{mkdir, write};
            use std::io::{File!, print};
            use std::option::{Option, Result};
            use std::process::{Captured!, Command!, Exit, Stdio!};
            use std::str::StaticRead;
            // What `command` writes to its standard output, its standard error after a `|`.
            fn run(command: Command!) -> Buffer! {
                match command.output() {
                    Result::Ok(captured) => {
                        let { exit, stdout, stderr } = captured;
                        stdout.put("|").put(stderr)
                    },
                    Result::Err(e) => f"error {e.errno}",
                }
            }
            fn main() -> i32 {
                var sr = StaticRead::new();
                let made = write(&sr, "input.txt", Buffer!::new().put("from a file"));
                let dir = mkdir(&sr, "sub");
                let marked = write(&sr, "sub/marker", Buffer!::new().put("here"));
                let sh = "sh";
                var out = run(Command!::new(&sr, sh).arg(&sr, "-c").arg(&sr, "printf %s \\"$KURSAL_A$KURSAL_B\\"; printf e >&2")
                    .env(&sr, "KURSAL_A", "set").env(&sr, "KURSAL_B", "gone").env_remove(&sr, "KURSAL_B"));
                out = out.put(",").put(run(Command!::new(&sr, "/usr/bin/env").env_clear().env(&sr, "ONLY", "1")));
                out = out.put(",").put(run(Command!::new(&sr, "cat").arg(&sr, "marker").current_dir(&sr, "sub")));
                let input = match File!::open_read(&sr, "input.txt") { Result::Ok(f) => Stdio!::File.(f), Result::Err(e) => Stdio!::Null };
                out = out.put(",").put(run(Command!::new(&sr, "cat").stdin(input)));
                out = out.put(",").put(run(Command!::new(&sr, "cat").stdin(Stdio!::Null)));
                out = out.put(",").put(match Command!::new(&sr, sh).arg(&sr, "-c").arg(&sr, "echo hidden").stdout(Stdio!::Null).spawn() {
                    Result::Ok(child) => {
                        var child = child;
                        let held = match &child.take_stdout() {
                            Option::Some(f) => { let closed = f.close(); "piped" },
                            Option::None => "null",
                        };
                        let ended = child.wait();
                        Buffer!::new().put(held)
                    },
                    Result::Err(e) => Buffer!::new(),
                });
                out = out.put(",").put(run(Command!::new(&sr, "true").env(&sr, "A=B", "x")));
                out = out.put(",").put(run(Command!::new(&sr, "true").current_dir(&sr, "no-such-directory")));
                // Into a pipe and out of another: the child reads to the end once this process closes its end.
                match Command!::new(&sr, "tr").arg(&sr, "a-z").arg(&sr, "A-Z").stdin(Stdio!::Pipe).stdout(Stdio!::Pipe).spawn() {
                    Result::Ok(child) => {
                        var child = child;
                        match &child.take_stdin() {
                            Option::Some(w) => {
                                let wrote = w.output().put("piped").finish();
                            },
                            Option::None => (),
                        }
                        var piped = Buffer!::new();
                        match &child.take_stdout() {
                            Option::Some(r) => {
                                var r = r.input();
                                let n = &r.read_exact(&piped, 100usize);
                                match r.finish() {
                                    Result::Ok(rest) => rest.destroy(),
                                    Result::Err(e) => (),
                                }
                            },
                            Option::None => (),
                        }
                        let ended = child.wait();
                        out = out.put(",").put(piped);
                    },
                    Result::Err(e) => (),
                }
                print(out);
                0
            }''', stdout="set|e,ONLY=1\n|,here|,from a file|,|,null,error 22,error 2,PIPED")

    def test_children_are_killed_and_signalled(self):
        self.expect('''
            use std::io::print;
            use std::option::Result;
            use std::process::{Command!, Exit, Polled!};
            use std::str::StaticRead;
            fn ended(result: Result[Exit, std::io::Error]) -> i32 {
                match result {
                    Result::Ok(exit) => match exit { Exit::Code(c) => c, Exit::Signal(s) => 100i32 + s },
                    Result::Err(e) => 0i32 - e.errno,
                }
            }
            fn main() -> i32 {
                var sr = StaticRead::new();
                let killed = match Command!::new(&sr, "sleep").arg(&sr, "30").spawn() {
                    Result::Ok(child) => {
                        var child = child;
                        let sent = (&child.kill()).is_ok();
                        let pid = &child.id() > 0i64;
                        if sent && pid { ended(child.wait()) } else { ended(child.wait()) - 1000i32 }
                    },
                    Result::Err(e) => 0i32,
                };
                let terminated = match Command!::new(&sr, "sleep").arg(&sr, "30").spawn() {
                    Result::Ok(child) => {
                        var child = child;
                        let sent = &child.signal(15i32);
                        let bad = match &child.signal(99999i32) { Result::Err(e) => e.errno, Result::Ok(u) => 0i32 };
                        let code = ended(child.wait());
                        if sent.is_ok() && bad == 22i32 { code } else { 0i32 }
                    },
                    Result::Err(e) => 0i32,
                };
                print(f"{killed} {terminated}");
                0
            }''', stdout="109 115")

    def test_errors_describe_themselves(self):
        self.expect('''
            use std::io::{Error, ErrorKind, File!, print};
            use std::option::Result;
            use std::str::StaticRead;
            fn found(e: Error) -> bool { match e.kind() { ErrorKind::NotFound => true, _ => false } }
            fn denied(e: Error) -> bool { match e.kind() { ErrorKind::PermissionDenied => true, _ => false } }
            fn present(e: Error) -> bool { match e.kind() { ErrorKind::AlreadyExists => true, _ => false } }
            fn other(e: Error) -> bool { match e.kind() { ErrorKind::Other => true, _ => false } }
            fn main() -> i32 {
                var sr = StaticRead::new();
                match File!::open_read(&sr, "no-such-file") {
                    Result::Ok(f) => { let closed = f.close(); 1 },
                    Result::Err(e) => {
                        let kinds = found(e) && denied(Error.{ errno: 13i32 }) && present(Error.{ errno: 17i32 })
                            && other(Error.{ errno: 9999i32 });
                        var message = e.message();
                        let n = &message.len();
                        print(f"{e}; {n} {kinds}");
                        message.destroy();
                        0
                    },
                }
            }''', stdout="No such file or directory (error 2); 25 true")

    # ------------------------------------------------------------------ time --

    def test_sleep_waits_for_its_duration(self):
        self.expect('''
            use std::time::{Duration, Instant, sleep};
            fn main() -> i32 {
                let start = Instant::now();
                sleep(Duration::from_millis(20u64));
                let slept = start.elapsed();
                let d = Duration::from_secs(18446744073709551615u64);
                if slept.as_millis() >= 20u64 && Duration::from_nanos(1500000000u64).as_secs() == 1u64
                    && d.as_nanos() == 18446744073709551615u64 { 0 } else { 1 }
            }''')

    def test_durations_and_instants_add_subtract_and_compare(self):
        self.expect('''
            use std::time::{Duration, Instant};
            use std::cmp::{Ord, max, min};
            use std::hash::Eq;
            fn main() -> i32 {
                let second = Duration::from_secs(1u64);
                let half = Duration::from_millis(500u64);
                let most = Duration::from_nanos(18446744073709551615u64);
                let sums = second.add(half).as_millis() == 1500u64 && second.sub(half).as_millis() == 500u64
                    && half.sub(second).is_zero() && half.mul(3u64).as_millis() == 1500u64
                    && most.add(second).as_nanos() == 18446744073709551615u64 && most.mul(2u64).as_nanos() == 18446744073709551615u64;
                let (a, b, order) = Ord[Duration, Duration]::cmp(half, second);
                let (c, d, same) = Eq[Duration, Duration]::eq(half, Duration::from_nanos(500000000u64));
                let compared = order.is_lt() && same && max(half, second).as_secs() == 1u64 && min(half, second).as_millis() == 500u64;
                let start = Instant::now();
                let later = start.add(second);
                let (x, y, before) = Ord[Instant, Instant]::cmp(start, later);
                let instants = later.since(start).as_secs() == 1u64 && start.since(later).is_zero() && before.is_lt()
                    && start.elapsed().as_secs() < 60u64;
                if sums && compared && instants { 0 } else { 1 }
            }''')

    def test_instant_reads_the_monotonic_clock_through_the_c_library(self):
        # `Instant::now` reads CLOCK_MONOTONIC through time.c, which names the host's clock, so no clock
        # number (Linux's 1, FreeBSD's 4, macOS's 6) is written in Kursal: the time between two readings
        # around a sleep is the sleep's at least, and no more than the whole run's.
        started = time.monotonic_ns()
        result = self.run_program('''
            use std::time::{Duration, Instant, sleep};
            use std::io::print;
            fn main() -> i32 {
                let start = Instant::now();
                sleep(Duration::from_millis(30u64));
                let elapsed = Instant::now().since(start).as_nanos();
                let printed = print(f"{elapsed}");
                0
            }''')
        whole = time.monotonic_ns() - started
        self.assertEqual(result.returncode, 0, result.stderr[:500])
        self.assertTrue(30_000_000 <= int(result.stdout) <= whole, (result.stdout, whole))
        self.assertFalse("clock_gettime" in (LIB / "std/time.kur").read_text(), "std::time names a clock itself")

    def test_available_parallelism_counts_the_processors_of_the_affinity_mask(self):
        # Allowed to run on one processor (`taskset -c 0`), the process can run one thread at a time.
        def one_processor() -> None:
            os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
        self.expect('''
            use std::process::available_parallelism;
            fn main() -> i32 {
                available_parallelism() as i32
            }''', status=1, preexec=one_processor)

    # -------------------------------------------------------------------- io --

    def test_reading_a_directory_is_an_error(self):
        self.expect('''
            use std::option::Result;
            use std::io::File!;
            use std::str::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                match File!::open_read(&sr, ".") {
                    Result::Err(code) => 1,
                    Result::Ok(file) => match file.input().read_to_end() {
                        Result::Ok(contents) => { contents.destroy(); 2 },
                        Result::Err(e) => if e.errno == 21i32 { 0 } else { 3 },
                    },
                }
            }''')

    def test_files_that_are_not_regular_are_read_to_their_end(self):
        # A pipe (/dev/stdin fed by one) and a device (/dev/null) have no size to read by: `fs::read` and
        # `Input!::read_to_end` read them until they end. A regular file is read whole, a directory is
        # EISDIR.
        text = "line\n" * 20000
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::fs::{read, write};
            use std::io::{Error, File!, print};
            use std::option::Result;
            use std::str::StaticRead;
            fn length(r: Result[Buffer!, Error]) -> i32 {
                match r {
                    Result::Ok(b) => { var b = b; let n = &b.len(); b.destroy(); n as i32 },
                    Result::Err(e) => 0i32 - e.errno,
                }
            }
            fn main() -> i32 {
                var sr = StaticRead::new();
                let none = length(read(&sr, "/dev/null"));
                let directory = length(read(&sr, "."));
                let written = write(&sr, "file.txt", Buffer!::new().put("12345")).is_ok();
                let regular = length(read(&sr, "file.txt"));
                let piped = match File!::open_read(&sr, "/dev/stdin") {
                    Result::Ok(file) => length(file.input().read_to_end()),
                    Result::Err(e) => 0i32 - e.errno,
                };
                let printed = print(f"{none} {directory} {regular} {piped}");
                if written { 0 } else { 1 }
            }''', stdout=f"0 -21 5 {len(text)}", stdin=text)

    def test_path_exists_tells_absence_from_failure(self):
        # A missing name and a path through a file are absent; a directory that cannot be searched says
        # nothing, and is the error (EACCES), unless permissions do not bind this user.
        with tempfile.TemporaryDirectory(prefix="kursal-exists-") as base:
            locked = Path(base) / "locked"
            locked.mkdir()
            (Path(base) / "file").write_text("")
            locked.chmod(0o000)
            try:
                self.expect(f'''
                    use std::fs::path_exists;
                    use std::option::Result;
                    use std::str::StaticRead;
                    fn main() -> i32 {{
                        var sr = StaticRead::new();
                        let found = path_exists(&sr, "{base}/file");
                        let missing = path_exists(&sr, "{base}/missing");
                        let through_a_file = path_exists(&sr, "{base}/file/inner");
                        if !found.unwrap_or(false) || missing.unwrap_or(true) || through_a_file.unwrap_or(true) {{
                            return 1;
                        }}
                        match path_exists(&sr, "{base}/locked/inner") {{
                            Result::Ok(found) => 0,
                            Result::Err(e) => e.errno,
                        }}
                    }}''', status=0 if os.geteuid() == 0 else 13)
            finally:
                locked.chmod(0o755)

    def test_input_reads_a_file(self):
        # An `Input!` over a file: `read` and `read_exact` hand out the bytes `read_line` read ahead before
        # they read more, and `read_to_end` returns the rest and closes the file; `finish` closes it and
        # hands back what was read ahead and not returned. At the end `read_line` is `None` and `read` 0. A
        # directory is EISDIR, and the buffer stays as it was.
        self.expect_program(*IO_PROGRAMS["input over a file"])

    def test_input_reads_a_file_to_its_end_without_reading_ahead(self):
        # `read_to_end` of a file sizes its buffer by the file, reads it with one `read(2)` and finds the end
        # with another, and never makes the 64 KiB buffer `read_line` reads ahead into.
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::fs::write;
            use std::io::File!;
            use std::option::Result;
            use std::process::trap;
            use std::str::StaticRead;
            extern "C" fn probe_allocated() -> u64;
            extern "C" fn probe_reads() -> u64;
            fn main() -> i32 {
                var sr = StaticRead::new();
                var text = Buffer!::new();
                var i = 0usize;
                while i < 20000usize {
                    text = text.write_byte(b'a');
                    i = i + 1usize;
                }
                let made = write(&sr, "big.txt", text);
                let allocated = unsafe probe_allocated();
                let reads = unsafe probe_reads();
                let input = match File!::open_read(&sr, "big.txt") { Result::Ok(f) => f.input(), Result::Err(e) => trap(), };
                let n = match input.read_to_end() {
                    Result::Ok(all) => { var all = all; let n = &all.len(); all.destroy(); n },
                    Result::Err(e) => 0usize,
                };
                let allocated = unsafe probe_allocated() - allocated;
                let reads = unsafe probe_reads() - reads;
                if n != 20000usize { 1 } else if allocated >= 65536u64 { 2 } else if reads != 2u64 { 3 } else { 0 }
            }''', counted=True)

    def test_output_writes_a_file(self):
        # An `Output!` over a file: what is put and written, and the bytes of a large buffer, written where
        # they lie, all in order; `finish` closes the file. A failed write is the error `finish` reports,
        # for a small write gathered and for a large one alike.
        self.expect_program(*IO_PROGRAMS["output over a file"])

    def test_print_and_eprint_write_to_stdout_and_stderr(self):
        result = self.run_program('''
            use std::arena::Domain!;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::io::{eprint, stderr, print, stdout};
            use std::str::Str;
            use std::str::StaticRead;
            fn main() {
                var read = StaticRead::new();
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                var cx = cx;
                print("a");
                stdout().write(&read, "b").finish();
                print(Buffer!::new().put("c"));
                stdout().write(&cx, Str::from_static(&cx, "d")).finish();
                eprint("A");
                stderr().write(&read, "B").finish();
                eprint(Buffer!::new().put("C"));
                stderr().write(&cx, Str::from_static(&cx, "D")).finish();
                cx.destroy();
            }''')
        self.assertEqual((result.stdout, result.stderr, result.returncode), ("abcd", "ABCD", 0))

    def test_print_and_put_take_every_printable_type(self):
        # Each instance of `Printable`, printed to stdout by `print` and put into a buffer by `put`.
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::io::{Error, File!, print};
            use std::option::Result;
            use std::str::StaticRead;
            fn error() -> Error {
                var sr = StaticRead::new();
                match File!::open_read(&sr, "/nonexistent/kursal") {
                    Result::Ok(f) => {
                        let closed = f.close();
                        Error::io()
                    },
                    Result::Err(e) => e,
                }
            }
            fn main() {
                print("str ");
                print(1u8); print(" "); print(2u16); print(" "); print(3u32); print(" "); print(4u64); print(" ");
                print(5usize); print(" "); print(-6i8); print(" "); print(-7i16); print(" "); print(-8i32); print(" ");
                print(-9i64); print(" "); print(-10isize); print(" "); print(0.1f32); print(" "); print(2.5f64); print(" ");
                print(true); print(" "); print(Buffer!::new().put("buf")); print(" "); print(error()); print("\n");
                print(Buffer!::new().put("str ").put(1u8).put(" ").put(2u16).put(" ").put(3u32).put(" ").put(4u64).put(" ")
                    .put(5usize).put(" ").put(-6i8).put(" ").put(-7i16).put(" ").put(-8i32).put(" ").put(-9i64).put(" ")
                    .put(-10isize).put(" ").put(0.1f32).put(" ").put(2.5f64).put(" ").put(false).put(" ")
                    .put(Buffer!::new().put("buf")).put(" ").put(error()).put("\n"));
            }''', stdout="str 1 2 3 4 5 -6 -7 -8 -9 -10 0.1 2.5 true buf No such file or directory (error 2)\n"
                         "str 1 2 3 4 5 -6 -7 -8 -9 -10 0.1 2.5 false buf No such file or directory (error 2)\n")

    def test_every_writer_takes_put_and_write(self):
        # `put` and `write`, the methods of every writer, on each writer of the library.
        self.expect('''
            use std::arena::Domain!;
            use std::buffer::Buffer!;
            use std::fmt::{CountingWriter, FixedWriter!, ReadingWriter!, Writer};
            use std::io::{print, stdout};
            use std::option::Result;
            use std::str::Str;
            use std::str::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                var cx = cx;
                let word = Str::from_static(&cx, "text");
                let printed = stdout().put("output ").put(1u8).put(" ").write(&cx, word).put("\n").finish();
                print(Buffer!::new().put("buffer ").put(2u16).put(" ").write(&sr, "static").put("\n"));
                let (fixed, status) = FixedWriter!::new(9usize).put("fixed ").put(345u32).write(&sr, "dropped").finish();
                let full = match status {
                    Result::Ok(nothing) => false,
                    Result::Err(e) => true,
                };
                print(fixed.put("\n"));
                let { count } = CountingWriter::new().put("count ").put(-12i32).write(&sr, "xyz");
                let (out, cx) = ReadingWriter!::new(Buffer!::new(), cx).put("reading ").put(true).put(" ").write(&sr, "x").finish();
                var cx = cx;
                print(out.put(" ").write(&cx, word).put("\n"));
                cx.destroy();
                if printed.is_ok() && full && count == 12usize { 0 } else { 1 }
            }''', stdout="output 1 text\nbuffer 2 static\nfixed 345\nreading true x text\n")

    def test_a_static_placeholder_is_written_as_text_is(self):
        # No `Format` instance takes a `Str['static]`: an f-string writes one with `write_static`, as it writes
        # its literal text, here into writers a generic function knows only by `Writer`, and lent.
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::{Format, Writer};
            use std::io::print;
            use std::str::Str;
            forall W where Writer[W].
            fn label(out: W, name: Str['static]) -> W {
                let open = "<";
                f"{open}{name}>".write(out)
            }
            forall T, W where Format[T, W], Writer[W], Copy[T].
            fn twice(out: W, x: T) -> W {
                let sep: Str['static] = ", ";
                f" {x}{sep}{x}".write(out)
            }
            fn main() {
                var lent = " lent";
                let out = twice(label(Buffer!::new(), "tag"), 7u32);
                print(f"{&lent}\n".write(out));
            }''', stdout="<tag> 7, 7 lent\n")

    def test_static_text_has_no_format_instance(self):
        self.reject('''
            use std::buffer::Buffer!;
            use std::fmt::{Format, Writer};
            forall T, W where Format[T, W], Writer[W], Copy[T].
            fn show(out: W, x: T) -> W {
                f"{x}".write(out)
            }
            fn main() {
                show(Buffer!::new(), "text").destroy();
            }''', r"Format\[Str\['static\], Buffer!\]")

    def test_branded_text_formats_through_the_lent_context(self):
        # `Format[Str['r], ReadingWriter![W, C]]`: text reads through the capability `.write(&cx, out)`
        # lends, an open buffer's `Exclusive!` or a domain, whether it views a byte slice or a literal.
        self.expect('''
            use std::arena::Domain!;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::io::print;
            use std::str::Str;
            fn main() {
                var exists 'b. (tag, bytes, restore) = Buffer!::new().put(42u32).open();
                let digits = bytes.as_str();
                var text = f"[{digits}]".write(&tag, Buffer!::new());
                restore.restore(tag).destroy();
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                var { tag: t, arena } = cx;
                let zs = (&arena.alloc_slice(&t, 3usize, b'z')).as_str();
                var cx = Domain!.{ tag: t, arena };
                let planted = Str::from_static(&cx, "planted");
                let line = f"{&text} {zs} {planted}\n".write(&cx, Buffer!::new());
                text.destroy();
                print(line);
                cx.destroy();
            }''', stdout="[42] zzz planted\n")

    def test_print_reports_a_failed_write(self):
        with open("/dev/full", "w") as full:
            result = self.run_program('''
                use std::io::print;
                use std::option::Result;
                fn main() -> i32 {
                    match print("lost") {
                        Result::Ok(nothing) => 1,
                        Result::Err(e) => e.errno,
                    }
                }''', stdout=full)
        self.assertEqual(result.returncode, 28)

    def test_print_reports_a_closed_pipe(self):
        # The pipe's reader is gone before the program starts: each write fails with EPIPE (32), which
        # `print` returns, rather than SIGPIPE ending the process. A child started meanwhile gets SIGPIPE's
        # default action back, so a shell that sends itself one ends by it (status 3 if it does not).
        reader, writer = os.pipe()
        os.close(reader)
        with os.fdopen(writer, "w") as pipe:
            result = self.run_program('''
                use std::io::print;
                use std::option::Result;
                use std::process::{Command!, Exit};
                use std::str::StaticRead;
                fn main() -> i32 {
                    var sr = StaticRead::new();
                    let child = match Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, "kill -s PIPE $$; exit 3").run() {
                        Result::Ok(exit) => match exit {
                            Exit::Signal(s) => s,
                            Exit::Code(c) => 100 + c,
                        },
                        Result::Err(e) => 200,
                    };
                    if child != 13 {
                        return child;
                    }
                    var i = 0u32;
                    while i < 3u32 {
                        match print("lost") {
                            Result::Ok(nothing) => (),
                            Result::Err(e) => return e.errno,
                        }
                        i = i + 1u32;
                    }
                    1
                }''', stdout=pipe)
        self.assertEqual(result.returncode, 32)

    def test_standard_streams_the_process_started_without_stay_out_of_reach(self):
        # Started with descriptors 0, 1 and 2 closed, the program still has them, on /dev/null, so the file
        # it creates takes another number: `print`, `eprint` and `stdin` work and reach /dev/null, and the
        # file holds only what was written to it.
        def close_standard_streams() -> None:
            for fd in (0, 1, 2):
                os.close(fd)
        self.expect('''
            use std::fmt::Writer;
            use std::io::{File!, eprint, print, stdin};
            use std::option::{Option, Result};
            use std::str::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                let out = match File!::create(&sr, "owned.txt") {
                    Result::Ok(f) => f.output(),
                    Result::Err(e) => return 1,
                };
                let out = out.put("the file's own\\n");
                let printed = print("stdout\\n").is_ok() && eprint("stderr\\n").is_ok();
                let line = match stdin() {
                    Result::Ok(input) => {
                        var input = input;
                        let line = match &input.read_line() {
                            Result::Ok(found) => match found {
                                Option::Some(text) => { text.destroy(); 4 },
                                Option::None => 0,
                            },
                            Result::Err(e) => 5,
                        };
                        match input.finish() {
                            Result::Ok(rest) => { rest.destroy(); line },
                            Result::Err(e) => 5,
                        }
                    },
                    Result::Err(e) => 5,
                };
                let written = out.finish().is_ok();
                let reader = match File!::open_read(&sr, "owned.txt") {
                    Result::Ok(f) => f.input(),
                    Result::Err(e) => return 6,
                };
                let length = match reader.read_to_end() {
                    Result::Ok(text) => { var text = text; let n = &text.len(); text.destroy(); n },
                    Result::Err(e) => 0usize,
                };
                if !written { 2 } else if !printed { 3 } else if line != 0 { line } else if length != 15usize { 7 } else { 0 }
            }''', preexec=close_standard_streams)

    def test_read_line_tells_an_empty_line_from_the_end(self):
        self.expect('''
            use std::io::{print, stdin};
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::option::{Option, Result};
            fn main() -> i32 {
                var input = match stdin() { Result::Ok(i) => i, Result::Err(e) => return 8, };
                var lines = 0i32;
                var reading = true;
                while reading {
                    match &input.read_line() {
                        Result::Err(e) => {
                            lines = 9i32;
                            reading = false;
                        },
                        Result::Ok(line) => match line {
                            Option::None => {
                                reading = false;
                            },
                            Option::Some(text) => {
                                lines = lines + 1i32;
                                print(Buffer!::new().write_byte(b'[').put(text).write_byte(b']'));
                            },
                        },
                    }
                }
                match input.finish() {
                    Result::Ok(rest) => rest.destroy(),
                    Result::Err(e) => return 8,
                }
                lines
            }''', status=3, stdout="[a][][b]", stdin="a\n\nb")

    def test_input_reads_lines_ahead_and_loses_no_byte(self):
        # An `Input!` over standard input returns lines, and `read` and `read_exact` hand out what it read
        # ahead first; while it holds standard input, another `stdin` fails with EBUSY (16) rather than
        # skip what it read ahead; `finish` hands back what was read ahead and not returned, and standard
        # input with it, as `read_to_end` gives it back with the rest.
        self.expect_program(*IO_PROGRAMS["input over stdin"])

    def test_input_reads_many_lines_in_few_reads(self):
        # 20000 lines, 200 KB, take a few reads of 64 KiB, not one per byte; a line longer than the buffer
        # comes whole.
        lines = [f"line {i}" for i in range(20000)] + ["x" * 100000, "end"]
        self.expect('''
            use std::io::{Input!, stdin};
            use std::option::{Option, Result};
            extern "C" fn probe_reads() -> u64;
            fn main() -> i32 {
                var input = match stdin() { Result::Ok(i) => i, Result::Err(e) => return 1, };
                var lines = 0u64;
                var longest = 0usize;
                var reading = true;
                while reading {
                    match &input.read_line() {
                        Result::Ok(found) => match found {
                            Option::Some(text) => {
                                var text = text;
                                let n = &text.len();
                                if n > longest { longest = n; }
                                text.destroy();
                                lines = lines + 1u64;
                            },
                            Option::None => { reading = false; },
                        },
                        Result::Err(e) => { reading = false; },
                    }
                }
                match input.finish() {
                    Result::Ok(rest) => rest.destroy(),
                    Result::Err(e) => return 1,
                }
                let reads = unsafe probe_reads();
                if lines != 20002u64 || longest != 100000usize { 2 } else if reads > 64u64 { 3 } else { 0 }
            }''', stdin="\n".join(lines), counted=True)

    def test_a_read_error_is_not_the_end_of_the_input(self):
        directory = os.open(".", os.O_RDONLY)
        try:
            result = self.run_program('''
                use std::io::stdin;
                use std::option::Result;
                fn main() -> i32 {
                    match stdin() {
                        Result::Ok(input) => match input.read_to_end() {
                            Result::Ok(text) => { text.destroy(); 1 },
                            Result::Err(e) => e.errno,
                        },
                        Result::Err(e) => 1,
                    }
                }''', stdin_fd=directory)
        finally:
            os.close(directory)
        self.assertEqual(result.returncode, 21)

    # -------------------------------------------------- buffers, text, maps --

    def test_a_zero_capacity_buffer_grows(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            fn main() -> i32 {
                var b = Buffer!::with_capacity(0usize).write_byte(b'a').put("bc");
                let n = &b.len();
                b.destroy();
                var z = Buffer!::new_zeroed(0usize);
                var c = &z.clone();
                var c = c.write_byte(b'x');
                let m = &c.len();
                z.destroy();
                c.destroy();
                if n == 3usize && m == 1usize { 0 } else { 1 }
            }''')

    def test_split_on_an_empty_separator_is_the_whole_text(self):
        self.expect('''
            use std::arena::Domain!;
            use std::collections::arena_vec::ArenaVec;
            use std::str::Str;
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let text = Str::from_static(&cx, "a,b");
                let whole = ArenaVec::split(&cx, text, Str::from_static(&cx, ""));
                let parts = ArenaVec::split(&cx, text, Str::from_static(&cx, ","));
                let status = if whole.len(&cx) == 1usize && whole.at(&cx, 0usize).len() == 3usize
                    && parts.len(&cx) == 2usize { 0 } else { 1 };
                cx.destroy();
                status
            }''')

    def test_arena_map_grows_to_any_size_and_stays_small_under_churn(self):
        self.expect('''
            use std::arena::Domain!;
            use std::collections::arena_map::ArenaMap;
            use std::option::Option;
            fn is(o: Option[u32], v: u32) -> bool {
                match o { Option::Some(x) => x == v, Option::None => false }
            }
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let map: ArenaMap[u32, u32, 'k] = ArenaMap::new(&cx);
                map.grow(&cx, 3usize);
                map.insert(&cx, 1u32, 10u32);
                map.grow(&cx, 0usize);
                map.insert(&cx, 2u32, 20u32);
                map.grow(&cx, 1000usize);
                var i = 0u32;
                while i < 100000u32 {
                    map.insert(&cx, 100u32 + i % 7u32, i);
                    map.remove(&cx, 100u32 + (i + 3u32) % 7u32);
                    i = i + 1u32;
                }
                var bad = 0i32;
                if !is(map.get(&cx, 1u32), 10u32) || !is(map.get(&cx, 2u32), 20u32) {
                    bad = 1i32;
                }
                // 100000 keys came and went; the dead ones were dropped as the map grew.
                if map.positions(&cx) > 1000usize || !is(map.key_at(&cx, 0usize), 1u32) {
                    bad = bad + 2i32;
                }
                cx.destroy();
                bad
            }''')

    def test_arena_map_churn_takes_no_more_of_the_arena(self):
        # A map of at most 8 keys, removed and inserted again a million times after a warm-up of a thousand:
        # the arena (whose chunks come from `malloc`) takes no more memory, where a new slot array at each
        # `grow` took some 35 MB.
        self.expect('''
            use std::arena::Domain!;
            use std::collections::arena_map::ArenaMap;
            extern "C" fn probe_allocated() -> u64;
            forall 'k.
            fn churn(&cx: Domain!['k], map: ArenaMap[u32, u32, 'k], from: u32, to: u32) {
                var i = from;
                while i < to {
                    map.insert(&cx, i % 8u32, i);
                    map.remove(&cx, (i + 3u32) % 8u32);
                    i = i + 1u32;
                }
            }
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                var cx = cx;
                let map: ArenaMap[u32, u32, 'k] = ArenaMap::new(&cx);
                churn(&cx, map, 0u32, 1000u32);
                let before = unsafe probe_allocated();
                churn(&cx, map, 1000u32, 1001000u32);
                let taken = unsafe probe_allocated() - before;
                let live = map.len(&cx);
                cx.destroy();
                if live > 8usize { 1 } else if taken != 0u64 { 2 } else { 0 }
            }''', counted=True)

    def test_hash_chains_of_colliding_keys(self):
        self.expect('''
            use std::collections::hashmap::OwnedMap!;
            use std::hash::{Hash, Eq, HashState};
            use std::option::Option;
            exposed struct Key { n: u32 }
            instance Hash[Key] {
                fn hash(self: Key, state: HashState) -> (Key, HashState) { (self, state) }
            }
            instance Eq[Key, Key] {
                fn eq(a: Key, b: Key) -> (Key, Key, bool) { (a, b, a.n == b.n) }
            }
            fn drop_entry(key: Key, value: u32) {}
            fn one(f: Option[(Key, u32)]) -> i32 {
                match f { Option::Some(kv) => 1i32, Option::None => 0i32 }
            }
            fn main() -> i32 {
                var map = OwnedMap!::new[Key, u32](std::process::hash_seed());
                var i = 0u32;
                while i < 3000u32 {
                    let old = &map.insert(Key.{ n: i }, i);
                    i = i + 1u32;
                }
                let replaced = match &map.insert(Key.{ n: 1500u32 }, 7u32) { Option::Some(kv) => kv.1 == 1500u32, Option::None => false };
                var k0 = Key.{ n: 0u32 };
                var k1 = Key.{ n: 1000u32 };
                var k2 = Key.{ n: 2999u32 };
                var k3 = Key.{ n: 1500u32 };
                let first = &map.remove(&k0);
                let middle = &map.remove(&k1);
                let last = &map.remove(&k2);
                let again = &map.remove(&k1);
                let removed = one(first) + one(middle) + one(last) + one(again);
                let kept = (&map.get(&k3)).unwrap_or(0u32) == 7u32 && &map.at(&k3) == 7u32;
                let present = &map.contains(&k3) && !&map.contains(&k1);
                let n = &map.len();
                map.destroy_with(drop_entry);
                if replaced && removed == 3i32 && kept && present && n == 2997usize { 0 } else { 1 }
            }''')

    def test_long_lists_and_scans_take_no_stack(self):
        self.expect('''
            use std::arena::Domain!;
            use std::collections::list::List;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::cursor::Cursor;
            use std::option::Result;
            fn is_a(b: u8) -> bool { b == b'a' }
            forall 'k.
            fn lists(&cx: Domain!['k]) -> bool {
                var list: List[u32, 'k] = List::empty();
                var i = 0u32;
                while i < 1000000u32 {
                    list = list.cons(&cx, i);
                    i = i + 1u32;
                }
                let doubled = list.map(&cx, |&c, x: u32| x * 2u32);
                let evens = doubled.filter_map(&cx, |&c, x: u32| if x % 4u32 == 0u32 { std::option::Option::Some.(x) } else { std::option::Option::None });
                let both = doubled.append(&cx, evens);
                let total = both.try_fold(&cx, 0u64, |&c, acc: u64, x: u32| -> Result[u64, ()] { Result::Ok.(acc + (x as u64)) });
                let long = both.len(&cx);
                let first = doubled.first(&cx);
                let sums = match total { Result::Ok(t) => t == 999999000000u64 + 499999000000u64, Result::Err(e) => false };
                let head = match first { std::option::Option::Some(x) => x, std::option::Option::None => 0u32 };
                long == 1500000usize && sums && head == 1999998u32
            }
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let listed = lists(&cx);
                cx.destroy();
                var b = Buffer!::new();
                var j = 0usize;
                while j < 1000000usize {
                    b = b.write_byte(b'a');
                    j = j + 1usize;
                }
                var exists 't. (tag, bytes, restore) = b.open();
                var cur = Cursor::new(bytes.as_str());
                let run = &cur.take_while(&tag, is_a);
                let beyond = cur.peek_at(&tag, 18446744073709551615usize);
                restore.restore(tag).destroy();
                if listed && run.len() == 1000000usize && beyond.is_none() { 0 } else { 1 }
            }''')

    def test_chain_and_zip_stop_pulling_a_finished_side(self):
        self.expect('''
            use std::iter::{Iterator, zip};
            use std::option::Option;
            // Ends after three items, then would start again: a side that is pulled after it ended.
            exposed struct Flaky { n: u32 }
            instance Iterator[Flaky -> u32] {
                fn next(&it: Flaky) -> Option[u32] {
                    let n = it.n;
                    it = Flaky.{ n: n + 1u32 };
                    if n == 3u32 { Option::None } else { Option::Some.(n) }
                }
            }
            fn main() -> i32 {
                var chained = Flaky.{ n: 0u32 }.chain(Flaky.{ n: 10u32 });
                var sum = 0u32;
                var count = 0u32;
                while count < 6u32 {
                    match &chained.next() {
                        Option::Some(x) => { sum = sum + x; },
                        Option::None => (),
                    }
                    count = count + 1u32;
                }
                var zipped = zip(Flaky.{ n: 0u32 }, Flaky.{ n: 0u32 });
                var pairs = 0u32;
                count = 0u32;
                while count < 8u32 {
                    match &zipped.next() {
                        Option::Some(p) => { pairs = pairs + 1u32; },
                        Option::None => (),
                    }
                    count = count + 1u32;
                }
                if sum == 0u32 + 1u32 + 2u32 + 10u32 + 11u32 + 12u32 && pairs == 3u32 { 0 } else { 1 }
            }''')

    def test_take_and_skip_stop_pulling_a_finished_iterator(self):
        self.expect('''
            use std::iter::Iterator;
            use std::option::Option;
            // Ends after three items, then would start again: an iterator pulled after it ended.
            exposed struct Flaky { n: u32 }
            instance Iterator[Flaky -> u32] {
                fn next(&it: Flaky) -> Option[u32] {
                    let n = it.n;
                    it = Flaky.{ n: n + 1u32 };
                    if n == 3u32 { Option::None } else { Option::Some.(n) }
                }
            }
            fn main() -> i32 {
                var taken = Flaky.{ n: 0u32 }.take(10usize);
                var skipped_past = Flaky.{ n: 0u32 }.skip(5usize);
                var skipped = Flaky.{ n: 0u32 }.skip(1usize);
                var took = 0u32;
                var past = 0u32;
                var sum = 0u32;
                var count = 0u32;
                while count < 8u32 {
                    match &taken.next() {
                        Option::Some(x) => { took = took + 1u32; },
                        Option::None => (),
                    }
                    match &skipped_past.next() {
                        Option::Some(x) => { past = past + 1u32; },
                        Option::None => (),
                    }
                    match &skipped.next() {
                        Option::Some(x) => { sum = sum + x; },
                        Option::None => (),
                    }
                    count = count + 1u32;
                }
                if took == 3u32 && past == 0u32 && sum == 1u32 + 2u32 { 0 } else { 1 }
            }''')

    # ------------------------------------------------------- core accessors --

    ACCESSORS = '''
        use std::option::{Option, Result};
        use std::str::Str;
        use std::buffer::Buffer!;
        use std::fmt::Writer;
        fn main() -> i32 {
            let a = Option::Some.(3u32).unwrap();
            let r: Result[u32, u32] = Result::Ok.(4u32);
            let b = r.unwrap();
            var exists 'b. (tag, bytes, restore) = Buffer!::new().put("pq").open();
            let c = bytes.at(&tag, 0usize);
            let (p, n, m) = bytes.into_parts();
            let text = unsafe Str::from_raw_parts(&tag, p, n);
            let d = text.at(&tag, 1usize);
            let status = if a == 3u32 && b == 4u32 && c == b'p' && d == b'q' { MISS } else { 1 };
            restore.restore(tag).destroy();
            status
        }'''

    def test_aborting_accessors_return_what_is_there(self):
        self.expect(self.ACCESSORS.replace("MISS", "0"))

    def test_aborting_accessors_stop_on_a_miss(self):
        misses = ('{ let n: Option[u32] = Option::None; n.unwrap(); 0 }',
                  '{ let e: Result[u32, u32] = Result::Err.(1u32); e.unwrap(); 0 }',
                  '{ bytes.at(&tag, 2usize); 0 }',
                  '{ text.at(&tag, 2usize); 0 }')
        for miss in misses:
            with self.subTest(miss=miss):
                result = self.run_program(self.ACCESSORS.replace("MISS", miss))
                self.assertLess(result.returncode, 0)


    # ------------------------------------------------------------ core API --

    def test_core_helpers(self):
        self.expect('''
            use std::cmp::{Order, min, max};
            use std::hash::{Hash, Eq, HashState, Seed};
            use std::mem::{alignof, swap};
            use std::option::{Option, Result};
            use std::ptr::{null, is_null, offset};
            use std::str::{Str, StaticRead};
            use std::utf8::{encode, validate, width};
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::array::Array!;
            fn main() -> i32 {
                var a = 1u32;
                var b = 2u32;
                swap(&a, &b);
                let mem = a == 2u32 && b == 1u32 && alignof[u64]() == 8usize && alignof[u8]() == 1usize && alignof[()]() == 1usize;
                let order = min(3u32, 5u32) == 3u32 && max(3u32, 5u32) == 5u32 && Order::Less.reverse().is_gt()
                    && Order::Equal.reverse().is_eq() && !Order::Greater.is_eq();
                let r: Result[u32, u32] = Result::Err.(4u32);
                let k: Result[u32, u32] = Result::Ok.(6u32);
                let none: Option[u32] = Option::None;
                let results = r.unwrap_or(9u32) == 9u32 && r.unwrap_or_else(|e: u32| e + 1u32) == 5u32 && r.err().unwrap_or(0u32) == 4u32
                    && r.ok().is_none() && k.ok().unwrap_or(0u32) == 6u32 && k.err().is_none()
                    && none.unwrap_or_else(|| 8u32) == 8u32 && Option::Some.(2u32).unwrap_or_else(|| 8u32) == 2u32;
                let p = null[u32]();
                let pointers = is_null(p) && !is_null(unsafe offset(p, 1usize));
                let (t, h1) = Hash[(u32, u64)]::hash((1u32, 2u64), HashState::new(Seed::new(3u64, 4u64)));
                let (u, h2) = Hash[(u32, u64)]::hash((1u32, 2u64), HashState::new(Seed::new(3u64, 4u64)));
                let (x, y, same) = Eq[(u32, u64), (u32, u64)]::eq((1u32, 2u64), (1u32, 2u64));
                let (x2, y2, diff) = Eq[(u32, u32, u32), (u32, u32, u32)]::eq((1u32, 2u32, 3u32), (1u32, 2u32, 4u32));
                let tuples = same && !diff && h1.finish() == h2.finish();
                let good = Buffer!::new().write_byte(104u8).write_byte(195u8).write_byte(169u8);
                let bad = Buffer!::new().write_byte(237u8).write_byte(160u8).write_byte(128u8);
                var exists 'g. (gt, gs, gr) = good.open();
                var exists 'b. (bt, bs, br) = bad.open();
                var text = encode(encode(encode(Buffer!::new(), 233u32), 128512u32), 55296u32);
                let n = &text.len();
                text.destroy();
                let utf8 = validate(&gt, gs) && !validate(&bt, bs) && width(55296u32) == 0usize && width(128u32) == 2usize
                    && width(1114112u32) == 0usize && n == 2usize + 4usize + 3usize;
                gr.restore(gt).destroy();
                br.restore(bt).destroy();
                let empty: Str['static] = "";
                let slices = empty.is_empty() && !"a".is_empty();
                var store = Buffer!::new_zeroed(12usize);
                let base = unsafe (unsafe &store.as_ptr() as *u32);
                var array = unsafe Array!::from_parts(base, 3usize);
                let old = &array.set(1usize, 7u32);
                let missed = &array.set(3usize, 9u32);
                let arrays = &array.at(1usize) == 7u32 && (&array.get(3usize)).is_none() && &array.len() == 3usize
                    && missed.is_err() && old.is_ok();
                let (pb, pn) = array.into_parts();
                store.destroy();
                if mem && order && results && pointers && tuples && utf8 && slices && arrays { 0 } else { 1 }
            }''')

    def test_iterators_and_writers_take_a_context(self):
        self.expect('''
            use std::arena::Domain!;
            use std::collections::list::List;
            use std::fmt::ReadingWriter!;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::str::Str;
            use std::slice::Slice;
            forall 'k.
            fn run(cx: Domain!['k]) -> (Domain!['k], bool) {
                var cx = cx;
                var list: List[u32, 'k] = List::empty();
                list = list.cons(&cx, 3u32).cons(&cx, 2u32).cons(&cx, 1u32);
                var it = list.iter(cx);
                var sum = 0u32;
                for x in &it {
                    sum = sum * 10u32 + x;
                }
                var cx = it.finish();
                let name = Str::from_static(&cx, "world");
                let (text, cx) = f"hello {name}".write(ReadingWriter!::new(Buffer!::new(), cx)).finish();
                let exists 'b. (tag, bytes, restore) = text.open();
                var bit = bytes.iter(tag);
                var count = 0usize;
                for b in &bit {
                    count = count + 1usize;
                }
                restore.restore(bit.finish()).destroy();
                (cx, sum == 123u32 && count == 11usize)
            }
            fn main() -> i32 {
                let exists 'k. cx = Domain!::new(std::process::hash_seed());
                let (cx, ok) = run(cx);
                cx.destroy();
                if ok { 0 } else { 1 }
            }''')

    def test_cursor_reads_through_any_reader(self):
        self.expect('''
            use std::cursor::Cursor;
            use std::str::StaticRead;
            use std::ascii::is_alpha;
            fn main() -> i32 {
                var sr = StaticRead::new();
                let cur = Cursor::new("abc1");
                let (cur, word) = cur.take_matching(&sr, is_alpha);
                if word.len() == 3usize && cur.peek(&sr).unwrap_or(0u8) == b'1' && cur.starts_with(&sr, "1") { 0 } else { 1 }
            }''')

    def test_split_and_merge_round_trip(self):
        self.expect('''
            use std::owned::Owned!;
            fn main() -> i32 {
                var exists 'r. (alloc, region) = Owned!::new((10u32, 20u32)).open();
                let (restore, parts) = region.split();
                var region = restore.merge(parts);
                let pair = &region.replace((1u32, 2u32));
                let back = alloc.close(region).into_value();
                let fallible = match Owned!::try_new(5u32) {
                    std::option::Result::Ok(owned) => owned.into_value(),
                    std::option::Result::Err(failed) => 0u32,
                };
                if pair.0 == 10u32 && pair.1 == 20u32 && back.0 == 1u32 && back.1 == 2u32 && fallible == 5u32 { 0 } else { 1 }
            }''')

    def test_readers_of_a_shared_tree_use_slices_and_text(self):
        # A `Shared!` node, not the `Exclusive!`, reads a slice's element through `get_ref` and text's bytes
        # through its read methods; the writer comes back once the tree is joined.
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::str::Str;
            fn main() -> i32 {
                var exists 'b. (tag, bytes, restore) = Buffer!::new().put("  key=value \\n").open();
                let exists 'q. (root, back) = tag.share();
                var exists 'l, 'r. (left, right, join) = root.split();
                let second = match bytes.get_ref(2usize) {
                    std::option::Option::Some(at) => at.read(&left),
                    std::option::Option::None => 0u8,
                };
                let whole = bytes.as_str();
                let text = whole.trim(&left);
                let key = text.take(text.find_byte(&right, b'=', 0usize).unwrap_or(0usize));
                let trimmed = key.is(&left, "key") && text.ends_with(&right, whole.slice(6usize, 11usize))
                    && text.get(&left, 0usize).unwrap_or(0u8) == b'k' && text.rfind_byte(&right, b'e').unwrap_or(0usize) == 8usize
                    && text.cmp(&left, key).is_gt() && !text.eq(&right, key) && text.contains(&left, key);
                let searched = whole.trim(&right).is(&left, "key=value") && whole.trim_start(&left).len() == 11usize
                    && whole.trim_end(&right).len() == 11usize && whole.skip(2usize).starts_with(&left, whole.slice(2usize, 5usize))
                    && whole.find(&right, whole.slice(6usize, 8usize), 0usize).unwrap_or(0usize) == 6usize
                    && whole.rfind_byte(&left, b' ').unwrap_or(0usize) == 11usize && whole.byte_or_zero(&right, 99usize) == 0u8;
                var tag = back.resume(join.join(left, right));
                let written = match bytes.get_ref(0usize) {
                    std::option::Option::Some(at) => { at.set(&tag, b'#'); at.copy(&tag) },
                    std::option::Option::None => 0u8,
                };
                restore.restore(tag).destroy();
                if second == b'k' && trimmed && searched && written == b'#' { 0 } else { 1 }
            }''')

    def test_readers_of_a_shared_tree_cannot_write(self):
        self.reject('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            fn main() -> i32 {
                let exists 'b. (tag, bytes, restore) = Buffer!::new().put("ab").open();
                var exists 'q. (root, back) = tag.share();
                match bytes.get_ref(0usize) {
                    std::option::Option::Some(at) => at.set(&root, b'#'),
                    std::option::Option::None => (),
                }
                restore.restore(back.resume(root)).destroy();
                0
            }''', "no instance of Access")

    def test_text_of_the_same_bytes_hashes_alike(self):
        # Text over a buffer's bytes, a literal, and that literal planted in a domain hash alike.
        self.expect('''
            use std::arena::Domain!;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::hash::{HashIn, HashState, key_eq, key_hash};
            use std::cap::Exclusive!;
            use std::str::{Str, StaticRead};
            fn main() -> i32 {
                var exists 'b. (tag, bytes, restore) = Buffer!::new().put("name").open();
                let text = bytes.as_str();
                var sr = StaticRead::new();
                let literal: Str['static] = "name";
                let seed = std::process::hash_seed();
                var exists 'k. cx = Domain!::new(seed);
                let planted = Str::from_static(&cx, literal);
                let same = key_hash(&tag, text, seed) == key_hash(&sr, literal, seed) && key_hash(&cx, planted, seed) == key_hash(&sr, literal, seed)
                    && key_eq(&cx, planted, Str::from_static(&cx, "name")) && !key_eq(&cx, planted, Str::from_static(&cx, "nam"))
                    && HashIn[Str['b], Exclusive!['b]]::hash_in(&tag, text, HashState::new(seed)).finish() == key_hash(&tag, text, seed);
                cx.destroy();
                restore.restore(tag).destroy();
                if same { 0 } else { 1 }
            }''')

    def test_the_hash_is_siphash_1_3_of_the_encoding(self):
        """A key's hash is SipHash-1-3, under the seed, of the words its instance writes, little-endian: an
        integer one word, a byte string its length and then its bytes, the last word padded with zeros."""
        seed = (0x0706050403020100, 0x0f0e0d0c0b0a0908)
        # The reference vectors' key, and their first SipHash-1-3 output, of the empty message.
        self.assertEqual(siphash13(seed, []), 0xabac0158050fc4dc)
        cases = (("42u64", [42]), ("(1u32, true)", [1, 1]), ('"aBaBaB"', [6, int.from_bytes(b"aBaBaB", "little")]),
                 ('"0123456789abcdef!"', [17, *(int.from_bytes(w, "little") for w in (b"01234567", b"89abcdef", b"!"))]),
                 ('""', [0]))
        lines = "".join(f"                let h{i} = key_hash(&sr, {key}, seed);\n                out = f\"{{h{i}}}\\n\".write(out);\n"
                        for i, (key, _) in enumerate(cases))
        self.expect('''
            use std::hash::{Seed, key_hash};
            use std::str::StaticRead;
            use std::io::{Error, stdout};
            use std::fmt::Writer;
            use std::option::Result;
            fn main() -> Result[(), Error] {
                var sr = StaticRead::new();
                let seed = Seed::new(''' + f"{seed[0]}u64, {seed[1]}u64" + ''');
                var out = stdout();
''' + lines + '''                out.finish()
            }''', stdout="".join(f"{siphash13(seed, words)}\n" for _, words in cases))

    def test_keys_that_collide_under_a_multiply_add_hash_do_not_collide(self):
        """`aB` and `b!` make the same `acc * 33 + b`, and so do any two keys that swap one for the other: 4096
        keys of 12 such pairs once shared one hash. Under each seed they spread over the buckets as random keys
        do, so no bucket of 4096 holds more than a handful, and the three keys of the review are apart."""
        result = self.run_program('''
            use std::buffer::Buffer!;
            use std::hash::{Seed, key_hash};
            use std::str::StaticRead;
            use std::collections::vec::OwnedVec!;
            use std::io::{Error, stdout};
            use std::fmt::Writer;
            use std::option::Result;
            fn most_in_one_bucket(seed: Seed) -> u32 {
                var counts = OwnedVec!::new[u32]();
                var b = 0usize;
                while b < 4096usize {
                    &counts.push(0u32);
                    b = b + 1usize;
                }
                var most = 0u32;
                var i = 0usize;
                while i < 4096usize {
                    var key = Buffer!::new();
                    var bit = 0usize;
                    while bit < 12usize {
                        key = if (i >> bit) % 2usize == 0usize { key.put("aB") } else { key.put("b!") };
                        bit = bit + 1usize;
                    }
                    var exists 'k. (tag, bytes, restore) = key.open();
                    let bucket = (key_hash(&tag, bytes.as_str(), seed) % 4096u64) as usize;
                    restore.restore(tag).destroy();
                    let before = &counts.at(bucket);
                    let n = before + 1u32;
                    &counts.set(bucket, n);
                    if n > most {
                        most = n;
                    }
                    i = i + 1usize;
                }
                counts.destroy();
                most
            }
            fn apart(seed: Seed) -> bool {
                var sr = StaticRead::new();
                let a = key_hash(&sr, "aBaBaB", seed);
                let b = key_hash(&sr, "b!b!b!", seed);
                let c = key_hash(&sr, "aBb!aB", seed);
                a != b && b != c && a != c
            }
            fn main() -> Result[(), Error] {
                var out = stdout();
                let s0 = Seed::new(0u64, 0u64);
                let s1 = Seed::new(1u64, 2u64);
                let s2 = Seed::new(18446744073709551615u64, 12345u64);
                let s3 = std::process::hash_seed();
                out = f"{most_in_one_bucket(s0)} {most_in_one_bucket(s1)} {most_in_one_bucket(s2)} {most_in_one_bucket(s3)}\\n".write(out);
                let all_apart = if apart(s0) && apart(s1) && apart(s2) && apart(s3) { 1u32 } else { 0u32 };
                out = f"{all_apart}\\n".write(out);
                out.finish()
            }''')
        self.assertEqual(result.returncode, 0, result.stderr[:500])
        loads, apart = result.stdout.splitlines()
        self.assertEqual(apart, "1")
        for load in loads.split():
            self.assertLessEqual(int(load), 16, loads)

    def test_a_hosted_program_draws_its_seed_at_start(self):
        """`std::process::hash_seed` is the same throughout a process, and another process draws another: a
        string hashed twice in one run hashes alike, and differently in the next run."""
        source = '''
            use std::hash::key_hash;
            use std::process::hash_seed;
            use std::str::StaticRead;
            use std::io::{Error, stdout};
            use std::fmt::Writer;
            use std::option::Result;
            fn main() -> Result[(), Error] {
                var sr = StaticRead::new();
                let first = key_hash(&sr, "a fixed string", hash_seed());
                let second = key_hash(&sr, "a fixed string", hash_seed());
                var out = stdout();
                out = f"{first} {second}\\n".write(out);
                out.finish()
            }'''
        runs = []
        for _ in range(2):
            result = self.run_program(source)
            self.assertEqual(result.returncode, 0, result.stderr[:500])
            first, second = result.stdout.split()
            self.assertEqual(first, second)
            runs.append(first)
        self.assertNotEqual(runs[0], runs[1])

    def test_a_freestanding_map_needs_no_new_c_symbol(self):
        """C made for a freestanding target (`kursal c --entry`, not hosted) of a program with a `HashMap!`
        and an arena's `ArenaMap`, seeded by the program, calls only what `alloc` declares: no seed, no
        randomness, nothing of `lib/libc`."""
        source = '''
            use alloc::arena::Domain!;
            use alloc::collections::arena_map::ArenaMap;
            use alloc::collections::hashmap::OwnedMap!;
            use core::hash::Seed;
            use core::option::Option;
            fn main() -> i32 {
                let seed = Seed::new(1u64, 2u64);
                var m = OwnedMap!::new[u64, u64](seed);
                let old = &m.insert(7u64, 8u64);
                var k = 7u64;
                let seven = &m.at(&k);
                m.destroy();
                var exists 'k. cx = Domain!::new(seed);
                let a: ArenaMap[u32, u32, 'k] = ArenaMap::new(&cx);
                let none = a.insert(&cx, 1u32, 2u32);
                let two = a.at(&cx, 1u32);
                cx.destroy();
                (seven + (two as u64)) as i32
            }'''
        with tempfile.TemporaryDirectory(prefix="kursal-std-") as base:
            directory = Path(base)
            package(directory, source, ("core", "alloc"))
            resolver, sigs, program = lowered(directory)
            c_file, o_file = directory / "main.c", directory / "main.o"
            c_file.write_text(emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl)))
            built = subprocess.run([c_compiler(), *FLAGS, "-c", str(c_file), "-o", str(o_file)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr[:500])
            symbols = subprocess.run(["nm", "-u", str(o_file)], capture_output=True, text=True, check=True).stdout.split()
        undefined = {name for name in symbols if name != "U"}
        self.assertLessEqual(undefined, {"malloc", "realloc", "free", "abort", "memcpy", "memmove", "memset", "memcmp"})

    def test_c_strings_read_their_bytes_safely(self):
        self.expect('''
            use std::process::Args;
            use std::str::StaticRead;
            use std::option::Option;
            fn main(args: Args) -> i32 {
                var sr = StaticRead::new();
                match args.get(1usize) {
                    Option::Some(arg) => if arg.len() == 5usize && arg.bytes().is(&sr, "hello") && args.bytes(1usize).unwrap().is(&sr, "hello") { 0 } else { 1 },
                    Option::None => 2,
                }
            }''', args=("hello",))

    # ------------------------------------------------------ collections API --

    def test_vectors_sort_stably_and_edit_in_place(self):
        self.expect('''
            use std::collections::vec::OwnedVec!;
            use std::cmp::Order;
            fn by_first(a: (u32, u32), b: (u32, u32)) -> Order {
                if a.0 < b.0 { Order::Less } else if a.0 > b.0 { Order::Greater } else { Order::Equal }
            }
            fn main() -> i32 {
                var v = OwnedVec!::new[(u32, u32)]();
                var i = 0u32;
                while i < 40u32 {
                    &v.push((i % 3u32, i));
                    i = i + 1u32;
                }
                var none = ();
                &v.sort_by(&none, |&n, a: (u32, u32), b: (u32, u32)| by_first(a, b));
                var stable = true;
                var j = 1usize;
                while j < 40usize {
                    let a = &v.at(j - 1usize);
                    let b = &v.at(j);
                    stable = stable && (a.0 < b.0 || (a.0 == b.0 && a.1 < b.1));
                    j = j + 1usize;
                }
                &v.set(0usize, (9u32, 9u32));
                let set = &v.at(0usize).0 == 9u32 && &v.last().unwrap_or((0u32, 0u32)).1 == 38u32;
                &v.truncate(2usize);
                let truncated = &v.len() == 2usize && !&v.is_empty();
                &v.clear();
                let cleared = &v.is_empty();
                v.destroy();
                var w = OwnedVec!::new[u32]();
                &w.push(5u32);
                &w.push(1u32);
                &w.push(4u32);
                &w.sort();
                var u = OwnedVec!::new[u32]();
                &u.push(5u32);
                &u.push(1u32);
                &u.push(4u32);
                &u.sort_unstable();
                let sorted = &w.at(0usize) == 1u32 && &w.at(2usize) == 5u32 && &u.at(1usize) == 4u32;
                w.destroy();
                u.destroy();
                if stable && set && truncated && cleared && sorted { 0 } else { 1 }
            }''')

    def test_vector_at_stops_on_a_miss(self):
        result = self.run_program('''
            use std::collections::vec::OwnedVec!;
            fn main() -> i32 {
                var v = OwnedVec!::new[u32]();
                &v.push(1u32);
                let x = &v.at(1usize);
                v.destroy();
                0
            }''')
        self.assertLess(result.returncode, 0)

    def test_owned_maps_look_up_by_place(self):
        self.expect('''
            use std::collections::hashmap::{HashMap!, OwnedMap!};
            use std::option::Option;
            // `HashMap::find`, with a probe of the key type.
            fn find(&named: OwnedMap![u32, u32], &probe: u32) -> bool {
                var exists 'm. (tag, map) = named.inner;
                let found = match &map.find(&tag, &probe) { Option::Some(r) => r.copy(&tag) == 11u32, Option::None => false };
                named = OwnedMap!.{ inner: pack 'm. (tag, map) };
                found
            }
            fn main() -> i32 {
                var m = OwnedMap!::new[u32, u32](std::process::hash_seed());
                var one = 1u32;
                var two = 2u32;
                let none = &m.insert(1u32, 10u32);
                let replaced = match &m.insert(1u32, 11u32) { Option::Some(kv) => kv.1 == 10u32, Option::None => false };
                let looked = &m.contains(&one) && !&m.contains(&two) && &m.at(&one) == 11u32 && (&m.get(&two)).is_none();
                let found = find(&m, &one) && !find(&m, &two);
                let with = (&m.with(&one, |v: u32| (v + 1u32, v))).unwrap_or(0u32) == 11u32 && &m.at(&one) == 12u32;
                let removed = match &m.remove(&one) { Option::Some(kv) => kv.1 == 12u32, Option::None => false };
                let empty = &m.is_empty();
                let none2 = &m.insert(3u32, 30u32);
                &m.clear();
                let cleared = &m.is_empty() && &m.len() == 0usize;
                m.destroy();
                if replaced && looked && found && with && removed && empty && cleared { 0 } else { 1 }
            }''')

    def test_owned_maps_are_visited_cleared_and_searched_by_a_probe(self):
        # Counting the words of a text in a map keyed by `Buffer!`: a word seen before is found through a
        # view of the text, with no key built for it; the map is visited in place, twice, and cleared,
        # keeping its room.
        self.expect('''
            use std::collections::hashmap::OwnedMap!;
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::cap::Exclusive!;
            use std::slice::Readable;
            use std::str::Str;
            use std::ascii::word_at;
            use std::option::Option;
            forall 'b.
            fn count(&m: OwnedMap![Buffer!, u64], &tag: Exclusive!['b], word: Str['b]) {
                var probe = Readable.{ cap: tag, bytes: word };
                let bumped = &m.find_update(&probe, |&n| { n = n + 1u64; });
                let { cap, bytes } = probe;
                tag = cap;
                if bumped.is_none() {
                    match &m.insert(Buffer!::new().write(&tag, word), 1u64) {
                        Option::Some(old) => { let (k, v) = old; k.destroy(); },
                        Option::None => (),
                    }
                }
            }
            fn drop_entry(k: Buffer!, v: u64) { k.destroy(); }
            fn main() -> i32 {
                var m = OwnedMap!::new[Buffer!, u64](std::process::hash_seed());
                var exists 'b. (tag, bytes, restore) = Buffer!::new().put("to be or not to be again").open();
                let text = bytes.as_str();
                var tag = tag;
                var at = 0usize;
                var words = 0usize;
                while words < 6usize {
                    match word_at(&tag, text, at) {
                        Option::Some(found) => {
                            var (word, next) = found;
                            count(&m, &tag, word);
                            at = next;
                        },
                        Option::None => (),
                    }
                    words = words + 1usize;
                }
                var probe = Readable.{ cap: tag, bytes: text.take(2usize) };
                let to = (&m.find(&probe)).unwrap_or(0u64);
                let doubled = (&m.find_with(&probe, |n: u64| (n * 2u64, n))).unwrap_or(0u64);
                var total = (0u64, 0usize);
                &m.each_in(&total, |&word, &n, &acc| {
                    let (sum, letters) = acc;
                    acc = (sum + n, letters + &word.len());
                });
                &m.each(|&word, &n| { n = n + 100u64; });
                var shown = Buffer!::new();
                &m.each_in(&shown, |&word, &n, &out| {
                    if n > 103u64 {
                        out = out.put(&word.clone()).put("=").put(n);
                    }
                });
                let before = &m.len();
                &m.clear_with(drop_entry);
                let cleared = &m.is_empty() && (&m.find(&probe)).is_none();
                let { cap, bytes } = probe;
                var tag = cap;
                count(&m, &tag, text.skip(19usize));
                let after = &m.len();
                restore.restore(tag).destroy();
                m.destroy_with(drop_entry);
                let (sum, letters) = total;
                var exists 's. (stag, sbytes, srestore) = shown.open();
                let one = sbytes.as_str().is(&stag, "to=104");
                srestore.restore(stag).destroy();
                if to == 2u64 && doubled == 2u64 && sum == 8u64 && letters == 9usize && one && before == 4usize && cleared && after == 1usize { 0 } else { 1 }
            }''')

    def test_maps_of_copy_entries_clear_in_place(self):
        self.expect('''
            use std::collections::hashmap::OwnedMap!;
            fn main() -> i32 {
                var m = OwnedMap!::new[u32, u32](std::process::hash_seed());
                var i = 0u32;
                while i < 100u32 {
                    let old = &m.insert(i, i * 2u32);
                    i = i + 1u32;
                }
                &m.clear();
                let empty = &m.is_empty();
                let again = &m.insert(7u32, 1u32);
                var seven = 7u32;
                let found = &m.at(&seven) == 1u32 && &m.len() == 1usize;
                m.destroy();
                if empty && found { 0 } else { 1 }
            }''')

    def test_vectors_insert_remove_swap_and_reverse(self):
        # Linear elements move: `insert` and `remove` shift the rest, `swap` and `reverse` exchange them.
        self.expect('''
            use std::collections::vec::{OwnedVec!, Vec!};
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            use std::option::Option;
            fn label(n: u64) -> Buffer! { Buffer!::new().put(n) }
            fn drop(b: Buffer!) { b.destroy(); }
            fn main() -> i32 {
                var v = OwnedVec!::new[Buffer!]();
                &v.insert(0usize, label(1u64));
                &v.insert(1usize, label(3u64));
                &v.insert(1usize, label(2u64));
                &v.insert(0usize, label(0u64));
                &v.swap(0usize, 3usize);
                &v.reverse();
                let taken = match &v.remove(1usize) {
                    Option::Some(b) => { var b = b; let n = &b.len(); b.destroy(); n },
                    Option::None => 9usize,
                };
                let missed = match &v.remove(7usize) {
                    Option::Some(b) => { b.destroy(); false },
                    Option::None => true,
                };
                var shown = Buffer!::new();
                while !&v.is_empty() {
                    match &v.pop() {
                        Option::Some(b) => { shown = shown.put(b); },
                        Option::None => (),
                    }
                }
                v.destroy_with(drop);
                var n = OwnedVec!::new[u32]();
                &n.push(5u32);
                &n.push(6u32);
                &n.push(7u32);
                let copies = (&n.first()).unwrap_or(0u32) == 5u32 && &n.contains(6u32) && !&n.contains(8u32);
                n.destroy();
                var exists 'k. (tag, raw) = Vec!::new[u32]();
                var exists 'k2. (tag, raw) = raw.push(tag, 4u32);
                let branded = (&raw.get(&tag, 0usize)).unwrap_or(0u32) == 4u32 && &raw.at(&tag, 0usize) == 4u32 && (&raw.get(&tag, 1usize)).is_none();
                raw.destroy(tag);
                var exists 'b. (btag, bytes, restore) = shown.open();
                let order = bytes.as_str().is(&btag, "310");
                restore.restore(btag).destroy();
                if taken == 1usize && missed && order && copies && branded { 0 } else { 1 }
            }''')

    def test_vector_insert_past_the_end_stops(self):
        result = self.run_program('''
            use std::collections::vec::OwnedVec!;
            fn main() -> i32 {
                var v = OwnedVec!::new[u32]();
                &v.insert(1usize, 5u32);
                v.destroy();
                0
            }''')
        self.assertLess(result.returncode, 0)

    def test_map_lookups_allocate_nothing(self):
        # A key that is only looked up is lent, not copied into a block of its own: 10000 lookups through
        # each of `get`, `contains`, `find` and a `remove` of a missing key make no allocation.
        self.expect('''
            use std::collections::hashmap::{HashMap!, OwnedMap!};
            use std::option::Option;
            extern "C" fn probe_allocations() -> u64;
            fn find(&named: OwnedMap![u64, u64], &probe: u64) -> bool {
                var exists 'm. (tag, map) = named.inner;
                let found = (&map.find(&tag, &probe)).is_some();
                named = OwnedMap!.{ inner: pack 'm. (tag, map) };
                found
            }
            fn main() -> i32 {
                var m = OwnedMap!::new[u64, u64](std::process::hash_seed());
                let none = &m.insert(1u64, 10u64);
                let before = unsafe probe_allocations();
                var hits = 0u64;
                var i = 0u64;
                while i < 10000u64 {
                    var key = i % 2u64;
                    match &m.get(&key) { Option::Some(v) => { hits = hits + v; }, Option::None => () }
                    if &m.contains(&key) && find(&m, &key) { hits = hits + 1u64; }
                    var missing = 2u64;
                    let gone = &m.remove(&missing);
                    i = i + 1u64;
                }
                let made = unsafe probe_allocations() - before;
                m.destroy();
                if hits != 55000u64 { 1 } else if made != 0u64 { 2 } else { 0 }
            }''', counted=True)

    def test_arena_maps_update_retain_and_combine(self):
        self.expect('''
            use std::arena::Domain!;
            use std::collections::arena_map::{ArenaMap, ArenaSet};
            forall 'k.
            fn check(&cx: Domain!['k]) -> bool {
                let m: ArenaMap[u32, u32, 'k] = ArenaMap::new(&cx);
                let a = m.get_or_insert(&cx, 1u32, 10u32);
                let b = m.get_or_insert(&cx, 1u32, 20u32);
                var c = m.upsert(&cx, 1u32, 0u32, |&cx, v: u32| v + 5u32);
                let d = m.upsert(&cx, 2u32, 7u32, |&cx, v: u32| v * 2u32);
                let e = m.insert(&cx, 3u32, 30u32);
                m.retain(&cx, |&c, k: u32, v: u32| k != 2u32);
                let maps = a == 10u32 && b == 10u32 && c == 15u32 && d == 14u32 && !m.contains(&cx, 2u32)
                    && m.len(&cx) == 2usize && m.at(&cx, 3u32) == 30u32 && m.key_at(&cx, 0usize).unwrap_or(0u32) == 1u32;
                let x: ArenaSet[u32, 'k] = ArenaSet::new(&cx);
                let y: ArenaSet[u32, 'k] = ArenaSet::new(&cx);
                x.insert_all(&cx, m.keys(&cx));
                let more = x.insert(&cx, 2u32);
                let also = y.insert(&cx, 2u32);
                let other = y.insert(&cx, 4u32);
                let both = x.intersection(&cx, y);
                let only = x.difference(&cx, y);
                let sets = both.len(&cx) == 1usize && both.contains(&cx, 2u32) && only.len(&cx) == 2usize
                    && only.key_at(&cx, 0usize).unwrap_or(0u32) == 1u32 && only.contains(&cx, 3u32);
                maps && sets
            }
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let ok = check(&cx);
                cx.destroy();
                if ok { 0 } else { 1 }
            }''')

    def test_arena_map_retain_counts_what_its_callback_changes(self):
        # The callback holds a copy of the map's handle and removes key 2 while key 1 is visited, then
        # rejects key 1: no key is left, and the count says so.
        self.expect('''
            use std::arena::Domain!;
            use std::collections::arena_map::ArenaMap;
            forall 'k.
            fn check(&cx: Domain!['k]) -> bool {
                let map: ArenaMap[u32, u32, 'k] = ArenaMap::new(&cx);
                map.insert(&cx, 1u32, 10u32);
                map.insert(&cx, 2u32, 20u32);
                map.retain(&cx, |&c, k: u32, v: u32| {
                    if k == 1u32 {
                        let gone = map.remove(&c, 2u32);
                    }
                    false
                });
                let emptied = map.len(&cx) == 0usize && map.keys(&cx).len(&cx) == 0usize;
                map.insert(&cx, 3u32, 30u32);
                let gone = map.remove(&cx, 3u32);
                emptied && map.len(&cx) == 0usize
            }
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let ok = check(&cx);
                cx.destroy();
                if ok { 0 } else { 1 }
            }''')

    def test_arena_vec_push_str_of_its_own_bytes(self):
        # `to_str` views the vec's first segment; pushing that view back after a `truncate` copies from
        # bytes the push itself overwrites.
        self.expect('''
            use std::arena::Domain!;
            use std::collections::arena_vec::ArenaVec;
            use std::fmt::Writer;
            forall 'k.
            fn check(&cx: Domain!['k]) -> bool {
                let v: ArenaVec[u8, 'k] = ArenaVec::new(&cx);
                v.push_static(&cx, "abcd");
                let s = v.to_str(&cx);
                v.truncate(&cx, 1usize);
                v.push_str(&cx, s);
                let grown = v.to_str(&cx).is(&cx, "aabcd");
                let t = v.to_str(&cx);
                v.truncate(&cx, 0usize);
                v.push_str(&cx, t.slice(1usize, 4usize));
                grown && v.to_str(&cx).is(&cx, "abc")
            }
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let ok = check(&cx);
                cx.destroy();
                if ok { 0 } else { 1 }
            }''')

    def test_arena_vec_locates_indices_past_four_billion_segments(self):
        # Segment k of a vec of u64 holds 4 << k elements, after 4 * (2^k - 1) of them.
        self.expect('''
            use std::collections::arena_vec::locate;
            fn at(i: usize, segment: usize, offset: usize) -> bool {
                let (k, o) = locate[u64](i);
                k == segment && o == offset && o < 4usize << k
            }
            fn main() -> i32 {
                if at(1000usize, 7usize, 492usize) && at(17179869184usize, 32usize, 4usize)
                    && at(34359738368usize, 33usize, 4usize) { 0 } else { 1 }
            }''')

    def test_lists_are_built_front_to_back(self):
        self.expect('''
            use std::arena::Domain!;
            use std::collections::list::List;
            use std::option::Result;
            forall 'k.
            fn check(&cx: Domain!['k]) -> bool {
                let list: List[u32, 'k] = List::empty().cons(&cx, 3u32).cons(&cx, 2u32).cons(&cx, 1u32);
                let doubled = list.map(&cx, |&c, x: u32| x * 2u32);
                let joined = list.append(&cx, doubled);
                let halted: Result[List[u32, 'k], u32] = list.try_map(&cx, |&c, x: u32| if x == 2u32 { Result::Err.(x) } else { Result::Ok.(x) });
                let failed = match halted { Result::Ok(l) => 0u32, Result::Err(e) => e };
                joined.len(&cx) == 6usize && joined.nth(&cx, 3usize).unwrap_or(0u32) == 2u32
                    && joined.last(&cx).unwrap_or(0u32) == 6u32 && list.len(&cx) == 3usize && failed == 2u32
            }
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let ok = check(&cx);
                cx.destroy();
                if ok { 0 } else { 1 }
            }''')

    def test_list_cells_are_out_of_reach(self):
        # A cell's `Ref` would let code rewrite a list others hold, and make it cyclic.
        self.reject('''
            use std::arena::Domain!;
            use std::collections::list::List;
            fn main() -> i32 {
                var exists 'k. cx = Domain!::new(std::process::hash_seed());
                let list: List[u32, 'k] = List::empty().cons(&cx, 1u32);
                let cells = match list {
                    List::Cell(r) => 1,
                    List::Empty => 0,
                };
                cx.destroy();
                cells
            }''', "the shape of alloc::collections::list::List is not exposed here")

    def test_buffers_truncate_and_clear(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::fmt::Writer;
            fn main() -> i32 {
                var b = Buffer!::new().put("hello").truncate(2usize).put("y");
                let n = &b.len();
                var b = b.truncate(10usize);
                let m = &b.len();
                var b = b.clear();
                let z = &b.len();
                b.destroy();
                if n == 3usize && m == 3usize && z == 0usize { 0 } else { 1 }
            }''')

if __name__ == "__main__":
    unittest.main()
