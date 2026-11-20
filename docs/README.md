# Generated Kursal API documentation

The compiler generates a Krayon site from a package's resolved modules and
source declarations. Krayon, a static site generator written in Kursal, is
built separately and must be on your `PATH`. From the repository root:

```sh
bin/kursal doc lib/std -o docs/generated
krayon build docs/generated
python3 -m http.server 8000 --directory docs/generated/dist
```

Open `http://localhost:8000/`. `SITE` must not exist before `kursal doc`
runs; the command does not overwrite an existing documentation tree. Remove
`docs/generated/` before regenerating it. The generated `docs/generated/` tree
is ignored by Git.

The generator uses the compiler's resolver to find the selected package and
its dependencies, and documents all of them. Use `//!` at the start of a file
for a module overview and `///` immediately before a declaration for its
documentation; the first sentence of the first paragraph is its summary line.
The sentence ends at a `.` outside backticks that ends the paragraph or comes
before a space and anything but a lowercase letter; `e.g.`, `i.e.`, `cf.` and
`vs.` do not end it.
Ordinary `//` comments stay in source and are omitted from the site.

## Organization

- **Module pages.** Each module a package exposes has a page: the overview,
  then a table for each kind of item it has (modules, re-exports, types,
  traits, functions, constants, instances, and methods it gives to other
  modules' types), each row the item's linked name and summary line. The
  functions, constants and instances follow in full. Functions and constants
  stay on the module page rather than getting pages of their own: a Kursal
  function has nothing beneath it, so its page would hold one signature and
  its comment, and the module page shows them all at once while each still
  has a stable anchor to link to.
- **Type pages.** Each struct, enum and tuple has a page with its definition,
  its methods and associated constants wherever they are defined (an `impl`
  in another module or package says so), and every instance whose head
  mentions the type. A primitive or builtin type (`u8`, `NonNull`) that a
  module's `impl` gives methods to has a page beside that module, with those
  methods and the instances that mention it.
- **Trait pages.** Each trait has a page with its definition, its methods, and
  its implementors: every instance of it in every documented package.
- **Blanket methods.** The methods of an `impl` over a type parameter
  (`impl I` under a premise) are listed in full on their module's page.
- **Signatures** are highlighted with the classes of Krayon's highlighter
  (`tok-keyword`, `tok-type`, `tok-string`, `tok-number`, `tok-comment`), and
  each type and trait they name is a link to its page. The resolver decides
  what a name means, through the module's imports; a type parameter, a
  primitive or a builtin has no page and is not linked.
- **Source.** Each source file has a page, its lines numbered and anchored,
  and each item links to the line it starts on.
- **Sidebar.** A module page lists its items by kind; a type or trait page
  lists its methods and instances, then the other types and traits of its
  module. Both end with the package's module tree, the current module marked.

`extern` declarations are left out of the pages (they remain on the source
pages). They bind the C functions a module is built on, such as the
`kur_libc_*` helpers, and are the module's own plumbing rather than its
interface. Kursal has no item-level privacy, so every other declaration of a
public module is listed. A visibility rule would be applied where the
generator walks a module's items (`module_page` and `search_index` in
`compiler/doc/generator.kur`) and where it gathers a type's methods and the
instances (`model::build`), so that hidden items get no page, row, sidebar
entry or search record, and links to them (`model::decl_url`) are left out as
links to undocumented items are.

## Re-exported modules

A module that a package exports from another package, such as `core::cmp`,
which `std` exports as `std::cmp`, has its pages under both: `/core/cmp/` and
`/std/cmp/`, the latter titled `std::cmp` and noting that it is re-exported
from `core::cmp`, with a link there. A reader of `std` stays in `std`, and a
reader of `core` alone still has its pages. Both are generated from the same
source in one run, so there is nothing to keep in step by hand. Links on a
package's pages lead to the path that package exposes (`std::cmp::Ord` rather
than `core::cmp::Ord`), the shortest when it has several, else to the declaring
package's own.
The search index lists each item once, at the path the documented package
(the one `kursal doc` was given) links to.

## URLs and anchors

| Page | URL |
|---|---|
| package root module | `/std/` |
| module | `/std/io/`, `/std/cmp/` |
| type, trait | `/std/io/struct.File~.html`, `/std/option/enum.Option.html`, `/std/cmp/trait.Ord.html` |
| primitive or builtin type | `/core/num/primitive.u8.html`, `/core/ptr/builtin.NonNull.html` |
| source file | `/src/core/cmp.kur.html#L44` |

| Anchor | On |
|---|---|
| `#fn.read_line`, `#const.MAX` | a module page |
| `#method.read_line`, `#const.MAX` | a type, trait or primitive page; a module page's blanket methods |
| `#instance.Ord-5bu8-2cu8-5d` | module, type and trait pages |
| `#L12` | a source page |

Names are spelled for URLs and anchors as follows: ASCII letters, digits and
`_` are kept, a linear type's `!` becomes `~`, `::` becomes `.`, and every
other byte becomes `-` and its two lowercase hex digits. So `File!` is
`File~`, and the instance `Ord[u8, u8]`, its head written without spaces, is
`instance.Ord-5bu8-2cu8-5d`. Every character of the result is unreserved in a
URL, so links need no percent-encoding, and no two names share a spelling. In
the rare case that two instances shown on one page are written alike (the same
head naming different types in different modules), each anchor also gets
`.` and the encoded path of its module. Pages and anchors depend only on names
and paths, never on the order of declarations, so adding a declaration leaves
every existing link in place.

Links inside generated HTML are relative to the page, and Krayon prefixes the
root-relative links of its templates and Markdown with `base_path`, so a site
works when served from a subdirectory.

## Search

`kursal doc` writes `assets/search-index.json`: one record per module, type,
trait, primitive, function, constant, method and instance, with its name
(`n`), kind (`k`), path (`p`), URL from the site's root (`u`) and summary line
(`s`). The search box in the header (`assets/search.js`, no dependencies)
loads it on first use from the root the page's `doc-root` meta tag names,
which Krayon's `url` filter fills in with `base_path`. A query is matched
against names, or against paths when it contains `::` (`io::read_line`,
`std::fs`), and a linear type's `!` is ignored on both sides. Results rank an
exact match first, then names or paths the query starts, each written in the
query's case before any other case; then path segments it starts, then names
and paths containing it, then paths and summaries containing every word.
Within a tier, types and traits come before functions, methods, modules,
constants and instances, then shorter paths first: `Buffer` lists `Buffer!`,
then the module `buffer`. Press `/` to focus the box, the arrow keys to choose a
result, Enter to open it and Escape to close the list.

## Theme

The site uses the Kursion palette and opens in dark mode; the header toggle switches
to light mode and saves the choice in the browser. Headings, navigation and prose use
Space Grotesk and code uses JetBrains Mono, loaded from Google Fonts with system fallbacks.
Text keeps a contrast of at least 4.5:1 against its background in both modes.

The layouts, stylesheet and scripts live in [`theme/`](theme/): Krayon layouts
in `theme/_includes/` (`doc.html` for module, type and trait pages,
`source.html` for source files, `index.html` for the package list, all inside
`page.html`) and static files in `theme/assets/`. `kursal doc` looks for the
theme beside the standard library it uses, in `docs/theme` next to
`$KURSAL_LIB` (or next to the `lib/` it finds without it), and copies both
directories into `SITE/src`, so a generated site builds on its own. It stops
with an error, before writing anything, when the theme is missing.

This is a source-derived API site. It does not yet provide full language-guide
chapters.
