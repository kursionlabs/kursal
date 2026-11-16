"""The `toml` package at run time, in the manner of toml-test: a decoder built with it reads documents and
prints them as tagged JSON, which must equal what Python's `tomllib` reads; a document `tomllib` rejects, it
must reject on the same line. Some errors are also checked at their byte offset and column."""

import json
import math
import struct
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, ClassVar, Optional, cast

from kursal.cc import cc_command
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sigs import signatures

MANIFEST = '[package]\nname="t"\nkind="executable"\nentry="package::main"\nsource_root="."\n'
C_HELPERS = [LIB / "libc/fs.c", LIB / "libc/thread.c"]

# Reads a document on stdin. Valid: prints its root table as toml-test's tagged JSON (a float as its bits,
# a date or time as its text) and exits 0. Invalid: prints `OFFSET LINE COLUMN MESSAGE` and exits 1.
DECODER = r'''
use std::alloc::arena::Domain;
use std::bytes::buffer::Buffer;
use std::collections::arena_map::ArenaMap;
use std::io::{print_buffer, read_all_stdin};
use std::option::{Option, Result};
use std::raw::Span;
use toml::parse;
use toml::value::{DatetimeKind, TomlValue, line_column};

fn hex(d: u8) -> u8 {
    if d < 10u8 { b'0' + d } else { b'a' + d - 10u8 }
}

forall 'k.
fn string(&cx: Domain!['k], out: Buffer!, s: Span['k]) -> Buffer! {
    var out = out.push_byte(b'"');
    var i = 0usize;
    while i < s.len() {
        let b = s.byte_or_zero(&cx, i);
        if b == b'"' || b == b'\\' {
            out = out.push_byte(b'\\').push_byte(b);
        } else if b < 32u8 || b == 127u8 {
            out = out.push_static("\\u00").push_byte(hex(b / 16u8)).push_byte(hex(b % 16u8));
        } else {
            out = out.push_byte(b);
        }
        i = i + 1usize;
    }
    out.push_byte(b'"')
}

forall 'k.
fn value(&cx: Domain!['k], out: Buffer!, v: TomlValue['k]) -> Buffer! {
    match v {
        TomlValue::String(s) => string(&cx, out.push_static("{\"type\":\"string\",\"value\":"), s).push_static("}"),
        TomlValue::Integer(n) => out.push_static("{\"type\":\"integer\",\"value\":\"").push_i64(n).push_static("\"}"),
        TomlValue::Float(x) => out.push_static("{\"type\":\"float\",\"value\":\"").push_u64(x.to_bits()).push_static("\"}"),
        TomlValue::Boolean(b) => out.push_static("{\"type\":\"bool\",\"value\":\"").push_static(if b { "true" } else { "false" }).push_static("\"}"),
        TomlValue::Datetime(d) => {
            let kind = match d.kind {
                DatetimeKind::OffsetDatetime => "datetime",
                DatetimeKind::LocalDatetime => "datetime-local",
                DatetimeKind::LocalDate => "date-local",
                DatetimeKind::LocalTime => "time-local",
            };
            string(&cx, out.push_static("{\"type\":\"").push_static(kind).push_static("\",\"value\":"), d.text).push_static("}")
        },
        TomlValue::Array(a) => {
            var o = out.push_static("[");
            var i = 0usize;
            while i < a.len(&cx) {
                if i > 0usize {
                    o = o.push_static(",");
                }
                o = value(&cx, o, a.at(&cx, i));
                i = i + 1usize;
            }
            o.push_static("]")
        },
        TomlValue::Table(t) => table(&cx, out, t),
    }
}

forall 'k.
fn table(&cx: Domain!['k], out: Buffer!, t: ArenaMap[Span['k], TomlValue['k], 'k]) -> Buffer! {
    var o = out.push_static("{");
    var first = true;
    var i = 0usize;
    while i < t.positions(&cx) {
        match t.key_at(&cx, i) {
            Option::Some(k) => {
                if !first {
                    o = o.push_static(",");
                }
                first = false;
                o = string(&cx, o, k).push_static(":");
                o = value(&cx, o, t.at(&cx, k));
            },
            Option::None => (),
        }
        i = i + 1usize;
    }
    o.push_static("}")
}

fn main() -> i32 {
    let exists 'k. cx = Domain::new();
    var cx = cx;
    let input = match read_all_stdin() {
        Result::Ok(b) => b,
        Result::Err(e) => Buffer::new(),
    };
    let src = input.save(&cx);
    let status = match parse(&cx, src) {
        Result::Ok(root) => {
            print_buffer(1i32, table(&cx, Buffer::new(), root));
            0
        },
        Result::Err(e) => {
            let (line, column) = line_column(&cx, src, e.offset);
            print_buffer(1i32, Buffer::new().push_usize(e.offset).push_static(" ").push_usize(line).push_static(" ")
                .push_usize(column).push_static(" ").push_static(e.message));
            1
        },
    };
    cx.destroy();
    status
}
'''

# Documents tomllib reads.
VALID = (
    "",
    "# only a comment\n",
    "a = 1\n",
    'a = "x" # comment\r\nb = \'lit\'\r\n',
    "a = 'C:\\path'\nb = \"tab\\there\\n\\u00e9\\U0001F600\\\\ \\\" \\b\\f\\r\"\n",
    'a = """\nline one\nline two"""\nb = """one \\\n    two \\\n\n  three"""\nc = """a\\  \n  b"""\n',
    "a = '''\nraw \\n text'''\nb = '''quotes '' ''''\nc = \"\"\"x\"\"\"\"\"\nd = \"\"\"\"\"\"\n",
    'a = """\r\ncrlf\r\nlines"""\nb = \'\'\'\r\nx\r\ny\'\'\'\n',
    "int = [0, +1, -1, 1_000, 9223372036854775807, -9223372036854775808, 0xDEAD_beef, 0o755, 0b1101, -0, +0]\n",
    "f = [1.0, -2.5, 3e10, 1E-5, 6.02e+23, 1_000.000_1, -0.0, +0.0, inf, +inf, -inf, nan, +nan, -nan, 1e400]\n",
    "t = true\nf = false\n",
    "odt = [1979-05-27T07:32:00Z, 1979-05-27T00:32:00-07:00, 1979-05-27T00:32:00.999999+07:30, 1979-05-27 07:32:00z]\n"
    "ldt = [1979-05-27T07:32:00, 1979-05-27t00:32:00.999999999]\nld = 2024-02-29\nlt = [07:32:00, 00:32:00.5]\n",
    "d = 1979-05-27 # a date, then a comment\n",
    "a = []\nb = [ 1, 2, ]\nc = [\n  1, # one\n  [2, 'two'],\n  { x = 1 },\n]\nd = [[], [[]]]\n",
    "t = {}\nu = { a = 1, b.c = 'x', 'q k' = true }\nv = {x={y={z=1}}}\n",
    "[a]\nx = 1\n[b]\ny = 2\n[a.c]\nz = 3\n",
    "[a.b.c]\nx = 1\n[a]\ny = 2\n",
    "[ a . 'b' . \"c\" ]\nx = 1\n",
    "a.b.c = 1\na.b.d = 2\na.e = 3\n",
    "[t]\nx.y = 1\nx.z = 2\n[t.u]\n",
    "[[fruit]]\nname = 'apple'\n[fruit.physical]\ncolor = 'red'\n[[fruit.variety]]\nname = 'red delicious'\n"
    "[[fruit.variety]]\nname = 'granny smith'\n[[fruit]]\nname = 'banana'\n[[fruit.variety]]\nname = 'plantain'\n",
    "[[a]]\n[[a]]\nb = 1\n[a.c]\nd = 2\n",
    "\"\" = 1\n'a b' = 2\n\"a.b\" = 3\n1234 = 4\n-_ = 5\n",
    "a = 'é ü ✓'\n# comment é\n",
    "x = 1\n\n\n   \t\ny = 2",
    "[dependencies]\nlib = { path = \"../lib\" }\n[package]\nname = \"p\"\n",
    "a = \"\"\"\\u0041\\\n   B\"\"\"\n",
    "key = \"value\"   \t# trailing\n[  t  ]  # comment\n",
)

# Documents tomllib rejects.
INVALID = (
    "a = \n",
    "a = 1\na = 2\n",
    "[a]\n[a]\n",
    "a = {}\n[a]\n",
    "a = {b = 1}\n[a]\n",
    "a.b = 1\n[a]\n",
    "a.b.c = 1\n[a.b]\n",
    "[a]\nb.c = 1\n[a.b]\n",
    "[a.b]\n[a]\nb.c = 1\n",
    "a = 1\n[a]\n",
    "a = [1]\n[[a]]\n",
    "[a]\n[[a]]\n",
    "[[a]]\n[a]\n",
    "a = {b = 1}\na.c = 2\n",
    "a = {b = {c = 1}, b.d = 2}\n",
    "a = {b = 1, b = 2}\n",
    "a = {b = 1,}\n",
    "a = {b = 1\n}\n",
    "a = [1, 2\n",
    "a = [1 2]\n",
    "a = \"unterminated\n",
    "a = 'unterminated\n",
    "a = \"\"\"never closed\n",
    "a = '''never closed\n",
    "a = \"bad \\q escape\"\n",
    "a = \"\\u12\"\n",
    "a = \"\\uD800\"\n",
    "a = \"tab\ton purpose\x01\"\n",
    "a = 1 b = 2\n",
    "a = 1 # comment \x7f here\n",
    "= 1\n",
    "a b = 1\n",
    "a. = 1\n",
    "[a\n",
    "[a]]\n",
    "[[a]\n",
    "[]\n",
    "a = 01\n",
    "a = 1__0\n",
    "a = 1.\n",
    "a = .5\n",
    "a = 1e\n",
    "a = +0x10\n",
    "a = 0x\n",
    "a = 2023-02-29\n",
    "a = 2023-13-01\n",
    "a = 0000-01-01\n",
    "a = 07:32\n",
    "a = 24:00:00\n",
    "a = truth\n",
    "a = True\n",
    "a = infinity\n",
    "a = 1\rb = 2\n",
    "\x00 = 1\n",
    "a = \"x\"\n\n\n[b\n",
    "[a]\nb = 1\n[a]\nc = 2\n",
    "a = '''x''''''\n",
)

# (document, byte offset, line, column) of an error; the integers out of range of `i64`, which tomllib reads
# as Python's integers, among them.
OFFSETS = (
    ("a = ", 4, 1, 5),
    ("a = 1\na = 2", 11, 2, 6),
    ("[a]\n[a]\n", 6, 2, 3),
    ("a = \"open", 9, 1, 10),
    ("x = 1\ny = [1,\n2", 15, 3, 2),
    ("é = 1\n", 0, 1, 1),
    ("k = 'é'\nv = \"\\x\"", 16, 2, 8),
    ("a = 9223372036854775808\n", 4, 1, 5),
    ("a = -9223372036854775809\n", 4, 1, 5),
    ("a = 0x8000000000000000\n", 4, 1, 5),
    ("a = 1 # \x01\n", 8, 1, 9),
)


def tagged(value: object) -> object:
    """`value`, read by tomllib, as the decoder prints it."""
    if isinstance(value, dict):
        return [(k, tagged(v)) for k, v in cast(dict[str, object], value).items()]
    if isinstance(value, list):
        return [tagged(v) for v in cast(list[object], value)]
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, bool):
        return {"type": "bool", "value": "true" if value else "false"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": str(struct.unpack("<Q", struct.pack("<d", value))[0])}
    if isinstance(value, datetime):
        return {"type": "datetime" if value.tzinfo else "datetime-local", "value": value}
    if isinstance(value, date):
        return {"type": "date-local", "value": value}
    assert isinstance(value, time)
    return {"type": "time-local", "value": value}


def pairs(text: str) -> object:
    """The decoder's JSON, its objects as lists of pairs (the order is checked) and its dates as values."""
    def hook(items: list[tuple[str, Any]]) -> object:
        found = dict(items)
        if set(found) == {"type", "value"} and found["type"] in ("datetime", "datetime-local", "date-local", "time-local"):
            return {"type": found["type"], "value": tomllib.loads("x = " + found["value"])["x"]}
        if set(found) == {"type", "value"}:
            return found
        return items
    return json.loads(text, object_pairs_hook=hook)


class TomlTests(unittest.TestCase):
    decoder: ClassVar[Optional[Path]] = None
    base: ClassVar[tempfile.TemporaryDirectory[str]]

    @classmethod
    def setUpClass(cls) -> None:
        sys.setrecursionlimit(20000)
        cls.base = tempfile.TemporaryDirectory(prefix="kursal-toml-")
        directory = Path(cls.base.name)
        deps = "".join(f'[dependencies.{d}]\npath="{LIB / d}"\n' for d in ("std", "toml"))
        (directory / "kur.toml").write_text(MANIFEST + deps)
        (directory / "lib.kur").write_text(DECODER)
        resolver = resolve(directory)
        sigs = signatures(resolver)
        program = lower(resolver, sigs)
        c_file = directory / "main.c"
        c_file.write_text(emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl)))
        executable = directory / "decoder"
        built = subprocess.run(cc_command([str(c_file), *map(str, C_HELPERS)], str(executable)), capture_output=True, text=True)
        assert built.returncode == 0, built.stderr[:500]
        cls.decoder = executable

    @classmethod
    def tearDownClass(cls) -> None:
        cls.base.cleanup()

    def decode(self, document: str) -> subprocess.CompletedProcess[bytes]:
        assert self.decoder is not None
        return subprocess.run([str(self.decoder)], input=document.encode(), capture_output=True, timeout=60)

    def test_valid_documents_read_as_tomllib_reads_them(self):
        for document in VALID:
            with self.subTest(document=document):
                result = self.decode(document)
                self.assertEqual(result.returncode, 0, result.stdout)
                expected = tagged(tomllib.loads(document))
                self.assertEqual(pairs(result.stdout.decode()), expected)

    def test_invalid_documents_fail_on_tomllib_line(self):
        for document in INVALID:
            with self.subTest(document=document):
                with self.assertRaises(tomllib.TOMLDecodeError) as raised:
                    tomllib.loads(document)
                result = self.decode(document)
                self.assertEqual(result.returncode, 1, result.stdout)
                offset, line, column, message = result.stdout.decode().split(" ", 3)
                self.assertTrue(message)
                self.assertEqual(int(line), tomllib_line(document, str(raised.exception)))

    def test_errors_have_offsets_lines_and_columns(self):
        for document, offset, line, column in OFFSETS:
            with self.subTest(document=document):
                result = self.decode(document)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout.decode().split(" ")[:3], [str(offset), str(line), str(column)])

    def test_invalid_utf8_is_an_error(self):
        assert self.decoder is not None
        result = subprocess.run([str(self.decoder)], input=b"a = 'x\xff'\n", capture_output=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.split(b" ")[:2]), (1, [b"6", b"1"]))

    def test_nan_keeps_its_sign(self):
        result = self.decode("a = -nan\nb = nan\n")
        found = cast(list[tuple[str, dict[str, str]]], pairs(result.stdout.decode()))
        bits = [int(v["value"]) for _, v in found]
        self.assertTrue(all(math.isnan(struct.unpack("<d", struct.pack("<Q", b))[0]) for b in bits))
        self.assertEqual([b >> 63 for b in bits], [1, 0])

    def test_a_large_document_reads_in_linear_time(self):
        # Many tables, each defined by a header and many dotted keys: quadratic definition checks would
        # take minutes.
        document = "".join(f"[t{i}]\n" + "".join(f"k{j}.x = {j}\n" for j in range(20)) for i in range(2000))
        result = self.decode(document)
        self.assertEqual(result.returncode, 0, result.stdout[:200])
        self.assertEqual(len(cast(list[object], pairs(result.stdout.decode()))), 2000)


def tomllib_line(document: str, message: str) -> int:
    """The line of a `TOMLDecodeError`'s message: `(at line N, column M)`, or the last line at the end."""
    if message.endswith("(at end of document)"):
        return document.replace("\r\n", "\n").count("\n") + 1
    return int(message.rsplit("(at line ", 1)[1].split(",")[0])


if __name__ == "__main__":
    unittest.main()
