"""The standard library at run time: small programs that use it, built with its C helpers and run, each
checking its own results; and programs that misuse it, rejected."""

import os
import resource
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import IO, Callable, Optional

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
                    stdout: Optional[IO[str]] = None, stdin_fd: Optional[int] = None,
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
            source_in: dict[str, object] = {"input": stdin} if stdin_fd is None else {"stdin": stdin_fd}
            return subprocess.run([str(executable), *args], **source_in,  # type: ignore[call-overload]
                                  stdout=stdout if stdout is not None else subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=60, cwd=directory, preexec_fn=preexec)

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
                            Result::Err(e) => if e.errno == 21i32 { 0 } else { 3 },
                        };
                        let closed = file.close();
                        status
                    },
                }
            }''', deps=("std", "libc"))

    def test_a_failed_write_is_an_error(self):
        self.expect('''
            use std::bytes::buffer::Buffer;
            use std::option::Result;
            use libc::fs::create;
            fn main() -> i32 {
                var path = Buffer::new().push_static("/dev/full");
                let created = create(&path);
                path.destroy();
                match created {
                    Result::Err(code) => 1,
                    Result::Ok(file) => {
                        var file = file;
                        let status = match &file.write_static("full") {
                            Result::Ok(nothing) => 2,
                            Result::Err(e) => if e.errno == 28i32 { 0 } else { 3 },
                        };
                        let closed = file.close();
                        status
                    },
                }
            }''', deps=("std", "libc"))

    def test_print_reports_a_failed_write(self):
        with open("/dev/full", "w") as full:
            result = self.run_program('''
                use std::io::print;
                use std::option::Result;
                fn main() -> i32 {
                    match print(1, "lost") {
                        Result::Ok(nothing) => 1,
                        Result::Err(e) => e.errno,
                    }
                }''', stdout=full)
        self.assertEqual(result.returncode, 28)

    def test_read_line_tells_an_empty_line_from_the_end(self):
        self.expect('''
            use std::io::{read_line, print_buffer};
            use std::bytes::buffer::Buffer;
            use std::option::{Option, Result};
            fn main() -> i32 {
                var lines = 0i32;
                var reading = true;
                while reading {
                    match read_line() {
                        Result::Err(e) => return 9,
                        Result::Ok(line) => match line {
                            Option::None => {
                                reading = false;
                            },
                            Option::Some(text) => {
                                lines = lines + 1i32;
                                print_buffer(1, Buffer::new().push_byte(b'[').push_buffer(text).push_byte(b']'));
                            },
                        },
                    }
                }
                lines
            }''', status=3, stdout="[a][][b]", stdin="a\n\nb")

    def test_a_read_error_is_not_the_end_of_the_input(self):
        directory = os.open(".", os.O_RDONLY)
        try:
            result = self.run_program('''
                use std::io::read_all_stdin;
                use std::option::Result;
                fn main() -> i32 {
                    match read_all_stdin() {
                        Result::Ok(text) => { text.destroy(); 1 },
                        Result::Err(e) => e.errno,
                    }
                }''', stdin_fd=directory)
        finally:
            os.close(directory)
        self.assertEqual(result.returncode, 21)

    # ------------------------------------------------- buffers, spans, maps --

    def test_a_zero_capacity_buffer_grows(self):
        self.expect('''
            use std::bytes::buffer::Buffer;
            fn main() -> i32 {
                let b = Buffer::new_with_cap(0usize).push_byte(b'a').push_static("bc");
                let n = &b.len();
                b.destroy();
                let z = Buffer::new_zeroed(0usize);
                let c = &z.clone();
                var c = c.push_byte(b'x');
                let m = &c.len();
                z.destroy();
                c.destroy();
                if n == 3usize && m == 1usize { 0 } else { 1 }
            }''')

    def test_split_on_an_empty_separator_is_the_whole_span(self):
        self.expect('''
            use std::alloc::arena::Domain;
            use std::raw::Span;
            fn main() -> i32 {
                let exists 'k. cx = Domain::new();
                let text = Span::from_static(&cx, "a,b");
                let whole = text.split(&cx, Span::from_static(&cx, ""));
                let parts = text.split(&cx, Span::from_static(&cx, ","));
                let status = if whole.len(&cx) == 1usize && whole.at(&cx, 0usize).len() == 3usize
                    && parts.len(&cx) == 2usize { 0 } else { 1 };
                cx.destroy();
                status
            }''')

    def test_arena_map_grows_to_any_size_and_stays_small_under_churn(self):
        self.expect('''
            use std::alloc::arena::Domain;
            use std::collections::arena_map::ArenaMap;
            use std::option::Option;
            fn is(o: Option[u32], v: u32) -> bool {
                match o { Option::Some(x) => x == v, Option::None => false }
            }
            fn main() -> i32 {
                let exists 'k. cx = Domain::new();
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

    def test_hash_chains_of_colliding_keys(self):
        self.expect('''
            use std::collections::hashmap::{OwnedMap, Found};
            use std::hash::{Hash, Eq, HashState};
            exposed struct Key { n: u32 }
            instance Hash[Key] {
                fn hash(self: Key, state: HashState) -> (Key, HashState) { (self, state) }
            }
            instance Eq[Key, Key] {
                fn eq(a: Key, b: Key) -> (Key, Key, bool) { (a, b, a.n == b.n) }
            }
            fn drop_entry(key: Key, value: u32) {}
            fn one(f: Found[(Key, u32)]) -> i32 {
                match f { Found::Item(kv) => 1i32, Found::Missing => 0i32 }
            }
            fn main() -> i32 {
                var map = OwnedMap::new[Key, u32]();
                var i = 0u32;
                while i < 3000u32 {
                    let old = &map.insert(Key.{ n: i }, i);
                    i = i + 1u32;
                }
                let replaced = match &map.insert(Key.{ n: 1500u32 }, 7u32) { Found::Item(kv) => 1i32, Found::Missing => 0i32 };
                let (k0, first) = &map.remove(Key.{ n: 0u32 });
                let (k1, middle) = &map.remove(Key.{ n: 1000u32 });
                let (k2, last) = &map.remove(Key.{ n: 2999u32 });
                let (k3, again) = &map.remove(Key.{ n: 1000u32 });
                let (k4, seven) = &map.get(Key.{ n: 1500u32 });
                let removed = one(first) + one(middle) + one(last) + one(again);
                let kept = match seven { Found::Item(v) => v == 7u32, Found::Missing => false };
                let n = &map.len();
                map.destroy(drop_entry);
                if replaced == 1i32 && removed == 3i32 && kept && n == 2997usize { 0 } else { 1 }
            }''')

    def test_long_lists_and_scans_take_no_stack(self):
        self.expect('''
            use std::alloc::arena::Domain;
            use std::collections::list::List;
            use std::bytes::buffer::Buffer;
            use std::bytes::cursor::Cursor;
            use std::option::Result;
            fn is_a(b: u8) -> bool { b == b'a' }
            forall 'k.
            fn lists(&cx: Domain!['k]) -> bool {
                var list: List[u32, 'k] = List::Empty;
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
                let exists 'k. cx = Domain::new();
                let listed = lists(&cx);
                cx.destroy();
                var b = Buffer::new();
                var j = 0usize;
                while j < 1000000usize {
                    b = b.push_byte(b'a');
                    j = j + 1usize;
                }
                let exists 't. (tag, bytes, restore) = b.open();
                var cur = Cursor::new(bytes);
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

    # ------------------------------------------------------- core accessors --

    ACCESSORS = '''
        use std::option::{Option, Result};
        use std::raw::Span;
        use std::bytes::buffer::Buffer;
        fn main() -> i32 {
            let a = Option::Some.(3u32).unwrap();
            let r: Result[u32, u32] = Result::Ok.(4u32);
            let b = r.unwrap();
            let exists 'b. (tag, bytes, restore) = Buffer::new().push_static("pq").open();
            let c = bytes.at(&tag, 0usize);
            let (p, n, m) = bytes.into_parts();
            let text = unsafe Span::from_raw_parts(&tag, p, n);
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


if __name__ == "__main__":
    unittest.main()
