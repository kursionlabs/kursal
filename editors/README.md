# Editors

Tree-sitter grammars and highlight queries for editors.

| Directory | Files | Text |
|---|---|---|
| [`tree-sitter-kur`](tree-sitter-kur/README.md) | `.kur` | Kursal source |
| [`tree-sitter-kurs`](tree-sitter-kurs/README.md) | `.kurs` | readable KURS, the core language as the compiler prints it |

Each directory holds an editable `grammar.js`, the parser generated from it (`src/`; not edited by
hand), `queries/highlights.scm`, and `tree-sitter.json` for editors that read Tree-sitter package
metadata. After changing a grammar, regenerate its parser:

```sh
cd editors/tree-sitter-kur
tree-sitter generate
```

`reference-compiler/tests/test_editors.py` checks that the generated parsers are current, that every
`.kur` file in the repository and the readable KURS of several examples parse without errors, that the
highlight queries compile, and that the `.kur` grammar accepts and rejects a set of sources as the
reference parser does. It is skipped when `tree-sitter` is not installed.

## Using a grammar

Register a Tree-sitter language named `kur` (or `kurs`) built from the directory's `src/parser.c`,
associate the file extension with it, and install `queries/highlights.scm` where the editor looks
for that language's queries (for Neovim, `queries/kur/highlights.scm` on the runtime path). Where
two patterns capture the same node, the later one wins, as in Neovim and Helix.

To look at a parse tree from the command line:

```sh
cd editors/tree-sitter-kur
tree-sitter parse ../../examples/sayhi/lib.kur
```
