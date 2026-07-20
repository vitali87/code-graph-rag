# Latest News

Newest first. The top three entries are rendered into the README's "Latest News"
section automatically by `scripts/generate_readme.py`, so edit them here rather
than in the README.

- **Structural Search & Replace**: Find and rewrite code by AST pattern with ast-grep, exposed as agent tools so you can match and transform structure across the whole codebase instead of relying on text or regex.
- **Data-Flow Tracing**: New `FLOWS_TO` taint edges follow values through assignments, function calls, and I/O sinks, with coverage across C#, Java, C, and Go.
- **C# and Dart Support**: Full C# (with Roslyn semantic analysis) and Dart/Flutter now join the graph, bringing the total to 14 supported languages.
