; tree-sitter-dart ships no HIGHLIGHTS_QUERY in its pip package, so this
; fallback is Dart's only highlights source. Annotations (`@override`,
; `@deprecated`) are `annotation` nodes. `const` and `final` are NAMED nodes
; (const_builtin/final_builtin), not anonymous keyword tokens: a quoted
; "const" fails to compile (and one invalid token kills the whole query).
(annotation) @attribute

(const_builtin) @keyword.modifier
(final_builtin) @keyword.modifier

[
  "static"
  "abstract"
  "late"
  "external"
  "covariant"
  "get"
  "set"
] @keyword.modifier
