"""The standard library at run time: small programs that use it, built with its C helpers and run, each
checking its own results; and programs that misuse it, rejected."""

import os
import resource
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Optional

from kursal.cc import cc_command
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures

MANIFEST = '[package]\nname="t"\nkind="executable"\nentry="package::main"\nsource_root="."\n'
C_HELPERS = [LIB / "libc/fs.c", LIB / "libc/thread.c"]


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
                    ) -> subprocess.CompletedProcess[str]:
        """Builds `source` as a package with `deps` and runs it; the completed process."""
        with tempfile.TemporaryDirectory(prefix="kursal-std-") as base:
            directory = Path(base)
            package(directory, source, deps)
            resolver, sigs, program = lowered(directory)
            c_file = directory / "main.c"
            c_file.write_text(emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl)))
            executable = directory / "main"
            built = subprocess.run(cc_command([str(c_file), *map(str, C_HELPERS)], str(executable)),
                                   capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr[:500])
            return subprocess.run([str(executable), *args], input=stdin, capture_output=True, text=True,
                                  timeout=60, cwd=directory, preexec_fn=preexec)

    def expect(self, source: str, status: int = 0, stdout: str = "", **kwargs) -> None:
        result = self.run_program(source, **kwargs)
        self.assertEqual((result.stdout, result.returncode), (stdout, status), result.stderr[:500])

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
            use std::alloc::heap::HeapAllocator;
            use std::alloc::owned::Allocation;
            fn main() -> i32 {
                let exists 'r. (record, region) = Allocation::adopt_value(HeapAllocator::alloc(8usize), 41u64);
                let old = &region.replace(1u64);
                HeapAllocator::drop(record.release(region));
                if old == 41u64 { 0 } else { 1 }
            }''')

    def test_adopt_value_aborts_on_a_block_too_small(self):
        result = self.run_program('''
            use std::alloc::heap::HeapAllocator;
            use std::alloc::owned::Allocation;
            fn main() -> i32 {
                let exists 'r. (record, region) = Allocation::adopt_value(HeapAllocator::alloc(4usize), 41u64);
                let old = &region.replace(1u64);
                HeapAllocator::drop(record.release(region));
                0
            }''')
        self.assertLess(result.returncode, 0)

    def test_owned_and_vec_round_trips(self):
        self.expect('''
            use std::alloc::owned::Owned;
            use std::collections::vec::Vec;
            use std::bytes::buffer::Buffer;
            fn main() -> i32 {
                let exists 'a. (record, region) = Owned::new(7u32).open();
                let seven = &region.replace(8u32);
                let eight = record.close(region).into_value();
                let exists 'v. (tag, v) = Vec::new[u8]();
                let b = std::text::str::finish(v, tag);
                let n = &b.len();
                b.destroy();
                if seven == 7u32 && eight == 8u32 && n == 0usize { 0 } else { 1 }
            }''')

    # -------------------------------------------------------- alloc::hashmap --

    def test_remove_in_rebrands_and_removes(self):
        self.expect('''
            use std::collections::hashmap::{HashMap, OwnedMap, Found};
            fn drop_entry(key: u32, value: u32) {}
            fn main() -> i32 {
                var cx = ();
                let exists 'm. (tag, map) = HashMap::new[u32, u32]();
                let exists 'a. (tag, map, old) = map.insert_in(tag, &cx, 1u32, 10u32);
                let exists 'b. (tag, map, removed) = map.remove_in(tag, &cx, 1u32);
                let again = match &map.get_in(&tag, &cx, 1u32) { Found::Item(r) => 1i32, Found::Missing => 0i32 };
                let got = match removed { Found::Item(v) => v, Found::Missing => 0u32 };
                map.destroy(tag, drop_entry);
                var owned = OwnedMap::new[u32, u32]();
                let none = &owned.insert_in(&cx, 2u32, 20u32);
                let twenty = match &owned.remove_in(&cx, 2u32) { Found::Item(v) => v, Found::Missing => 0u32 };
                let left = &owned.len();
                owned.destroy(drop_entry);
                if again == 0i32 && got == 10u32 && twenty == 20u32 && left == 0usize { 0 } else { 1 }
            }''')

    # ---------------------------------------------------------------- thread --

    def test_trampoline_is_unsafe(self):
        self.reject('''
            use thread::thread::trampoline;
            use std::alloc::heap::HeapAllocator;
            fn main() -> i32 {
                let raw = HeapAllocator::alloc(8usize).into_raw();
                let result = trampoline[(), u32](raw);
                0
            }''', "calling an unsafe function requires unsafe authorization", deps=("std", "thread"))

    def test_spawn_returns_a_zero_size_result(self):
        self.expect('''
            use thread::thread;
            use std::option::Result;
            fn main() -> i32 {
                match thread::spawn(|| -> () { () }) {
                    Result::Err(failed) => {
                        let (f, error) = failed;
                        f();
                        1
                    },
                    Result::Ok(t) => match t.join() {
                        Result::Ok(unit) => 0,
                        Result::Err(error) => 2,
                    },
                }
            }''', deps=("std", "thread"))

    @unittest.skipIf(os.geteuid() == 0, "the process limit does not bind root")
    def test_failed_spawn_hands_the_closure_back(self):
        def no_more_processes() -> None:
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        self.expect('''
            use thread::thread;
            use std::bytes::buffer::Buffer;
            use std::option::Result;
            fn main() -> i32 {
                let owned = Buffer::new().push_static("abc");
                match thread::spawn(|| -> usize { let n = &owned.len(); owned.destroy(); n }) {
                    Result::Err(failed) => {
                        let (f, error) = failed;
                        if f() == 3usize && error.code > 0i32 { 7 } else { 1 }
                    },
                    Result::Ok(t) => match t.join() {
                        Result::Ok(n) => 2,
                        Result::Err(error) => 3,
                    },
                }
            }''', status=7, deps=("std", "thread"), preexec=no_more_processes)

    # ----------------------------------------------------------------- arena --

    def test_arena_aligns_addresses(self):
        self.expect('''
            use std::alloc::arena::Arena;
            fn main() -> i32 {
                let exists 'k. (tag, arena) = Arena::new();
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
            use std::alloc::arena::Domain;
            use std::collections::list::List;
            use std::bytes::buffer::Buffer;
            fn main() -> i32 {
                let exists 'k. cx = Domain::new();
                let list = List::Empty.cons(&cx, Buffer::new());
                cx.destroy();
                0
            }''', "type argument must satisfy Copy")

    # ------------------------------------------------------------------ toml --

    def test_toml_reads_a_document(self):
        self.expect('''
            use std::alloc::arena::Domain;
            use std::bytes::buffer::Buffer;
            use std::option::Result;
            use toml::document::parse;
            fn main() -> i32 {
                let exists 'k. cx = Domain::new();
                let input = Buffer::new().push_static("[package]\\nname = \\"x\\" # c\\r\\n");
                let status = match parse(&cx, input) { Result::Ok(value) => 0, Result::Err(e) => 1 };
                let bad = match parse(&cx, Buffer::new().push_static("[a")) { Result::Ok(value) => 1, Result::Err(e) => 0 };
                cx.destroy();
                status + bad
            }''', deps=("std", "toml"))

    # -------------------------------------------------------------------- io --

    def test_reading_a_directory_is_an_error(self):
        self.expect('''
            use std::bytes::buffer::Buffer;
            use std::option::Result;
            use libc::fs::open_read;
            fn main() -> i32 {
                var path = Buffer::new().push_static(".");
                let opened = open_read(&path);
                path.destroy();
                match opened {
                    Result::Err(code) => 1,
                    Result::Ok(file) => {
                        var file = file;
                        let status = match &file.read_to_buffer() {
                            Result::Ok(contents) => { contents.destroy(); 2 },
                            Result::Err(code) => if code == 21i32 { 0 } else { 3 },
                        };
                        let closed = file.close();
                        status
                    },
                }
            }''', deps=("std", "libc"))


if __name__ == "__main__":
    unittest.main()
