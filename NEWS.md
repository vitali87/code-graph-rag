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

- **CodeQL Scanning**: Switch to advanced CodeQL setup for more comprehensive analysis of pull requests from forks.
- **Python Annotations**: Infer Python instance attributes from annotations for improved type handling.
- **Xdebug Trace Support**: Now reads gzip-compressed Xdebug traces for enhanced debugging.
- **Rust Macro Edge**: Ensures turbofish calls within macros receive their CALLS edge.
- **Improved Syncing**: Re-parse files attaching members to re-indexed modules and keep declared namespaces of unchanged PHP files.
<!-- latest-release-end -->
- **GitHub Sponsors**: Added support for GitHub Sponsors via the command line.
- **Signature**: Implemented header reading and argument binding for rewrite functionality.
- **Migration**: Prevented unintended purging of shared Folder/File nodes.
- **Engine Operations**: Restored `delete-project` and stats execution on the embedded engine.
- **Dependencies**: Updated `mcp` to version 1.30.0 to address GHSA-84m7-p3x7-pcfv.
- **Contextual Queries**: Support for more detailed context queries has been added, including reasons and evaluation ingestor reads, enhancing the understanding of code interactions.
- **Graph Improvements**: The graph analysis capabilities have been expanded to follow cross-service links and implementors/overrides, providing a more comprehensive view of code dependencies.
- **Improved Logging & Error Handling**: Logging has been refined to provide more context, including per-symbol indexing and connection lifecycle details. Error messages are clearer, and the CLI handles unreachable or login-refusing Memgraph instances gracefully.
- **Expanded Language Support & Features**: Julia is now a fully supported language, and new features include context-slice queries, isolated check capture/restore, and improved support for various language-specific constructs.
- **Refined Data Flow & Graph Representation**: Improvements to how data flow and graph relationships are represented, including better handling of re-exports, overrides, and project dependencies.
- **Enhanced CLI Scope**: `cgr stats` and `cgr export` now support scoping to specific projects or workspaces, and offer improved error handling.
- **Improved Logging**: Logging has been refined to provide more informative messages, including per-symbol indexing and connection lifecycle details, and reduced unnecessary output.
- **Expanded Language Support**: Added support for Julia as a supported language.
- **Refined Graph Navigation**: Improved handling of cross-service links in flow verdicts and signature deltas, and enhanced the ability to follow re-exports.
- **Contextual Improvements**: Added support for context-slice queries, reasons, and the necessary ingestor reads.
- **Incremental Indexing**: Improved handling of same-stem sibling matches and skipped EXPOSES cleanups during incremental updates.
- **Real-Time Watcher**: `cgr watch` ships the file watcher with the package, so an installed cgr keeps a repository's graph in sync while you edit, in the same project `cgr start` syncs.
- **Vector Store Improvements**: Enhanced handling of Milvus collection sizes and embedding size checks to prevent errors.
- **Provider Configuration**: Improved default endpoint handling for providers when configuration is not explicitly set.
- **Project Scope**: Increased project-level scoping for exclusions, module deletions, and other operations.
- **CLI Enhancements**: Added a Credits page to the documentation and improved the status reporting for CLI syncs.
- **Dependency Updates**: Updated several dependencies including transformers, actions, and SonarSource tools.
- **Code Generation**: Split Cypher response cleaning and AST-grep rule validation into smaller, more manageable helpers for improved clarity and maintainability.
- **Scripting Improvements**: Resolved an issue where closed pull request run associations were incorrectly reported as missing, and improved error reporting in scripts to avoid over-claiming failing checks.
- **Data Handling**: Enhanced the handling of CONTAINS_SECTION in the double's module subtree and refined snippet lookup to project the matched node's own path.
- **Cross-Service Consumers**: `endpoints`, `endpoint_callers` and `remote_dependencies` MCP tools read the EXPOSES / RESOLVES_TO edges, and `cgr dead-code --no-endpoint-roots` reports an endpoint no indexed call site reaches.
- **UTF-8 Handling**: Improved handling of invalid UTF-8 bytes prevents file definitions from being dropped.
- **Neo4j Backend Support**: Added support for Neo4j as a graph backend through a pluggable dialect seam.
- **Gloss Node & Notes**: Introduced the Gloss node for agent-authored notes and enabled writing and reading Gloss notes via the MCP server.
- **File System Operations**: Improved handling of file system operations with platform-neutral joins and safeguards against removing project root.
- **Cache Management**: Enhanced cache management with atomic publishing, dedicated cache stamps, and improved deletion handling for orphan caches.
- **Graph Querying**: Introduced deterministic graph query tools for more precise analysis of code relationships.
- **Re-ingestion & Parsing**: Improved re-ingestion processes, including handling of deleted files, re-parsing of related files, and rebuilding of key data structures.
- **Security & Stability**: Addressed security concerns by blocking removal of project root and improved stability by preventing skipped edits and handling orphaned caches.
- **C++ & Go Integration**: Enhanced integration with C++ and Go, including parsing of module interfaces and handling of module-level class aliases.
- **Index Consistency**: Ensure incremental deletes match clean index states.
- **Patch Reporting**: Distinguish between unverifiable and verified patches in reports.
- **Windows Teardown**: Clear the read-only bit when removing a temp repo for successful Windows teardown.
- **TypeScript Grading**: Grade TypeScript inheritance against the tsc oracle for improved accuracy.
- **Exclude Set Handling**: Record the exclusion set to ensure `--exclude` flags are correctly applied across runs.
- **MCP Indexing**: Honour `.cgrignore` and `.gitignore` in MCP index and update processes, matching the CLI.
- **Transactional Multi-File Edits**: Multi-file edits now run as a transaction with staged verification and undo, alongside concrete-syntax-preserving patchers per language and import rewriting for renames and moves.
- **Scala Inheritance and Markdown Front-Matter**: Scala `INHERITS` edges are read from `extends`/`with` clauses, and declared Markdown front-matter is read onto the `Module` node.
- **Graph Retrieval**: MCP and CLI graph retrieval is scoped to one project, and call-site and import-site locations are now stored on graph edges.
- **Direct MCP Tools**: `find_duplicate_code` and `get_function_source` are exposed as direct MCP tools, and PHP `use function` imports resolve through declared namespaces.
- **Graph Indexing**: The name property is now indexed for improved graph read-path lookups. SQL routines are also indexed to resolve calls that name their target in a string.
- **Duplicate Code Detection**: An AST-based duplicate code detection system has been introduced, alongside fixes for duplicate detection across multiple repositories.
- **Agentic QA**: A new agentic QA benchmark harness and indexing-time benchmark have been added.
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
