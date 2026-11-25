# Generated Kursal API documentation

The compiler generates the content of a Krayon site, its pages and search index,
from a package's resolved modules and source declarations. The site itself, its
`krayon.toml` and theme, is [`site/`](site/), outside the compiler. Krayon, a
static site generator written in Kursal, is built separately and must be on your
`PATH`. From the repository root:

```sh
bin/kursal doc lib/std -o docs/generated/src
cp -R docs/site/. docs/generated
krayon build docs/generated
python3 -m http.server 8000 --directory docs/generated/dist
```

Open `http://localhost:8000/`. `kursal doc PATH -o DIR` writes the pages into
`DIR`, the site's input directory, creating the directories above it; `DIR` must
not exist, as the command does not overwrite an existing documentation tree.
Copying `site/` over the generated tree adds `krayon.toml` beside it and the
layouts and assets into it. Remove `docs/generated/` before regenerating it. The
generated `docs/generated/` tree is ignored by Git.

`krayon build` (or `krayon check`, which validates without writing `dist`) fails
on a local link or fragment that leads nowhere and on an ID a page has twice, and
checks that the search index is valid JSON, so a build that succeeds has no
broken cross-reference.

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
- **Type pages.** Each struct, enum, tuple and type alias has a page with its
  definition, its methods and associated constants wherever they are defined
  (an `impl` in another module or package says so), and every instance whose
  head mentions the type. An `impl` written through a type alias gives its
  methods to the type the alias stands for, on that type's page. A primitive
  or builtin type (`u8`, `NonNull`) that a module's `impl` gives methods to
  has a page beside that module, with those methods and the instances that
  mention it.
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
pages). They bind the C functions a module is built on, such as the C
runtime's, which the `libc` package declares (so `bin/kursal doc lib/std`
documents `libc` as a dependency of `std`, and its module pages hold their
overviews and `libc::thread`'s raw `Thread!`), and are plumbing rather than
interface. Kursal has no item-level privacy, so every other declaration of a
public module is listed.

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
| type, trait | `/std/io/struct.File~.html`, `/std/option/enum.Option.html`, `/std/str/struct.Str.html`, `/std/cmp/trait.Ord.html` |
| primitive or builtin type | `/core/num/primitive.u8.html`, `/core/ptr/builtin.NonNull.html` |
| source file | `/src/core/cmp.kur.html#L44` |

| Anchor | On |
|---|---|
| `#fn.print`, `#const.MAX` | a module page |
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

`kursal doc` writes `search-index.json`: one record per module, type,
trait, primitive, function, constant, method and instance, with its name
(`n`), kind (`k`), path (`p`), URL from the site's root (`u`) and summary line
(`s`). The search box in the header (`site/src/assets/search.js`, no dependencies)
loads it on first use from the root the page's `doc-root` meta tag names,
which Krayon's `url` filter fills in with `base_path`. A query is matched
against names, or against paths when it contains `::` (`io::print`,
`std::fs`), and a linear type's `!` is ignored on both sides. Results rank an
exact match first, then names or paths the query starts, each written in the
query's case before any other case; then path segments it starts, then names
and paths containing it, then paths and summaries containing every word.
Within a tier, types and traits come before functions, methods, modules,
constants and instances, then shorter paths first: `Buffer` lists `Buffer!`,
then the module `buffer`. Press `/` to focus the box, the arrow keys to choose a
result, Enter to open it and Escape to close the list.

## Theme

The site uses the project's palette and opens in dark mode; the header toggle switches
to light mode and saves the choice in the browser. Headings, navigation and prose use
Space Grotesk and code uses JetBrains Mono, loaded from Google Fonts with system fallbacks.
Text keeps a contrast of at least 4.5:1 against its background in both modes.

The site lives in [`site/`](site/), a Krayon site without content: `site/krayon.toml`
and, under `site/src/`, the layouts in `_includes/` and the stylesheet, scripts and logo in
`assets/`. The compiler does not read it. The generated pages are its content, and they
name the layouts they need: `doc.html` for module, type and trait pages, `source.html` for
source files and `index.html` for the package list. `site/src/_includes/` holds the three,
each inside `page.html`. A different site can render the same pages with its own layouts of those
names; the front matter fields they use are `title`, `kind`, `nav_title`, `parent`,
`parent_title`, `package`, the `nav_items` records (`section`, `name`, `url`, `kind`) and the
`tree` records (`name`, `url`, `depth`, `current`).

The site sets `fingerprint_assets`, and the layouts link the stylesheet, scripts and logo
through Krayon's `asset_url` filter, so each build names them by the hash of their content
and a browser never keeps a stale copy. The search index is a page, not an asset, so it keeps
its name. To serve the site from a subdirectory, set `base_path` in `docs/generated/krayon.toml`
before building.

This is a source-derived API site. It does not yet provide full language-guide
chapters.
