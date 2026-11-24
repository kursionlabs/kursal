; Highlighting for Kursal source. Where patterns overlap, the later one wins (the Neovim and Helix
; convention), so general patterns come first and the linear-type and place patterns last.

; ------------------------------------------------------------ identifiers --

(identifier) @variable

(path_expression (path_segment name: (identifier) @module) "::")

((path_segment name: (identifier) @type)
  (#match? @type "^[A-Z]"))

(self) @variable.builtin

; ------------------------------------------------------------------ types --

(named_type name: (path (identifier) @module))
(named_type name: (path (identifier) @type .))

((named_type name: (path . (identifier) @type.builtin .))
  (#any-of? @type.builtin
    "bool" "i8" "i16" "i32" "i64" "isize" "u8" "u16" "u32" "u64" "usize" "f32" "f64"))

(never_type) @type.builtin
(inferred_type) @type.builtin

(struct_declaration name: (identifier) @type)
(tuple_declaration name: (identifier) @type)
(enum_declaration name: (identifier) @type)
(trait_declaration name: (identifier) @type)
(variant_declaration name: (identifier) @constant)

(premise trait: (path (identifier) @type .))

(forall_clause parameter: (identifier) @type.parameter)
(forall_clause determined: (identifier) @type.parameter)
(foreach_type binder: (identifier) @type.parameter)
(foreach_expression binder: (identifier) @type.parameter)

(brand) @label

; -------------------------------------------------------------- functions --

(function_declaration name: (identifier) @function)
(function_signature name: (identifier) @function)
(extern_function name: (identifier) @function)

(call_expression
  function: (path_expression (path_segment name: (identifier) @function.call) .))
(call_expression
  function: (field_expression field: (identifier) @function.method.call))

; Variants and constructors: `Option::Some`, `Pair.(a, b)`.
(variant_case variant: (path_expression (path_segment name: (identifier) @constant) .))
(construct_expression name: (path_expression (path_segment name: (identifier) @type) .))
(struct_expression name: (path_expression (path_segment name: (identifier) @type) .))
(construct_expression
  name: (path_expression (path_segment) "::" (path_segment name: (identifier) @constant) .))

; ------------------------------------------------------- names and fields --

(parameter name: (identifier) @variable.parameter)
(closure_parameter name: (identifier) @variable.parameter)

(field_declaration name: (identifier) @property)
(field_initializer name: (identifier) @property)
(field_pattern name: (identifier) @property)
(field_expression field: (identifier) @property)
(field_expression field: (tuple_index) @property)

(const_declaration name: (identifier) @constant)

(use_tree path: (identifier) @module)
(exposure scope: (path (identifier) @module))

; --------------------------------------------------------------- literals --

(integer_literal) @number
(float_literal) @number.float
(byte_literal) @character
(boolean_literal) @boolean
(string_literal) @string
(fstring_literal) @string
(escape_sequence) @string.escape
(escaped_brace) @string.escape
(interpolation "{" @punctuation.special "}" @punctuation.special)

(line_comment) @comment
(block_comment) @comment

; --------------------------------------------------------------- keywords --

[
  "use"
  "export"
  "exposed"
  "struct"
  "tuple"
  "enum"
  "impl"
  "trait"
  "instance"
  "extern"
  "closure"
  "forall"
  "where"
  "exists"
  "foreach"
  "of"
  "pack"
  "as"
  "let"
] @keyword

"fn" @keyword.function

[
  "var"
  "const"
  "unsafe"
] @keyword.modifier

[
  "if"
  "else"
  "match"
  "while"
  "for"
  "in"
] @keyword.conditional

"return" @keyword.return

; -------------------------------------------------------------- operators --

[
  "->"
  "=>"
  "::"
  "=="
  "!="
  "<"
  "<="
  ">"
  ">="
  "&&"
  "||"
  "|"
  "^"
  "&"
  "<<"
  ">>"
  "+"
  "-"
  "*"
  "/"
  "%"
  "!"
  "="
  "?"
  ".."
  "..="
] @operator

[ "(" ")" "[" "]" "{" "}" ".{" ".(" ] @punctuation.bracket
[ "," ";" ":" "." ] @punctuation.delimiter

; ----------------------------------------------------------- linear types --
; A linear type's `!` reads as one unit with its name.

(named_type name: (path (identifier) @type.linear .) linear: "!" @type.linear)
(struct_declaration name: (identifier) @type.linear linear: "!" @type.linear)
(tuple_declaration name: (identifier) @type.linear linear: "!" @type.linear)
(enum_declaration name: (identifier) @type.linear linear: "!" @type.linear)
(path_segment name: (identifier) @type.linear linear: "!" @type.linear)
(use_tree path: (identifier) @type.linear . linear: "!" @type.linear)
(use_tree alias: (identifier) @type.linear . linear: "!" @type.linear)

; ----------------------------------------------------------------- places --
; The `&` of a place parameter or slot and of a lend reads as one unit with its name.

(parameter place: "&" @attribute name: (_) @attribute)
(closure_parameter place: "&" @attribute name: (identifier) @attribute)
(place_slot "&" @attribute name: (_) @attribute)
(unary_expression operator: "&" @attribute)

; --------------------------------------------------------- reserved words --
; `module` is reserved: the compiler rejects it wherever it appears.

((identifier) @keyword
  (#eq? @keyword "module"))
