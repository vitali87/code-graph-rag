; tree-sitter-julia ships no HIGHLIGHTS_QUERY in its pip package, so this
; fallback is Julia's only highlights source. Macro invocations (`@assert`)
; are `macro_identifier` children of a macrocall_expression. `break` and
; `continue` are NAMED nodes (break_statement/continue_statement), and
; `in`/`isa` are operators, so they cannot join the anonymous keyword list
; (a quoted string that is not an anonymous token fails to compile, and one
; invalid token kills the whole query).
(macro_identifier) @function.macro

[
  "abstract"
  "baremodule"
  "begin"
  "catch"
  "const"
  "do"
  "else"
  "elseif"
  "end"
  "export"
  "false"
  "finally"
  "for"
  "function"
  "global"
  "if"
  "import"
  "let"
  "local"
  "macro"
  "module"
  "mutable"
  "primitive"
  "quote"
  "return"
  "struct"
  "true"
  "try"
  "type"
  "using"
  "where"
  "while"
] @keyword
