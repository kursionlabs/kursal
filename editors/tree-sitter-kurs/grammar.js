/**
 * Tree-sitter grammar for readable KURS (`.kurs`), the text that `reference-compiler/kursal/readable.py`
 * prints. It is structural: it recognizes balanced groups and KURS tokens, so that types spread over
 * several lines, block state, quantifiers and instructions highlight, and it checks nothing about
 * them. The serialized form that `kurs.py` reads is S-expressions and is not covered.
 */

// A name: `main`, `counter::bump`, `#tmp0`, `counter#1`, `b10`, and a linear type's, which ends with `!`:
// `std::buffer::Buffer!`, `std::buffer::Buffer!::new`.
const NAME = /[A-Za-z_#][A-Za-z0-9_#]*!?(::[A-Za-z_#][A-Za-z0-9_#]*!?)*/;

module.exports = grammar({
  name: 'kurs',

  word: $ => $.identifier,

  extras: $ => [/\s/, $.comment],

  rules: {
    source_file: $ => repeat($._item),

    _item: $ => choice(
      $.brace_group, $.paren_group, $.bracket_group,
      $.keyword, $.instruction, $.primitive_type, $.boolean,
      $.value, $.place, $.raw_target, $.brand,
      $.number, $.string, $.identifier, $.operator,
      ',', ';', '.', ':',
    ),

    brace_group: $ => seq('{', repeat($._item), '}'),
    paren_group: $ => seq('(', repeat($._item), ')'),
    bracket_group: $ => seq('[', repeat($._item), ']'),

    keyword: _ => choice(
      'type', 'struct', 'enum', 'fn', 'unsafe', 'extern', 'data', 'trait', 'instance',
      'forall', 'where', 'exists', 'foreach', 'of', 'using', 'as',
      'locals', 'entry', 'block', 'state', 'empty', 'known', 'open',
      'return', 'jump', 'branch', 'switch', 'unreachable',
    ),

    instruction: _ => choice(
      'const', 'copy', 'construct', 'destruct', 'construct_variant', 'variant_tag',
      'load', 'store', 'alloca', 'call', 'trait_call', 'pack',
      'add', 'sub', 'mul', 'div', 'mod', 'eq', 'ne', 'lt', 'le', 'gt', 'ge',
      'bit_and', 'bit_or', 'bit_xor', 'shl', 'shr',
      'int_cast', 'float_cast', 'to_ptr', 'cast', 'sizeof', 'fresh', 'static_brand', 'fence',
      'ptr_load', 'ptr_store', 'ptr_load_volatile', 'ptr_store_volatile', /ptr_atomic_[a-z_]+/,
    ),

    primitive_type: _ => choice(
      'bool', 'i8', 'i16', 'i32', 'i64', 'isize', 'u8', 'u16', 'u32', 'u64', 'usize', 'f32', 'f64',
      'unit', 'Never',
    ),

    boolean: _ => choice('true', 'false'),

    // `%v2`: a value.
    value: _ => token(seq('%', NAME)),

    // `&counter#1.value`, `&choice.Some.0`: a place and its path.
    place: _ => token(seq('&', NAME, repeat(seq('.', /[A-Za-z0-9_#]+/)))),

    // `&*%p`: the target of a raw pointer.
    raw_target: _ => token(seq('&*%', NAME)),

    brand: _ => token(seq("'", /[A-Za-z0-9_#]+/)),

    number: _ => token(/-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?/),

    string: _ => token(seq('"', repeat(choice(/[^"\\]/, /\\./)), '"')),

    identifier: _ => NAME,

    operator: _ => choice('=', '->', '=>', '..=', '|', '*', '&', '!', '_'),

    comment: _ => token(seq('//', /[^\n]*/)),
  },
});
