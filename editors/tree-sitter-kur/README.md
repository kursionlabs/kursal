# tree-sitter-kur

Tree-sitter grammar and highlight queries for Kursal source (`.kur`).

The reference parser, `reference-compiler/kursal/parser.py`, decides what is Kursal; `grammar.js`
follows its productions. Where rejecting a program takes more than its shape, this grammar accepts
it and leaves the rejection to the compiler: a chained comparison (`a < b < c`), a second tuple
index (`p.0.1`, written `(p.0).1`), an assignment to something that is not a place (`*p = v`,
written `unsafe *p = v`, among them), a `match` arm with no comma after a body that needs one (any
body but a block-like one before the next arm, and a `return` in the last arm), and the reserved
word `module` used as a name. Every other keyword of the reference parser is reserved here too, so
`self` and `_` are names only where the reference parser takes them.

Captures to note:

- a place parameter, slot or lend: `&` and the name, `@attribute`
- a linear type: the name and its `!`, `@type.linear`
- a brand (`'k`): `@label`
- a `forall` or `foreach` type parameter: `@type.parameter`
- a variant: `@constant`; a module prefix: `@module`
