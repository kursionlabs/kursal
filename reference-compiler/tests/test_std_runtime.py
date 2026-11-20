"""The standard library at run time: small programs that use it, built with its C helpers and run, each
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

from kursal.cc import cc_command
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures

MANIFEST = '[package]\nname="t"\nkind="executable"\nentry="package::main"\nsource_root="."\n'
C_HELPERS = [LIB / "libc/fs.c", LIB / "libc/thread.c"]
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
            sources, link = [str(c_file), *map(str, C_HELPERS)], ()
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
                let exists 'r. (record, region) = Allocation!::adopt_value(HeapAllocator::alloc(8usize), 41u64);
                let old = &region.replace(1u64);
                HeapAllocator::drop(record.release(region));
                if old == 41u64 { 0 } else { 1 }
            }''')

    def test_adopt_value_aborts_on_a_block_too_small(self):
        result = self.run_program('''
            use std::heap::HeapAllocator;
            use std::owned::Allocation!;
            fn main() -> i32 {
                let exists 'r. (record, region) = Allocation!::adopt_value(HeapAllocator::alloc(4usize), 41u64);
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
                let exists 'a. (record, region) = Owned!::new(7u32).open();
                let seven = &region.replace(8u32);
                let eight = record.close(region).into_value();
                let exists 'v. (tag, v) = Vec!::new[u8]();
                let n = &v.len(&tag);
                v.free(tag);
                if seven == 7u32 && eight == 8u32 && n == 0usize { 0 } else { 1 }
            }''')

    def test_fallible_constructors_report_exhaustion(self):
        # Under a 256 MiB address-space limit, a 1 GiB buffer and a vec of four 128 MiB slots are refused;
        # the constructors that fit build containers that work.
        def limit() -> None:
            resource.setrlimit(resource.RLIMIT_AS, (256 << 20, 256 << 20))
        self.expect('''
            use std::buffer::Buffer!;
            use std::collections::hashmap::{HashMap!, OwnedMap!};
            use std::collections::vec::{OwnedVec!, Vec!};
            use std::io::print_buffer;
            use std::option::Result;
            fn forget(k: u32, v: u32) -> () { () }
            fn main() -> i32 {
                let big_buffer = match Buffer!::try_new_with_cap(1usize << 30usize) {
                    Result::Ok(b) => { b.destroy(); "built" },
                    Result::Err(e) => "refused",
                };
                let big_vec = match Vec!::try_new[[u64; 16777216]]() {
                    Result::Ok(package) => {
                        let exists 'v. (tag, v) = package;
                        v.free(tag);
                        "built"
                    },
                    Result::Err(e) => "refused",
                };
                let buffer = match Buffer!::try_new_with_cap(2usize) {
                    Result::Ok(b) => { let b = b.push_static("abc"); let n = &b.len(); b.destroy(); n },
                    Result::Err(e) => 0usize,
                };
                let vec = match Vec!::try_new[u64]() {
                    Result::Ok(package) => {
                        let exists 'v. (tag, v) = package;
                        let exists 'w. (tag, v) = v.push(tag, 7u64);
                        let n = &v.len(&tag);
                        v.free(tag);
                        n
                    },
                    Result::Err(e) => 0usize,
                };
                let map = match HashMap!::try_new[u32, u32]() {
                    Result::Ok(package) => {
                        let exists 'm. (tag, m) = package;
                        let exists 'n. (tag, m, old) = m.insert(tag, 1u32, 2u32);
                        let n = &m.len(&tag);
                        m.free(tag);
                        n
                    },
                    Result::Err(e) => 0usize,
                };
                let owned_vec = match OwnedVec!::try_new[u32]() {
                    Result::Ok(v) => { let v = v.push(3u32).push(4u32); let n = &v.len(); v.free(); n },
                    Result::Err(e) => 0usize,
                };
                let owned_map = match OwnedMap!::try_new[u32, u32]() {
                    Result::Ok(m) => { var m = m; let old = &m.insert(1u32, 2u32); let n = &m.len(); m.destroy(forget); n },
                    Result::Err(e) => 0usize,
                };
                print_buffer(f"{big_buffer} {big_vec} {buffer} {vec} {map} {owned_vec} {owned_map}");
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
                let exists 'm. (tag, map) = HashMap!::new[u32, u32]();
                let exists 'a. (tag, map, old) = map.insert_in(tag, &cx, 1u32, 10u32);
                let exists 'b. (tag, map, removed) = map.remove_in(tag, &cx, 1u32);
                let again = match &map.get_in(&tag, &cx, 1u32) { Option::Some(r) => 1i32, Option::None => 0i32 };
                let got = match removed { Option::Some(v) => v, Option::None => 0u32 };
                map.destroy(tag, drop_entry);
                var owned = OwnedMap!::new[u32, u32]();
                let none = &owned.insert_in(&cx, 2u32, 20u32);
                let twenty = match &owned.remove_in(&cx, 2u32) { Option::Some(v) => v, Option::None => 0u32 };
                let left = &owned.len();
                owned.destroy(drop_entry);
                if again == 0i32 && got == 10u32 && twenty == 20u32 && left == 0usize { 0 } else { 1 }
            }''')

    # ---------------------------------------------------------------- thread --

    def test_trampoline_is_unsafe(self):
        self.reject('''
            use thread::thread::trampoline;
            use std::heap::HeapAllocator;
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
            use std::buffer::Buffer!;
            use std::option::Result;
            fn main() -> i32 {
                let owned = Buffer!::new().push_static("abc");
                match thread::spawn(|| -> usize { let n = &owned.len(); owned.destroy(); n }) {
                    Result::Err(failed) => {
                        let (f, error) = failed;
                        if f() == 3usize && error.errno > 0i32 { 7 } else { 1 }
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
            use std::arena::Arena!;
            fn main() -> i32 {
                let exists 'k. (tag, arena) = Arena!::new();
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
                let exists 'k. cx = Domain!::new();
                let list = List::empty().cons(&cx, Buffer!::new());
                cx.destroy();
                0
            }''', "type argument must satisfy Copy")

    # -------------------------------------------------------------------- fs --

    def test_files_and_directories_by_span(self):
        # Paths are spans of an arena, read in place; each step's failure has its own status.
        self.expect('''
            use std::arena::Domain!;
            use std::buffer::Buffer!;
            use std::io::Error;
            use std::fs::{FileKind, canonicalize, metadata, mkdir, path_exists, read_dir, read_to_arena, remove, rename, write};
            use std::option::{Option, Result};
            use std::raw::Span;
            fn main() -> i32 {
                let exists 'k. cx = Domain!::new();
                var cx = cx;
                let dir = Span::from_static(&cx, "d.dir/");
                let dir = dir.take(5usize);
                let a = Buffer!::new().push_static("d.dir/a").save(&cx);
                let b = Buffer!::new().push_static("d.dir/b").save(&cx);
                let none = Error.{ errno: 0i32 };
                var status = 0i32;
                if mkdir(&cx, dir).is_err() { status = 1; }
                if mkdir(&cx, dir).err().unwrap_or(none).errno != 17i32 { status = 2; }
                if write(&cx, a, Buffer!::new().push_static("abc")).is_err() { status = 3; }
                if rename(&cx, a, b).is_err() || path_exists(&cx, a).unwrap_or(true) || !path_exists(&cx, b).unwrap_or(false) { status = 4; }
                match read_to_arena(&cx, b) {
                    Result::Ok(text) => if !text.is(&cx, "abc") { status = 5; },
                    Result::Err(e) => { status = 5; },
                }
                match metadata(&cx, b) {
                    Result::Ok(m) => match m.kind { FileKind::File => if m.size != 3u64 { status = 6; }, _ => { status = 6; } },
                    Result::Err(e) => { status = 6; },
                }
                match metadata(&cx, dir) {
                    Result::Ok(m) => match m.kind { FileKind::Directory => (), _ => { status = 7; } },
                    Result::Err(e) => { status = 7; },
                }
                match canonicalize(&cx, b) {
                    Result::Ok(path) => {
                        let full = path.save(&cx);
                        if !full.starts_with(&cx, Span::from_static(&cx, "/")) || !full.ends_with(&cx, Span::from_static(&cx, "/d.dir/b")) { status = 8; }
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
                let nul = Buffer!::new().push_static("a").push_byte(0u8).push_static("b").save(&cx);
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
                use std::io::print_buffer;
                use std::option::Option;
                use std::slice::StaticRead;
                fn main() -> i32 {
                    var sr = StaticRead::new();
                    match variable(&sr, "KURSAL_TEST_VARIABLE") {
                        Option::Some(value) => { print_buffer(value); },
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
            use std::io::print_buffer;
            use std::option::Option;
            use std::process::Args;
            fn main(args: Args) -> i32 {
                var out = Buffer!::new();
                var i = 1usize;
                while i < args.len() {
                    match args.bytes(i) {
                        Option::Some(arg) => { out = out.push_static(arg).push_static("|"); },
                        Option::None => (),
                    }
                    i = i + 1usize;
                }
                print_buffer(out);
                if args.bytes(args.len()).is_none() { args.len() as i32 } else { 99 }
            }''', status=4, stdout="one|two words||", args=("one", "two words", ""))

    def test_each_entry_shape_makes_its_exit_status(self):
        """`fn main()`, `-> i32` and `-> Result[(), Error]`, each with and without `Args`: the exit status
        and stderr of each, and the report of an `Err`."""
        imports = '''
            use std::io::{Error, File!, print};
            use std::option::Result;
            use std::process::Args;
            use std::slice::StaticRead;
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
            use std::fs::write;
            use std::io::print_buffer;
            use std::option::Result;
            use std::process::{Command!, Exit};
            use std::slice::StaticRead;
            fn outcome(command: Command!) -> Buffer! {
                match command.run() {
                    Result::Ok(exit) => match exit {
                        Exit::Code(c) => Buffer!::new().push_static("code ").push_usize(c as usize),
                        Exit::Signal(s) => Buffer!::new().push_static("signal ").push_usize(s as usize),
                    },
                    Result::Err(e) => Buffer!::new().push_static("errno ").push_usize(e.errno as usize),
                }
            }
            fn main() -> i32 {
                var sr = StaticRead::new();
                let made = write(&sr, "not-executable", Buffer!::new().push_static("#!/bin/sh"));
                print_buffer(outcome(Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, "exit 7")).push_static(","));
                print_buffer(outcome(Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, "kill -15 $$")).push_static(","));
                print_buffer(outcome(Command!::new(&sr, "no-such-program-for-kursal")).push_static(","));
                print_buffer(outcome(Command!::new(&sr, "./not-executable")).push_static(","));
                print_buffer(outcome(Command!::new(&sr, "true").arg(&sr, "a\\x00b")));
                Command!::new(&sr, "never").arg(&sr, "run").destroy();
                0
            }''', stdout="code 7,signal 15,errno 2,errno 13,errno 22")

    def test_spawned_children_are_polled_and_waited_for(self):
        self.expect('''
            use std::io::print_buffer;
            use std::buffer::Buffer!;
            use std::option::Result;
            use std::process::{Child!, Command!, Exit, Polled!, available_parallelism, trap};
            use std::slice::StaticRead;
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
                print_buffer(Buffer!::new().push_usize(slow_code).push_static(",").push_usize(quick_code).push_static(",")
                    .push_usize(missing).push_static(if polls > 1usize { ",polled" } else { ",ended at once" })
                    .push_static(if available_parallelism() > 0usize { ",cpus" } else { ",none" }));
                0
            }''', stdout="3,4,2,polled,cpus")

    def test_output_captures_both_streams_and_the_exit(self):
        # A child writing 1 MiB to each of its standard output and error, the second first, fills one pipe
        # while this process would wait on the other: both are read as they come.
        self.expect('''
            use std::buffer::Buffer!;
            use std::io::print_buffer;
            use std::option::Result;
            use std::process::{Captured!, Command!, Exit};
            use std::slice::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                let script = "head -c 1048576 /dev/zero >&2; head -c 1048576 /dev/zero; echo out; echo err >&2; exit 3";
                match Command!::new(&sr, "sh").arg(&sr, "-c").arg(&sr, script).output() {
                    Result::Ok(captured) => {
                        let { exit, stdout, stderr } = captured;
                        let code = match exit { Exit::Code(c) => c, Exit::Signal(s) => 0i32 - s };
                        let out = &stdout.len();
                        let err = &stderr.len();
                        let success = exit.success();
                        let sizes = f"{code} {out} {err} {success}";
                        stdout.destroy();
                        stderr.destroy();
                        print_buffer(sizes);
                        0
                    },
                    Result::Err(e) => 1,
                }
            }''', stdout="3 1048580 1048580 false")

    def test_commands_take_their_streams_environment_and_directory(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::fs::{mkdir, write};
            use std::io::{File!, print_buffer};
            use std::option::{Option, Result};
            use std::process::{Captured!, Command!, Exit, Stdio!};
            use std::slice::StaticRead;
            // What `command` writes to its standard output, its standard error after a `|`.
            fn run(command: Command!) -> Buffer! {
                match command.output() {
                    Result::Ok(captured) => {
                        let { exit, stdout, stderr } = captured;
                        stdout.push_static("|").push_buffer(stderr)
                    },
                    Result::Err(e) => f"error {e.errno}",
                }
            }
            fn main() -> i32 {
                var sr = StaticRead::new();
                let made = write(&sr, "input.txt", Buffer!::new().push_static("from a file"));
                let dir = mkdir(&sr, "sub");
                let marked = write(&sr, "sub/marker", Buffer!::new().push_static("here"));
                let sh = "sh";
                var out = run(Command!::new(&sr, sh).arg(&sr, "-c").arg(&sr, "printf %s \\"$KURSAL_A$KURSAL_B\\"; printf e >&2")
                    .env(&sr, "KURSAL_A", "set").env(&sr, "KURSAL_B", "gone").env_remove(&sr, "KURSAL_B"));
                out = out.push_static(",").push_buffer(run(Command!::new(&sr, "/usr/bin/env").env_clear().env(&sr, "ONLY", "1")));
                out = out.push_static(",").push_buffer(run(Command!::new(&sr, "cat").arg(&sr, "marker").current_dir(&sr, "sub")));
                let input = match File!::open_read(&sr, "input.txt") { Result::Ok(f) => Stdio!::File.(f), Result::Err(e) => Stdio!::Null };
                out = out.push_static(",").push_buffer(run(Command!::new(&sr, "cat").stdin(input)));
                out = out.push_static(",").push_buffer(run(Command!::new(&sr, "cat").stdin(Stdio!::Null)));
                out = out.push_static(",").push_buffer(match Command!::new(&sr, sh).arg(&sr, "-c").arg(&sr, "echo hidden").stdout(Stdio!::Null).spawn() {
                    Result::Ok(child) => {
                        var child = child;
                        let held = match &child.take_stdout() {
                            Option::Some(f) => { let closed = f.close(); "piped" },
                            Option::None => "null",
                        };
                        let ended = child.wait();
                        Buffer!::new().push_static(held)
                    },
                    Result::Err(e) => Buffer!::new(),
                });
                out = out.push_static(",").push_buffer(run(Command!::new(&sr, "true").env(&sr, "A=B", "x")));
                out = out.push_static(",").push_buffer(run(Command!::new(&sr, "true").current_dir(&sr, "no-such-directory")));
                // Into a pipe and out of another: the child reads to the end once this process closes its end.
                match Command!::new(&sr, "tr").arg(&sr, "a-z").arg(&sr, "A-Z").stdin(Stdio!::Pipe).stdout(Stdio!::Pipe).spawn() {
                    Result::Ok(child) => {
                        var child = child;
                        match &child.take_stdin() {
                            Option::Some(w) => {
                                let wrote = &w.write_static("piped");
                                let closed = w.close();
                            },
                            Option::None => (),
                        }
                        var piped = Buffer!::new();
                        match &child.take_stdout() {
                            Option::Some(r) => {
                                var r = r;
                                let n = &r.read_exact(&piped, 100usize);
                                let closed = r.close();
                            },
                            Option::None => (),
                        }
                        let ended = child.wait();
                        out = out.push_static(",").push_buffer(piped);
                    },
                    Result::Err(e) => (),
                }
                print_buffer(out);
                0
            }''', stdout="set|e,ONLY=1\n|,here|,from a file|,|,null,error 22,error 2,PIPED")

    def test_children_are_killed_and_signalled(self):
        self.expect('''
            use std::io::print_buffer;
            use std::option::Result;
            use std::process::{Command!, Exit, Polled!};
            use std::slice::StaticRead;
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
                print_buffer(f"{killed} {terminated}");
                0
            }''', stdout="109 115")

    def test_errors_describe_themselves(self):
        self.expect('''
            use std::io::{Error, ErrorKind, File!, print_buffer};
            use std::option::Result;
            use std::slice::StaticRead;
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
                        let message = e.message();
                        let n = &message.len();
                        print_buffer(f"{e}; {n} {kinds}");
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
        # `Instant::now` reads CLOCK_MONOTONIC through fs.c, which names the host's clock, so no clock
        # number (Linux's 1, FreeBSD's 4, macOS's 6) is written in Kursal: the time between two readings
        # around a sleep is the sleep's at least, and no more than the whole run's.
        started = time.monotonic_ns()
        result = self.run_program('''
            use std::time::{Duration, Instant, sleep};
            use std::io::print_buffer;
            fn main() -> i32 {
                let start = Instant::now();
                sleep(Duration::from_millis(30u64));
                let elapsed = Instant::now().since(start).as_nanos();
                let printed = print_buffer(f"{elapsed}");
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
            use std::slice::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                match File!::open_read(&sr, ".") {
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
            }''')

    def test_files_that_are_not_regular_are_read_to_their_end(self):
        # A pipe (/dev/stdin fed by one) and a device (/dev/null) have no size to read by: `fs::read` and
        # `File!::read_to_buffer` read them until they end. A regular file is read whole, a directory is
        # EISDIR.
        text = "line\n" * 20000
        self.expect('''
            use std::buffer::Buffer!;
            use std::fs::{read, write};
            use std::io::{Error, File!, print_buffer};
            use std::option::Result;
            use std::slice::StaticRead;
            fn length(r: Result[Buffer!, Error]) -> i32 {
                match r {
                    Result::Ok(b) => { let n = &b.len(); b.destroy(); n as i32 },
                    Result::Err(e) => 0i32 - e.errno,
                }
            }
            fn main() -> i32 {
                var sr = StaticRead::new();
                let none = length(read(&sr, "/dev/null"));
                let directory = length(read(&sr, "."));
                let written = write(&sr, "file.txt", Buffer!::new().push_static("12345")).is_ok();
                let regular = length(read(&sr, "file.txt"));
                let piped = match File!::open_read(&sr, "/dev/stdin") {
                    Result::Ok(file) => {
                        var file = file;
                        let n = length(&file.read_to_buffer());
                        let closed = file.close();
                        n
                    },
                    Result::Err(e) => 0i32 - e.errno,
                };
                let printed = print_buffer(f"{none} {directory} {regular} {piped}");
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
                    use std::slice::StaticRead;
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

    def test_files_are_read_in_chunks(self):
        # `read` appends at most its count, one `read(2)`, and 0 at the end; `read_exact` reads on after
        # a short read, fewer only at the end; a directory is EISDIR, and the buffer stays as it was.
        self.expect('''
            use std::buffer::Buffer!;
            use std::fs::write;
            use std::io::{File!, print_buffer};
            use std::option::Result;
            use std::slice::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                let made = write(&sr, "chunks.txt", Buffer!::new().push_static("abcdefgh"));
                var file = match File!::open_read(&sr, "chunks.txt") { Result::Ok(f) => f, Result::Err(e) => { return 1; } };
                var text = Buffer!::new_with_cap(0usize);
                let first = (&file.read(&text, 3usize)).unwrap_or(9usize);
                let none = (&file.read(&text, 0usize)).unwrap_or(9usize);
                let rest = (&file.read_exact(&text, 10usize)).unwrap_or(9usize);
                let end = (&file.read(&text, 3usize)).unwrap_or(9usize);
                let closed = file.close();
                let failed = match File!::open_read(&sr, ".") {
                    Result::Err(e) => 0i32,
                    Result::Ok(dir) => {
                        var dir = dir;
                        let read = &dir.read(&text, 4usize);
                        let closed = dir.close();
                        match read {
                            Result::Ok(n) => 0i32,
                            Result::Err(e) => e.errno,
                        }
                    },
                };
                print_buffer(f"{first} {none} {rest} {end} {failed} ".push_buffer(text));
                0
            }''', stdout="3 0 5 0 21 abcdefgh")

    def test_a_failed_write_is_an_error(self):
        self.expect('''
            use std::option::Result;
            use std::io::File!;
            use std::slice::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                match File!::create(&sr, "/dev/full") {
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
            }''')

    def test_print_and_eprint_write_to_stdout_and_stderr(self):
        result = self.run_program('''
            use std::arena::Domain!;
            use std::buffer::Buffer!;
            use std::io::{eprint, eprint_buffer, eprint_bytes, eprint_span, print, print_buffer, print_bytes, print_span};
            use std::raw::Span;
            use std::slice::StaticRead;
            fn main() {
                var read = StaticRead::new();
                let exists 'k. cx = Domain!::new();
                var cx = cx;
                print("a");
                print_bytes(&read, "b");
                print_buffer(Buffer!::new().push_static("c"));
                print_span(&cx, Span::from_static(&cx, "d"));
                eprint("A");
                eprint_bytes(&read, "B");
                eprint_buffer(Buffer!::new().push_static("C"));
                eprint_span(&cx, Span::from_static(&cx, "D"));
                cx.destroy();
            }''')
        self.assertEqual((result.stdout, result.stderr, result.returncode), ("abcd", "ABCD", 0))

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
                use std::slice::StaticRead;
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
        # it creates takes another number: `print`, `eprint` and `read_line` work and reach /dev/null, and
        # the file holds only what was written to it.
        def close_standard_streams() -> None:
            for fd in (0, 1, 2):
                os.close(fd)
        self.expect('''
            use std::io::{File!, eprint, print, read_line};
            use std::option::{Option, Result};
            use std::slice::StaticRead;
            fn main() -> i32 {
                var sr = StaticRead::new();
                let file = match File!::create(&sr, "owned.txt") {
                    Result::Ok(f) => f,
                    Result::Err(e) => return 1,
                };
                var file = file;
                let written = &file.write_static("the file's own\\n");
                let printed = print("stdout\\n").is_ok() && eprint("stderr\\n").is_ok();
                let line = match read_line() {
                    Result::Ok(found) => match found {
                        Option::Some(text) => { text.destroy(); 4 },
                        Option::None => 0,
                    },
                    Result::Err(e) => 5,
                };
                let closed = file.close().is_ok();
                let reader = match File!::open_read(&sr, "owned.txt") {
                    Result::Ok(f) => f,
                    Result::Err(e) => return 6,
                };
                var reader = reader;
                let length = match &reader.read_to_end() {
                    Result::Ok(text) => { let n = &text.len(); text.destroy(); n },
                    Result::Err(e) => 0usize,
                };
                let shut = reader.close();
                if !written.is_ok() || !closed { 2 } else if !printed { 3 } else if line != 0 { line } else if length != 15usize { 7 } else { 0 }
            }''', preexec=close_standard_streams)

    def test_read_line_tells_an_empty_line_from_the_end(self):
        self.expect('''
            use std::io::{read_line, print_buffer};
            use std::buffer::Buffer!;
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
                                print_buffer(Buffer!::new().push_byte(b'[').push_buffer(text).push_byte(b']'));
                            },
                        },
                    }
                }
                lines
            }''', status=3, stdout="[a][][b]", stdin="a\n\nb")

    def test_input_reads_lines_ahead_and_loses_no_byte(self):
        # An `Input!` returns lines as `read_line` does; while it holds standard input, the other readers
        # of it fail with EBUSY (16) rather than skip what it read ahead; `read_to_end` returns the rest,
        # and `finish` hands back what was read ahead and not returned, and standard input with it.
        self.expect('''
            use std::buffer::Buffer!;
            use std::io::{Error, Input!, print_buffer, read_all_stdin, read_line, stdin};
            use std::process::trap;
            use std::option::{Option, Result};
            fn shown(&out: Buffer!, line: Result[Option[Buffer!], Error]) {
                out = match line {
                    Result::Ok(found) => match found {
                        Option::Some(text) => out.push_byte(b'[').push_buffer(text).push_byte(b']'),
                        Option::None => out.push_static("end"),
                    },
                    Result::Err(e) => out.push_static("error ").push_i64(e.errno as i64),
                };
            }
            fn errno(r: Result[Input!, Error]) -> i32 {
                match r {
                    Result::Ok(input) => { input.finish().destroy(); 0i32 },
                    Result::Err(e) => e.errno,
                }
            }
            fn main() -> i32 {
                var out = Buffer!::new();
                var input = match stdin() { Result::Ok(i) => i, Result::Err(e) => trap(), };
                shown(&out, &input.read_line());
                shown(&out, &input.read_line());
                shown(&out, read_line());
                let again = errno(stdin());
                let all_busy = match read_all_stdin() {
                    Result::Ok(text) => { text.destroy(); false },
                    Result::Err(e) => e.errno == 16i32,
                };
                shown(&out, &input.read_line());
                let ahead = input.finish();
                out = out.push_static(" ahead ").push_buffer(ahead);
                var second = match stdin() { Result::Ok(i) => i, Result::Err(e) => trap(), };
                shown(&out, &second.read_line());
                second.finish().destroy();
                var third = match stdin() { Result::Ok(i) => i, Result::Err(e) => trap(), };
                let none = match &third.read_to_end() { Result::Ok(text) => { let n = &text.len(); text.destroy(); n }, Result::Err(e) => 9usize };
                shown(&out, &third.read_line());
                third.finish().destroy();
                let printed = print_buffer(out);
                if again == 16i32 && all_busy && none == 0usize { 0 } else { 4 }
            }''', stdout="[a][]error 16[b] ahead c\nlastendend", stdin="a\n\nb\nc\nlast")

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
                input.finish().destroy();
                let reads = unsafe probe_reads();
                if lines != 20002u64 || longest != 100000usize { 2 } else if reads > 64u64 { 3 } else { 0 }
            }''', stdin="\n".join(lines), counted=True)

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
            use std::buffer::Buffer!;
            fn main() -> i32 {
                let b = Buffer!::new_with_cap(0usize).push_byte(b'a').push_static("bc");
                let n = &b.len();
                b.destroy();
                let z = Buffer!::new_zeroed(0usize);
                let c = &z.clone();
                var c = c.push_byte(b'x');
                let m = &c.len();
                z.destroy();
                c.destroy();
                if n == 3usize && m == 1usize { 0 } else { 1 }
            }''')

    def test_split_on_an_empty_separator_is_the_whole_span(self):
        self.expect('''
            use std::arena::Domain!;
            use std::raw::Span;
            fn main() -> i32 {
                let exists 'k. cx = Domain!::new();
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
            use std::arena::Domain!;
            use std::collections::arena_map::ArenaMap;
            use std::option::Option;
            fn is(o: Option[u32], v: u32) -> bool {
                match o { Option::Some(x) => x == v, Option::None => false }
            }
            fn main() -> i32 {
                let exists 'k. cx = Domain!::new();
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
                let exists 'k. cx = Domain!::new();
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
                var map = OwnedMap!::new[Key, u32]();
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
                map.destroy(drop_entry);
                if replaced && removed == 3i32 && kept && present && n == 2997usize { 0 } else { 1 }
            }''')

    def test_long_lists_and_scans_take_no_stack(self):
        self.expect('''
            use std::arena::Domain!;
            use std::collections::list::List;
            use std::buffer::Buffer!;
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
                let exists 'k. cx = Domain!::new();
                let listed = lists(&cx);
                cx.destroy();
                var b = Buffer!::new();
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
        use std::raw::Span;
        use std::buffer::Buffer!;
        fn main() -> i32 {
            let a = Option::Some.(3u32).unwrap();
            let r: Result[u32, u32] = Result::Ok.(4u32);
            let b = r.unwrap();
            let exists 'b. (tag, bytes, restore) = Buffer!::new().push_static("pq").open();
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


    # ------------------------------------------------------------ core API --

    def test_core_helpers(self):
        self.expect('''
            use std::cmp::{Order, min, max};
            use std::hash::{Hash, Eq, HashState};
            use std::mem::{alignof, swap};
            use std::option::{Option, Result};
            use std::ptr::{null, is_null, offset};
            use std::slice::{Slice, StaticRead};
            use std::utf8::{encode, validate, width};
            use std::buffer::Buffer!;
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
                let (t, h1) = Hash[(u32, u64)]::hash((1u32, 2u64), HashState::new());
                let (u, h2) = Hash[(u32, u64)]::hash((1u32, 2u64), HashState::new());
                let (x, y, same) = Eq[(u32, u64), (u32, u64)]::eq((1u32, 2u64), (1u32, 2u64));
                let (x2, y2, diff) = Eq[(u32, u32, u32), (u32, u32, u32)]::eq((1u32, 2u32, 3u32), (1u32, 2u32, 4u32));
                let tuples = same && !diff && h1.finish() == h2.finish();
                let good = Buffer!::new().push_byte(104u8).push_byte(195u8).push_byte(169u8);
                let bad = Buffer!::new().push_byte(237u8).push_byte(160u8).push_byte(128u8);
                let exists 'g. (gt, gs, gr) = good.open();
                let exists 'b. (bt, bs, br) = bad.open();
                var text = encode(encode(encode(Buffer!::new(), 233u32), 128512u32), 55296u32);
                let n = &text.len();
                text.destroy();
                let utf8 = validate(&gt, gs) && !validate(&bt, bs) && width(55296u32) == 0usize && width(128u32) == 2usize
                    && width(1114112u32) == 0usize && n == 2usize + 4usize + 3usize;
                gr.restore(gt).destroy();
                br.restore(bt).destroy();
                let empty: Slice[u8, 'static] = "";
                let slices = empty.is_empty() && !"a".is_empty();
                let store = Buffer!::new_zeroed(12usize);
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
            use std::raw::Span;
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
                let name = Span::from_static(&cx, "world");
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
                let exists 'k. cx = Domain!::new();
                let (cx, ok) = run(cx);
                cx.destroy();
                if ok { 0 } else { 1 }
            }''')

    def test_cursor_reads_through_any_reader(self):
        self.expect('''
            use std::cursor::Cursor;
            use std::slice::StaticRead;
            use std::ascii::is_alpha;
            fn main() -> i32 {
                let sr = StaticRead::new();
                let cur = Cursor::new("abc1");
                let (cur, word) = cur.take_matching(&sr, is_alpha);
                if word.len() == 3usize && cur.peek(&sr).unwrap_or(0u8) == b'1' && cur.starts_with(&sr, "1") { 0 } else { 1 }
            }''')

    def test_split_and_merge_round_trip(self):
        self.expect('''
            use std::owned::Owned!;
            fn main() -> i32 {
                let exists 'r. (alloc, region) = Owned!::new((10u32, 20u32)).open();
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

    def test_readers_of_a_shared_tree_use_slices_and_spans(self):
        # A `Shared!` node, not the `Exclusive!`, reads a slice's element through `get_ref` and a span's and
        # a byte slice's bytes through their read methods; the writer comes back once the tree is joined.
        self.expect('''
            use std::buffer::Buffer!;
            use std::raw::Span;
            fn main() -> i32 {
                let exists 'b. (tag, bytes, restore) = Buffer!::new().push_static("  key=value \\n").open();
                let exists 'q. (root, back) = tag.share();
                let exists 'l, 'r. (left, right, join) = root.split();
                let second = match bytes.get_ref(2usize) {
                    std::option::Option::Some(at) => at.read(&left),
                    std::option::Option::None => 0u8,
                };
                let text = Span::from_slice(bytes).trim(&left);
                let key = text.take(text.find_byte(&right, b'=', 0usize).unwrap_or(0usize));
                let spans = key.is(&left, "key") && text.ends_with(&right, Span::from_slice(bytes).slice(6usize, 11usize))
                    && text.get(&left, 0usize).unwrap_or(0u8) == b'k' && text.rfind_byte(&right, b'e').unwrap_or(0usize) == 8usize
                    && text.cmp(&left, key).is_gt() && !text.eq(&right, key) && text.contains(&left, key);
                let slices = bytes.trim(&right).is(&left, "key=value") && bytes.trim_start(&left).len() == 11usize
                    && bytes.trim_end(&right).len() == 11usize && bytes.skip(2usize).starts_with(&left, bytes.slice(2usize, 5usize))
                    && bytes.find(&right, bytes.slice(6usize, 8usize), 0usize).unwrap_or(0usize) == 6usize
                    && bytes.rfind_byte(&left, b' ').unwrap_or(0usize) == 11usize && bytes.byte_or_zero(&right, 99usize) == 0u8;
                let tag = back.resume(join.join(left, right));
                let written = match bytes.get_ref(0usize) {
                    std::option::Option::Some(at) => { at.set(&tag, b'#'); at.copy(&tag) },
                    std::option::Option::None => 0u8,
                };
                restore.restore(tag).destroy();
                if second == b'k' && spans && slices && written == b'#' { 0 } else { 1 }
            }''')

    def test_readers_of_a_shared_tree_cannot_write(self):
        self.reject('''
            use std::buffer::Buffer!;
            fn main() -> i32 {
                let exists 'b. (tag, bytes, restore) = Buffer!::new().push_static("ab").open();
                let exists 'q. (root, back) = tag.share();
                match bytes.get_ref(0usize) {
                    std::option::Option::Some(at) => at.set(&root, b'#'),
                    std::option::Option::None => (),
                }
                restore.restore(back.resume(root)).destroy();
                0
            }''', "no instance of Access")

    def test_byte_slices_and_spans_of_the_same_bytes_hash_alike(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::raw::Span;
            use std::hash::{HashIn, HashState, key_hash};
            use std::cap::Exclusive!;
            use std::slice::{Slice, StaticRead};
            fn main() -> i32 {
                let exists 'b. (tag, bytes, restore) = Buffer!::new().push_static("name").open();
                let span = Span::from_slice(bytes);
                let sr = StaticRead::new();
                let literal: Slice[u8, 'static] = "name";
                let same = span.hash(&tag) == bytes.hash(&tag) && bytes.hash(&tag) == literal.hash(&sr)
                    && key_hash(&tag, span) == key_hash(&tag, bytes) && key_hash(&tag, bytes) == key_hash(&sr, literal)
                    && HashIn[Span['b], Exclusive!['b]]::hash_in(&tag, span, HashState::new()).finish() == span.hash(&tag);
                restore.restore(tag).destroy();
                if same { 0 } else { 1 }
            }''')

    def test_c_strings_read_their_bytes_safely(self):
        self.expect('''
            use std::process::Args;
            use std::slice::StaticRead;
            use std::option::Option;
            fn main(args: Args) -> i32 {
                let sr = StaticRead::new();
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
                    v = v.push((i % 3u32, i));
                    i = i + 1u32;
                }
                var none = ();
                v = v.sort_by(&none, |&n, a: (u32, u32), b: (u32, u32)| by_first(a, b));
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
                v.free();
                var w = OwnedVec!::new[u32]().push(5u32).push(1u32).push(4u32).sort();
                var u = OwnedVec!::new[u32]().push(5u32).push(1u32).push(4u32).sort_unstable();
                let sorted = &w.at(0usize) == 1u32 && &w.at(2usize) == 5u32 && &u.at(1usize) == 4u32;
                w.free();
                u.free();
                if stable && set && truncated && cleared && sorted { 0 } else { 1 }
            }''')

    def test_vector_at_stops_on_a_miss(self):
        result = self.run_program('''
            use std::collections::vec::OwnedVec!;
            fn main() -> i32 {
                var v = OwnedVec!::new[u32]().push(1u32);
                let x = &v.at(1usize);
                v.free();
                0
            }''')
        self.assertLess(result.returncode, 0)

    def test_owned_maps_look_up_by_place(self):
        self.expect('''
            use std::collections::hashmap::{HashMap!, OwnedMap!};
            use std::option::Option;
            // `HashMap::find`, with a probe of the key type.
            fn find(&named: OwnedMap![u32, u32], &probe: u32) -> bool {
                let exists 'm. (tag, map) = named.inner;
                let found = match &map.find(&tag, &probe) { Option::Some(r) => r.copy(&tag) == 11u32, Option::None => false };
                named = OwnedMap!.{ inner: pack 'm. (tag, map) };
                found
            }
            fn main() -> i32 {
                var m = OwnedMap!::new[u32, u32]();
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
                m.free();
                if replaced && looked && found && with && removed && empty && cleared { 0 } else { 1 }
            }''')

    def test_owned_maps_are_visited_cleared_and_searched_by_a_probe(self):
        # Counting the words of a text in a map keyed by `Buffer!`: a word seen before is found through a
        # view of the text, with no key built for it; the map is visited in place, twice, and cleared,
        # keeping its room.
        self.expect('''
            use std::collections::hashmap::OwnedMap!;
            use std::buffer::Buffer!;
            use std::cap::Exclusive!;
            use std::slice::{Readable, Slice};
            use std::ascii::word_at;
            use std::option::Option;
            forall 'b.
            fn count(&m: OwnedMap![Buffer!, u64], &tag: Exclusive!['b], word: Slice[u8, 'b]) {
                var probe = Readable.{ cap: tag, bytes: word };
                let bumped = &m.find_update(&probe, |&n| { n = n + 1u64; });
                let { cap, bytes } = probe;
                tag = cap;
                if bumped.is_none() {
                    match &m.insert(Buffer!::new().push_bytes(&tag, word), 1u64) {
                        Option::Some(old) => { let (k, v) = old; k.destroy(); },
                        Option::None => (),
                    }
                }
            }
            fn drop_entry(k: Buffer!, v: u64) { k.destroy(); }
            fn main() -> i32 {
                var m = OwnedMap!::new[Buffer!, u64]();
                let exists 'b. (tag, text, restore) = Buffer!::new().push_static("to be or not to be again").open();
                var tag = tag;
                var at = 0usize;
                var words = 0usize;
                while words < 6usize {
                    match word_at(&tag, text, at) {
                        Option::Some(found) => {
                            let (word, next) = found;
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
                        out = out.push_buffer(&word.clone()).push_static("=").push_u64(n);
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
                m.destroy(drop_entry);
                let (sum, letters) = total;
                let exists 's. (stag, sbytes, srestore) = shown.open();
                let one = sbytes.is(&stag, "to=104");
                srestore.restore(stag).destroy();
                if to == 2u64 && doubled == 2u64 && sum == 8u64 && letters == 9usize && one && before == 4usize && cleared && after == 1usize { 0 } else { 1 }
            }''')

    def test_maps_of_copy_entries_clear_in_place(self):
        self.expect('''
            use std::collections::hashmap::OwnedMap!;
            fn main() -> i32 {
                var m = OwnedMap!::new[u32, u32]();
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
                m.free();
                if empty && found { 0 } else { 1 }
            }''')

    def test_vectors_insert_remove_swap_and_reverse(self):
        # Linear elements move: `insert` and `remove` shift the rest, `swap` and `reverse` exchange them.
        self.expect('''
            use std::collections::vec::{OwnedVec!, Vec!};
            use std::buffer::Buffer!;
            use std::option::Option;
            fn label(n: u64) -> Buffer! { Buffer!::new().push_u64(n) }
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
                    Option::Some(b) => { let n = &b.len(); b.destroy(); n },
                    Option::None => 9usize,
                };
                let missed = match &v.remove(7usize) {
                    Option::Some(b) => { b.destroy(); false },
                    Option::None => true,
                };
                var shown = Buffer!::new();
                while !&v.is_empty() {
                    match &v.pop() {
                        Option::Some(b) => { shown = shown.push_buffer(b); },
                        Option::None => (),
                    }
                }
                v.destroy(drop);
                var n = OwnedVec!::new[u32]().push(5u32).push(6u32).push(7u32);
                let copies = (&n.first()).unwrap_or(0u32) == 5u32 && &n.contains(6u32) && !&n.contains(8u32);
                n.free();
                let exists 'k. (tag, raw) = Vec!::new[u32]();
                let exists 'k2. (tag, raw) = raw.push(tag, 4u32);
                let branded = (&raw.get(&tag, 0usize)).unwrap_or(0u32) == 4u32 && &raw.at(&tag, 0usize) == 4u32 && (&raw.get(&tag, 1usize)).is_none();
                raw.free(tag);
                let exists 'b. (btag, bytes, restore) = shown.open();
                let order = bytes.is(&btag, "310");
                restore.restore(btag).destroy();
                if taken == 1usize && missed && order && copies && branded { 0 } else { 1 }
            }''')

    def test_vector_insert_past_the_end_stops(self):
        result = self.run_program('''
            use std::collections::vec::OwnedVec!;
            fn main() -> i32 {
                var v = OwnedVec!::new[u32]();
                &v.insert(1usize, 5u32);
                v.free();
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
                let exists 'm. (tag, map) = named.inner;
                let found = (&map.find(&tag, &probe)).is_some();
                named = OwnedMap!.{ inner: pack 'm. (tag, map) };
                found
            }
            fn main() -> i32 {
                var m = OwnedMap!::new[u64, u64]();
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
                m.free();
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
                let c = m.upsert(&cx, 1u32, 0u32, |&cx, v: u32| v + 5u32);
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
                let exists 'k. cx = Domain!::new();
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
                let exists 'k. cx = Domain!::new();
                let ok = check(&cx);
                cx.destroy();
                if ok { 0 } else { 1 }
            }''')

    def test_arena_vec_push_span_of_its_own_bytes(self):
        # `span` views the vec's first segment; pushing that view back after a `truncate` copies from
        # bytes the push itself overwrites.
        self.expect('''
            use std::arena::Domain!;
            use std::collections::arena_vec::ArenaVec;
            forall 'k.
            fn check(&cx: Domain!['k]) -> bool {
                let v: ArenaVec[u8, 'k] = ArenaVec::new(&cx);
                v.push_static(&cx, "abcd");
                let s = v.span(&cx);
                v.truncate(&cx, 1usize);
                v.push_span(&cx, s);
                let grown = v.span(&cx).is(&cx, "aabcd");
                let t = v.span(&cx);
                v.truncate(&cx, 0usize);
                v.push_span(&cx, t.slice(1usize, 4usize));
                grown && v.span(&cx).is(&cx, "abc")
            }
            fn main() -> i32 {
                let exists 'k. cx = Domain!::new();
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
                let exists 'k. cx = Domain!::new();
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
                let exists 'k. cx = Domain!::new();
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
            fn main() -> i32 {
                let b = Buffer!::new().push_static("hello").truncate(2usize).push_static("y");
                let n = &b.len();
                let b = b.truncate(10usize);
                let m = &b.len();
                let b = b.clear();
                let z = &b.len();
                b.destroy();
                if n == 3usize && m == 3usize && z == 0usize { 0 } else { 1 }
            }''')

if __name__ == "__main__":
    unittest.main()
