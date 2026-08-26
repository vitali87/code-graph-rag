# Latest News

Newest first. Entries are the project's headline **features and capabilities**
for users: new language support, analysis, querying, graph, and integrations.
They are NOT for CI, developer tooling, release or build automation, refactors,
documentation, tests, or bug fixes; leave that kind out. Every entry above the
`latest-release-end` marker is rendered into the README's "Latest News"
section by `scripts/generate_readme.py`, so edit entries here rather than in
the README. The release workflow prepends feature entries via
`scripts/update_news.py`, derived from the release's generated Highlights
(dropping non-feature themes), and moves the marker below the block it
inserted; hand edits remain welcome between releases and render too.

- **Graph Indexing**: The name property is now indexed for improved graph read-path lookups. SQL routines are also indexed to resolve calls that name their target in a string.
- **Duplicate Code Detection**: An AST-based duplicate code detection system has been introduced, alongside fixes for duplicate detection across multiple repositories.
- **Agentic QA**: A new agentic QA benchmark harness and indexing-time benchmark have been added.
<!-- latest-release-end -->
- **Java Taint Improvements**: Enhanced taint tracking in Java, including handling JDK shims, chained call receivers, literal arguments, and type-test patterns.
- **C# Taint Propagation**: Improved taint propagation in C# with refinements to argument binding, tuple deconstruction, and await plumbing methods.
- **Semantic Frontend Enhancements**: Added in-process Jedi semantic frontend for Python and re-run semantic frontends on the watch path for more accurate analysis.
- **Protocol Buffer Indexing**: Introduced a canonical protobuf index with provenance manifest and a verify command for improved data integrity.
- **Structural Analysis**: Added structural snapshot diffs between protobuf indexes and structural ast-grep support for seven additional languages.
- **Runtime Call Tracing**: A dynamic tracer runs your code (typically the test suite) and merges the calls that actually happened into the graph as `CALLS` edges (flagged where static analysis missed them), so dispatch through interfaces, virtual methods, function pointers, reflection, and framework routing becomes visible. Convert a run from Python, the JVM, Node.js, .NET, PHP, Lua, Dart, Go, Rust, or C/C++ with `cgr trace`, or ingest production pprof profiles from an eBPF continuous profiler (Parca, Pyroscope, OpenTelemetry) with `cgr trace convert --format ebpf`.
- **Ruby Support**: Ruby joins the graph through a new pluggable ast-grep tier that adds a language from a single YAML pattern file, emitting `Module`, `Function`, and `Class` nodes plus import edges without a hand-written parser.
- **Structural Search & Replace**: Find and rewrite code by AST pattern with ast-grep, exposed as agent tools so you can match and transform structure across the whole codebase instead of relying on text or regex.
- **Data-Flow Tracing**: New `FLOWS_TO` taint edges follow values through assignments, function calls, and I/O sinks. This release adds C#, Java, C, and Go, bringing tracing to 10 languages (Python, JavaScript, TypeScript/TSX, Go, Java, Rust, C++, C, and C#).
- **C# and Dart Support**: Full C# (with Roslyn semantic analysis) and Dart/Flutter now join the graph, bringing the total to 14 supported languages.
