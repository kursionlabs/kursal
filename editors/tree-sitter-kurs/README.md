# tree-sitter-kurs

Tree-sitter grammar and highlight queries for readable KURS (`.kurs`), the text that
`reference-compiler/kursal/readable.py` prints; the
[counter walkthrough](../../reference-compiler/examples/counter/generated/program.kurs) writes one.

The grammar is structural: it recognizes balanced groups and KURS tokens, so that types written over
several lines, block state, quantifiers and instructions highlight, and it checks nothing about them.
The core checker is the authority on KURS. The serialized form that `kurs.py` reads is S-expressions
and is not covered here.

Captures to note:

- a value (`%v2`): `@variable`
- a place (`&counter#1.value`) or raw target (`&*%p`): `@variable.parameter`
- a brand (`'1`): `@label`
- an instruction (`load`, `builtin::int_cast`): `@function.builtin`
- a declared or called function: `@function`, `@function.call`
- a block label, at its declaration and on every edge: `@label`
