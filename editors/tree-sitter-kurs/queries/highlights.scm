; Highlighting for readable KURS. Where patterns overlap, the later one wins.

(comment) @comment
(keyword) @keyword
(instruction) @function.builtin
(primitive_type) @type.builtin
(boolean) @boolean
(number) @number
(string) @string
(value) @variable
(place) @variable.parameter
(raw_target) @variable.parameter
(brand) @label
(identifier) @variable
((identifier) @type
  (#match? @type "(^|::)[A-Z][A-Za-z0-9_#]*$"))
(operator) @operator
[ "{" "}" "(" ")" "[" "]" ] @punctuation.bracket
[ "," ";" "." ":" ] @punctuation.delimiter

((keyword) @keyword.function
  (#eq? @keyword.function "fn"))
((keyword) @keyword.return
  (#eq? @keyword.return "return"))
((keyword) @keyword.conditional
  (#any-of? @keyword.conditional "jump" "branch" "switch" "unreachable"))

; Declared and called names follow `fn` and `call`. A block label follows `block` or `jump`,
; `branch` names two edges, and a `switch` edge follows `=>`.
(source_file (keyword) @_fn . (identifier) @function
  (#eq? @_fn "fn"))
(brace_group (keyword) @_fn . (identifier) @function
  (#eq? @_fn "fn"))
(brace_group (instruction) @_call . (identifier) @function.call
  (#any-of? @_call "call" "trait_call"))
(brace_group (keyword) @_block . (identifier) @label
  (#any-of? @_block "block" "jump"))
(brace_group
  (keyword) @_branch . (value) . "," . (identifier) @label . (bracket_group)? . (paren_group) . "," . (identifier) @label
  (#eq? @_branch "branch"))
(brace_group (operator) @_arrow . (identifier) @label . (bracket_group)? . (paren_group)
  (#eq? @_arrow "=>"))

; `name: type;` inside a struct, an enum, `locals` or a block's parameters.
(brace_group (identifier) @property . ":")
