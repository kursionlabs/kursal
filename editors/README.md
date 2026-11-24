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
for that language's queries. Where two patterns capture the same node, the later one wins, as in
Neovim and Helix. The steps below are for `kur`; for `kurs`, use `editors/tree-sitter-kurs` and the
name `kurs`. There is no TextMate grammar, so no VS Code extension.

### Neovim

Neovim loads a parser from `parser/kur.so` and its queries from `queries/kur/` on the runtime path.
Without plugins, from the repository root:

```sh
site=~/.local/share/nvim/site
mkdir -p "$site/parser" "$site/queries/kur"
(cd editors/tree-sitter-kur && tree-sitter build -o "$site/parser/kur.so")
cp editors/tree-sitter-kur/queries/highlights.scm "$site/queries/kur/"
```

and in `init.lua`:

```lua
vim.filetype.add({ extension = { kur = "kur", kurs = "kurs" } })
vim.api.nvim_create_autocmd("FileType", {
  pattern = { "kur", "kurs" },
  callback = function() vim.treesitter.start() end,
})
```

With [nvim-treesitter](https://github.com/nvim-treesitter/nvim-treesitter)'s `master` branch,
register the parser instead, run `:TSInstall kur`, and copy the queries as above:

```lua
require("nvim-treesitter.parsers").get_parser_configs().kur = {
  install_info = {
    url = "https://github.com/kursionlabs/kursal",
    location = "editors/tree-sitter-kur",
    files = { "src/parser.c" },
    branch = "main",
  },
  filetype = "kur",
}
vim.filetype.add({ extension = { kur = "kur" } })
```

### Helix

In `~/.config/helix/languages.toml`, with `rev` set to a commit of this repository:

```toml
[[language]]
name = "kur"
scope = "source.kur"
file-types = ["kur"]
roots = ["kur.toml"]
comment-token = "//"
indent = { tab-width = 4, unit = "    " }

[[grammar]]
name = "kur"
source = { git = "https://github.com/kursionlabs/kursal", rev = "COMMIT", subpath = "editors/tree-sitter-kur" }
```

then fetch and build the grammar, and install its queries in Helix's runtime directory:

```sh
hx --grammar fetch && hx --grammar build
mkdir -p ~/.config/helix/runtime/queries/kur
cp editors/tree-sitter-kur/queries/highlights.scm ~/.config/helix/runtime/queries/kur/
```

### From the command line

To look at a parse tree:

```sh
cd editors/tree-sitter-kur
tree-sitter parse ../../examples/hello/lib.kur
```
