/**
 * Tree-sitter grammar for Kursal source (`.kur` files).
 *
 * The reference parser, `reference-compiler/kursal/parser.py`, decides what is accepted; this grammar
 * follows its productions for editor parsing and highlighting. It accepts a superset where a rejection
 * takes more than a program's shape: a chained comparison, `p.0.1`, an assignment to something that
 * is not a place, and a `match` arm whose body is not a block with no comma before the next arm parse
 * here and are rejected by the compiler.
 */

function commaSep1(rule) {
  return seq(rule, repeat(seq(',', rule)), optional(','));
}

function commaSep(rule) {
  return optional(commaSep1(rule));
}

// The `!` that ends a linear type's name: part of the name, so nothing comes between them. After a name,
// `!=` is the operator, which the lexer prefers as the longer token: `a!=b` is `a != b`.
function linear() {
  return optional(field('linear', token.immediate('!')));
}

// `'a, 'b`: one or more, with no trailing comma.
function brandList($) {
  return seq(field('brand', $.brand), repeat(seq(',', field('brand', $.brand))));
}

const PREC = {
  closure: -1,
  or: 1,
  and: 2,
  compare: 3,
  bitor: 4,
  bitxor: 5,
  bitand: 6,
  shift: 7,
  add: 8,
  mul: 9,
  cast: 10,
  unary: 11,
  postfix: 12,
};

const INTEGER_SUFFIX = choice('i8', 'i16', 'i32', 'i64', 'isize', 'u8', 'u16', 'u32', 'u64', 'usize');

module.exports = grammar({
  name: 'kur',

  word: $ => $.identifier,

  extras: $ => [/\s/, $.line_comment, $.block_comment],

  conflicts: $ => [
    // A block-like expression that ends a block is its tail; one followed by more statements is a
    // statement of its own.
    [$.expression_statement, $._expression],
  ],

  rules: {
    source_file: $ => repeat($._item),

    // -------------------------------------------------------------- items --

    _item: $ => choice(
      $.use_declaration,
      $.module_declaration,
      $.struct_declaration,
      $.tuple_declaration,
      $.enum_declaration,
      $.function_declaration,
      $.extern_function,
      $.impl_block,
      $.trait_declaration,
      $.instance_declaration,
      $.const_declaration,
    ),

    // `forall T, 'k -> U where Trait[T, U].`: the parameters after `->` are determined by the others.
    forall_clause: $ => seq(
      'forall',
      field('parameter', $._forall_parameter),
      repeat(seq(',', field('parameter', $._forall_parameter))),
      optional(seq(
        '->',
        field('determined', $._forall_parameter),
        repeat(seq(',', field('determined', $._forall_parameter))),
      )),
      optional(seq('where', $.premise, repeat(seq(',', $.premise)))),
      '.',
    ),

    _forall_parameter: $ => choice($.identifier, $.brand),

    premise: $ => seq(field('trait', $.path), field('arguments', $.type_arguments)),

    use_declaration: $ => seq(choice('use', 'export'), field('tree', $.use_tree), ';'),

    use_tree: $ => seq(
      field('path', $.identifier),
      linear(),
      repeat(seq('::', field('path', $.identifier), linear())),
      optional(seq('::', choice(
        field('glob', '*'),
        seq('{', commaSep(field('child', $.use_tree)), '}'),
      ))),
      optional(seq('as', field('alias', $.identifier), linear())),
    ),

    module_declaration: $ => seq('module', field('path', $.path), ';'),

    path: $ => seq($.identifier, repeat(seq('::', $.identifier))),

    exposure: $ => seq('exposed', optional(seq('(', field('scope', $.path), ')'))),

    struct_declaration: $ => seq(
      optional($.forall_clause),
      optional($.exposure),
      'struct',
      field('name', $.identifier),
      linear(),
      '{',
      commaSep(field('field', $.field_declaration)),
      '}',
    ),

    field_declaration: $ => seq(field('name', $.identifier), ':', field('type', $._type)),

    tuple_declaration: $ => seq(
      optional($.forall_clause),
      optional($.exposure),
      'tuple',
      field('name', $.identifier),
      linear(),
      '(',
      commaSep(field('element', $._type)),
      ')',
      ';',
    ),

    enum_declaration: $ => seq(
      optional($.forall_clause),
      optional($.exposure),
      'enum',
      field('name', $.identifier),
      linear(),
      '{',
      commaSep(field('variant', $.variant_declaration)),
      '}',
    ),

    variant_declaration: $ => seq(
      field('name', $.identifier),
      optional(seq('(', commaSep1(field('payload', $._type)), ')')),
    ),

    // A trait method may end in `;` instead of a body.
    function_declaration: $ => seq(
      optional($.forall_clause),
      optional('unsafe'),
      'fn',
      field('name', $.identifier),
      field('parameters', $.parameter_list),
      optional(seq('->', field('return_type', $._type))),
      choice(field('body', $.block), ';'),
    ),

    extern_function: $ => seq(
      optional($.forall_clause),
      'extern',
      field('abi', $.string_literal),
      'fn',
      field('name', $.identifier),
      field('parameters', $.parameter_list),
      optional(seq('->', field('return_type', $._type))),
      ';',
    ),

    parameter_list: $ => seq('(', commaSep(field('parameter', $.parameter)), ')'),

    // `x: T`, `&x: T` (a place), `self`, `&self`, `self: T`.
    parameter: $ => seq(
      optional(field('place', '&')),
      choice(
        seq(field('name', $.self), optional(seq(':', field('type', $._type)))),
        seq(field('name', $.identifier), ':', field('type', $._type)),
      ),
    ),

    impl_block: $ => seq(
      optional($.forall_clause),
      'impl',
      field('target', $._type),
      '{',
      repeat(choice(field('method', $.function_declaration), field('constant', $.const_declaration))),
      '}',
    ),

    trait_declaration: $ => seq(
      optional($.forall_clause),
      'trait',
      field('name', $.identifier),
      '{',
      repeat(field('method', $.function_declaration)),
      '}',
    ),

    instance_declaration: $ => seq(
      optional($.forall_clause),
      optional('unsafe'),
      'instance',
      field('trait', $._type),
      '{',
      repeat(field('method', $.function_declaration)),
      '}',
    ),

    const_declaration: $ => seq(
      optional($.forall_clause),
      'const',
      field('name', $.identifier),
      ':',
      field('type', $._type),
      '=',
      field('value', $._expression),
      ';',
    ),

    // -------------------------------------------------------------- types --

    _type: $ => choice(
      $.named_type,
      $.pointer_type,
      $.never_type,
      $.tuple_type,
      $.array_type,
      $.function_type,
      $.closure_type,
      $.exists_type,
      $.foreach_type,
    ),

    named_type: $ => seq(
      field('name', $.path),
      linear(),
      optional(field('arguments', $.type_arguments)),
    ),

    // `[A, 'k, _]`, or `[A -> B]` where the arguments after `->` are determined by the others.
    type_arguments: $ => seq(
      '[',
      field('argument', $._type_argument),
      repeat(seq(',', field('argument', $._type_argument))),
      optional(seq(
        '->',
        field('determined', $._type_argument),
        repeat(seq(',', field('determined', $._type_argument))),
      )),
      optional(','),
      ']',
    ),

    _type_argument: $ => choice($.brand, $.inferred_type, $._type),

    inferred_type: _ => '_',

    pointer_type: $ => seq('*', field('pointee', $._type)),

    never_type: _ => '!',

    // `()`, `(T)` (just `T`), `(T,)`, `(T, U)`.
    tuple_type: $ => seq('(', commaSep(field('element', $._type)), ')'),

    array_type: $ => seq('[', field('element', $._type), ';', field('length', $._expression), ']'),

    function_type: $ => seq(
      optional('unsafe'),
      'fn',
      '(',
      commaSep($._slot),
      ')',
      '->',
      field('return_type', $._type),
    ),

    // `closure[Env](...) -> T`; a parameter's type may leave the environment unnamed.
    closure_type: $ => seq(
      'closure',
      optional(seq('[', field('environment', $._type), ']')),
      '(',
      commaSep($._slot),
      ')',
      '->',
      field('return_type', $._type),
    ),

    // A slot of a function or closure type: `T`, or a place `&name: T` (`&_: T`).
    _slot: $ => choice(field('parameter', $._type), field('parameter', $.place_slot)),

    place_slot: $ => seq('&', field('name', choice($.identifier, '_')), ':', field('type', $._type)),

    exists_type: $ => seq('exists', brandList($), '.', field('body', $._type)),

    foreach_type: $ => seq(
      'foreach',
      optional(field('indirect', '*')),
      field('binder', $.identifier),
      'of',
      field('source', $._type),
      '=>',
      field('body', $._type),
    ),


    // --------------------------------------------------------- statements --

    block: $ => seq('{', repeat($._statement), optional(field('tail', $._expression)), '}'),

    _statement: $ => choice(
      $.let_statement,
      $.var_statement,
      $.const_declaration,
      $.assignment_statement,
      $.expression_statement,
    ),

    let_statement: $ => seq(
      'let',
      optional(seq('exists', brandList($), '.')),
      field('pattern', $._pattern),
      optional(seq(':', field('type', $._type))),
      '=',
      field('value', $._expression),
      ';',
    ),

    var_statement: $ => seq(
      'var',
      field('name', $.identifier),
      optional(seq(':', field('type', $._type))),
      '=',
      field('value', $._expression),
      ';',
    ),

    // `x.f = v;`, and `unsafe *p = v;`, a store through a raw pointer.
    assignment_statement: $ => seq(field('left', $._operator_expression), '=', field('value', $._expression), ';'),

    expression_statement: $ => choice(
      seq($._expression, ';'),
      prec.dynamic(-1, $._block_like),
    ),

    // ----------------------------------------------------------- patterns --

    _pattern: $ => choice(
      $.identifier,
      $.wildcard_pattern,
      $.tuple_pattern,
      $.array_pattern,
      $.struct_pattern,
    ),

    wildcard_pattern: _ => '_',

    // `()` or `(a, ...)`: a tuple of one element is `(a,)`.
    tuple_pattern: $ => seq(
      '(',
      optional(seq(field('element', $._pattern), ',', commaSep(field('element', $._pattern)))),
      ')',
    ),

    array_pattern: $ => seq('[', commaSep1(field('element', $._pattern)), ']'),

    struct_pattern: $ => seq('{', optional($._field_patterns), '}'),

    _field_patterns: $ => choice(
      $.rest,
      seq(
        field('field', $.field_pattern),
        repeat(seq(',', field('field', $.field_pattern))),
        optional(seq(',', optional(seq($.rest, optional(','))))),
      ),
    ),

    field_pattern: $ => seq(field('name', $.identifier), optional(seq(':', field('pattern', $._pattern)))),

    // `..name`: the fields not listed, kept together under one name.
    rest: $ => seq('..', field('name', $.identifier)),

    // -------------------------------------------------------- expressions --

    _expression: $ => choice(
      $._block_like,
      $.closure_expression,
      $.return_expression,
      $.pack_expression,
      $._operator_expression,
    ),

    _block_like: $ => choice(
      $.block,
      $.if_expression,
      $.match_expression,
      $.while_expression,
      $.for_expression,
      $.foreach_expression,
    ),

    if_expression: $ => seq(
      'if',
      field('condition', $._operator_expression),
      field('consequence', $.block),
      optional(seq('else', field('alternative', choice($.block, $.if_expression)))),
    ),

    while_expression: $ => seq('while', field('condition', $._operator_expression), field('body', $.block)),

    for_expression: $ => seq(
      'for',
      field('name', $.identifier),
      'in',
      field('iterator', $._operator_expression),
      field('body', $.block),
    ),

    // `foreach x of s { ... }`, or `foreach x: X of s { ... }`, naming each component's type `X`.
    foreach_expression: $ => seq(
      'foreach',
      field('name', $.identifier),
      optional(seq(':', field('binder', $.identifier))),
      'of',
      field('source', $._operator_expression),
      field('body', $.block),
    ),

    match_expression: $ => seq(
      'match',
      field('value', $._operator_expression),
      '{',
      repeat(field('arm', $.match_arm)),
      optional(field('arm', alias($.wildcard_arm, $.match_arm))),
      '}',
    ),

    match_arm: $ => seq(
      field('pattern', $._case),
      repeat(seq('|', field('pattern', $._case))),
      '=>',
      field('body', $._expression),
      optional(','),
    ),

    // The `_` arm is the last one.
    wildcard_arm: $ => seq(field('pattern', $.wildcard_pattern), '=>', field('body', $._expression), optional(',')),

    _case: $ => choice($.variant_case, $.value_case),

    // `Variant` or `Enum::Variant`, or either with `(a, _)` binding the payload.
    variant_case: $ => seq(
      field('variant', $.path_expression),
      optional(seq('(', commaSep(field('binding', choice($.identifier, $.wildcard_pattern))), ')')),
    ),

    // `5`, `-5`, `b'a'`, or an inclusive range `b'0'..=b'9'`.
    value_case: $ => seq(field('low', $._case_value), optional(seq('..=', field('high', $._case_value)))),

    _case_value: $ => choice($.integer_literal, $.byte_literal, $.negative_literal),

    negative_literal: $ => seq('-', choice($.integer_literal, $.byte_literal)),

    // `|x, &y: T| body` or `|| body`; with a return type the body is a block.
    closure_expression: $ => prec(PREC.closure, seq(
      choice('||', seq('|', commaSep(field('parameter', $.closure_parameter)), '|')),
      choice(
        seq('->', field('return_type', $._type), field('body', $.block)),
        field('body', $._expression),
      ),
    )),

    closure_parameter: $ => seq(
      optional(field('place', '&')),
      field('name', $.identifier),
      optional(seq(':', field('type', $._type))),
    ),

    return_expression: $ => prec.right(seq('return', optional(field('value', $._expression)))),

    // `pack 'a, 'b. value`: the brands are the witnesses the package hides.
    pack_expression: $ => seq('pack', brandList($), '.', field('value', $._expression)),

    _operator_expression: $ => choice(
      $.binary_expression,
      $.cast_expression,
      $.unary_expression,
      $._postfix_expression,
    ),

    binary_expression: $ => {
      const table = [
        [PREC.or, '||'],
        [PREC.and, '&&'],
        [PREC.compare, choice('==', '!=', '<', '<=', '>', '>=')],
        [PREC.bitor, '|'],
        [PREC.bitxor, '^'],
        [PREC.bitand, '&'],
        [PREC.shift, choice('<<', '>>')],
        [PREC.add, choice('+', '-')],
        [PREC.mul, choice('*', '/', '%')],
      ];
      return choice(...table.map(([precedence, operator]) => prec.left(precedence, seq(
        field('left', $._operator_expression),
        field('operator', operator),
        field('right', $._operator_expression),
      ))));
    },

    cast_expression: $ => prec.left(PREC.cast, seq(field('value', $._operator_expression), 'as', field('type', $._type))),

    // `&x` lends a place; `unsafe e` authorizes one unsafe operation.
    unary_expression: $ => prec(PREC.unary, seq(
      field('operator', choice('-', '!', '*', '&', 'unsafe')),
      field('operand', $._operator_expression),
    )),

    _postfix_expression: $ => choice(
      $.field_expression,
      $.call_expression,
      $.try_expression,
      $._atom,
    ),

    field_expression: $ => prec(PREC.postfix, seq(
      field('value', $._postfix_expression),
      choice(seq('.', field('field', $.identifier)), field('field', $.tuple_index)),
    )),

    call_expression: $ => prec(PREC.postfix, seq(field('function', $._postfix_expression), field('arguments', $.arguments))),

    try_expression: $ => prec(PREC.postfix, seq(field('value', $._postfix_expression), '?')),

    arguments: $ => seq('(', commaSep(field('argument', $._expression)), ')'),

    _atom: $ => choice(
      $.integer_literal,
      $.float_literal,
      $.byte_literal,
      $.string_literal,
      $.fstring_literal,
      $.boolean_literal,
      $.parenthesized_expression,
      $.array_expression,
      $.path_expression,
      $.struct_expression,
      $.construct_expression,
    ),

    // `()`, `(e)`, `(e,)`, `(e, f)`.
    parenthesized_expression: $ => seq('(', commaSep(field('element', $._expression)), ')'),

    // `[a, b]`, or `[a, b, ..]`, repeating the last element to the array's length.
    array_expression: $ => seq(
      '[',
      optional(seq(
        field('element', $._expression),
        repeat(seq(',', field('element', $._expression))),
        optional(seq(',', optional(seq(field('fill', '..'), optional(','))))),
      )),
      ']',
    ),

    // `a`, `self`, `Option[u32]::Some`, `Map!['k]::new`: each segment may be a linear type's name and carry
    // type arguments.
    path_expression: $ => prec.right(seq($.path_segment, repeat(seq('::', $.path_segment)))),

    path_segment: $ => prec.right(seq(
      field('name', choice($.identifier, $.self)),
      linear(),
      optional(field('arguments', $.type_arguments)),
    )),

    // `Point.{ x: 1u32, y }` and `Point.{ x: 1u32, ..rest }`.
    struct_expression: $ => seq(
      field('name', $.path_expression),
      '.{',
      optional(choice(
        $.rest,
        seq(
          field('field', $.field_initializer),
          repeat(seq(',', field('field', $.field_initializer))),
          optional(seq(',', optional(seq($.rest, optional(','))))),
        ),
      )),
      '}',
    ),

    field_initializer: $ => seq(field('name', $.identifier), optional(seq(':', field('value', $._expression)))),

    // `Pair.(a, b)`, `Option::Some.(x)`: a tuple struct or a variant with its payload.
    construct_expression: $ => seq(
      field('name', $.path_expression),
      '.(',
      commaSep(field('argument', $._expression)),
      ')',
    ),

    // ----------------------------------------------------------- literals --

    integer_literal: _ => token(seq(
      choice(/[0-9][0-9_]*/, /0x[0-9a-fA-F_]+/, /0o[0-7_]+/, /0b[01_]+/),
      optional(INTEGER_SUFFIX),
    )),

    float_literal: _ => token(choice(
      seq(/[0-9][0-9_]*/, '.', /[0-9][0-9_]*/, optional(/[eE][+-]?[0-9][0-9_]*/), optional(choice('f32', 'f64'))),
      seq(/[0-9][0-9_]*/, /[eE][+-]?[0-9][0-9_]*/, optional(choice('f32', 'f64'))),
      seq(/[0-9][0-9_]*/, choice('f32', 'f64')),
    )),

    byte_literal: _ => token(seq("b'", choice(/\\x[0-9a-fA-F]{2}/, /\\./, /[^'\\]/), "'")),

    boolean_literal: _ => choice('true', 'false'),

    string_literal: $ => seq(
      '"',
      repeat(choice($.escape_sequence, $._string_content)),
      token.immediate('"'),
    ),

    _string_content: _ => token.immediate(prec(1, /[^"\\]+/)),

    escape_sequence: _ => token.immediate(choice(/\\x[0-9a-fA-F]{2}/, /\\./)),

    // `f"x = {x}"`; `{{` and `}}` stand for a brace.
    fstring_literal: $ => seq(
      'f"',
      repeat(choice($.escape_sequence, $.escaped_brace, $.interpolation, $._fstring_content)),
      token.immediate('"'),
    ),

    _fstring_content: _ => token.immediate(prec(1, /[^"{}\\]+/)),

    escaped_brace: _ => token.immediate(choice('{{', '}}')),

    interpolation: $ => seq(token.immediate('{'), field('value', $._expression), '}'),

    brand: _ => /'[A-Za-z0-9_]+/,

    tuple_index: _ => /\.[0-9]+/,

    self: _ => 'self',

    identifier: _ => /[_\p{XID_Start}][\p{XID_Continue}]*/,

    line_comment: _ => token(seq('//', /[^\n]*/)),

    block_comment: _ => token(seq('/*', /[^*]*\*+([^/*][^*]*\*+)*/, '/')),
  },
});
