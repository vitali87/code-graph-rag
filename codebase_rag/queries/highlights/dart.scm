; tree-sitter-dart ships no HIGHLIGHTS_QUERY in its pip package, so this
; fallback is Dart's only highlights source. Annotations (`@override`,
; `@deprecated`) are `annotation` nodes; `const` is not a named token kind in
; this grammar, so it is not listed.
(annotation) @attribute

[
  "static"
  "final"
  "abstract"
  "late"
  "external"
  "covariant"
  "get"
  "set"
] @keyword.modifier
