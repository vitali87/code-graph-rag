# Latest News

Newest first. Entries are the project's headline **features and capabilities**
for users: new language support, analysis, querying, graph, and integrations.
They are NOT for CI, developer tooling, release or build automation, refactors,
documentation, tests, or bug fixes — leave that kind out. The top three entries
are rendered into the README's "Latest News" section by
`scripts/generate_readme.py`, so edit them here rather than in the README. The
release workflow also prepends feature entries via `scripts/update_news.py`
(which drops non-feature themes); hand edits remain welcome between releases.

- **Runtime Call Tracing**: A dynamic tracer runs your code (typically the test suite) and merges the calls that actually happened into the graph as `CALLS` edges — flagged where static analysis missed them — so dispatch through interfaces, virtual methods, function pointers, reflection, and framework routing becomes visible. Convert a run from Python, the JVM, Node.js, .NET, PHP, Lua, Dart, or Go with `cgr trace`.
- **Ruby Support**: Ruby joins the graph through a new pluggable ast-grep tier that adds a language from a single YAML pattern file, emitting `Module`, `Function`, and `Class` nodes plus import edges without a hand-written parser.
- **Structural Search & Replace**: Find and rewrite code by AST pattern with ast-grep, exposed as agent tools so you can match and transform structure across the whole codebase instead of relying on text or regex.
- **Data-Flow Tracing**: New `FLOWS_TO` taint edges follow values through assignments, function calls, and I/O sinks. This release adds C#, Java, C, and Go, bringing tracing to 10 languages (Python, JavaScript, TypeScript/TSX, Go, Java, Rust, C++, C, and C#).
- **C# and Dart Support**: Full C# (with Roslyn semantic analysis) and Dart/Flutter now join the graph, bringing the total to 14 supported languages.
