; Plain-JS modifier tokens only: public/private/protected/readonly/abstract/
; declare/override are TypeScript-only node kinds, and one invalid token makes
; the WHOLE concatenated highlights query fail to compile, silently zeroing
; modifiers and decorators for the language (issue #525).
(decorator) @function.decorator

[
  "async"
  "export"
  "default"
  "static"
  "get"
  "set"
] @keyword.modifier
