# Generated Kursal API documentation

The compiler generates a Krayon site from a package's resolved modules and
source declarations. From the repository root:

```sh
bin/kursal doc lib/std -o docs/generated
../krayon/bin/krayon build docs/generated
python3 -m http.server 8000 --directory docs/generated/dist
```

Open `http://localhost:8000/`. Build Krayon from the sibling repository first
if `../krayon/bin/krayon` is missing. `SITE` must not exist before `kursal doc`
runs; the command does not overwrite an existing documentation tree. Remove
`docs/generated/` before regenerating it.

The generator uses the compiler's resolver to find the selected package and
its dependencies. It writes a package index, pages for each package root and
public child module, links for re-exports, declaration headers, and adjacent
documentation comments. Use `//!` at the start of a file for a module overview
and `///` immediately before a declaration for its documentation. Ordinary
`//` comments stay in source and are omitted from the site. An `impl` in a
hidden child module is included with its reachable type. The generated
`docs/generated/` tree is ignored by Git.

Each module page groups its sidebar by modules, types, traits, functions,
constants, instances, and externs. A type expands to its definition and
methods. An instance is listed under what it is an instance of, such as
`Ord[u8, u8]`, with the documentation comment above it.
The site follows the reader's light or dark system theme by default; the header
toggle saves an explicit choice in the browser.

The layouts, stylesheet and script live in [`theme/`](theme/): Krayon layouts
in `theme/_includes/` and static files in `theme/assets/`. `kursal doc` looks
for the theme beside the standard library it uses, in `docs/theme` next to
`$KURSAL_LIB` (or next to the `lib/` it finds without it), and copies both
directories into `SITE/src`, so a generated site builds on its own. It stops
with an error, before writing anything, when the theme is missing.

This is a source-derived API site. It does not yet provide full language-guide
chapters or search.
