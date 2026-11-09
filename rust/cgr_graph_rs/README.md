# cgr-graph-rs

Native acceleration for [code-graph-rag](https://github.com/vitali87/code-graph-rag)'s
embedded graph, written in Rust with [PyO3](https://pyo3.rs) and built with
[maturin](https://www.maturin.rs).

code-graph-rag requires it, and installs it for you on Linux (x86_64 and
aarch64, glibc and musl), macOS and Windows x64. It has no pure-Python
stand-in: code-graph-rag does not run without it, so it does not install on
other platforms.

It provides:

- the Cypher lexer and parser;
- the graph procedures (PageRank, degree centrality, weakly and strongly
  connected components, betweenness centrality, ancestors and descendants);
- the ingestor's batch writes;
- decoding of query results;
- the indexer's definition hashes, Python scope walks, interface
  fingerprints, callable-parameter scans and Python local-alias walks,
  computed on the trees py-tree-sitter parsed;
- a strict reader for the XML project files it indexes, which never expands
  an entity a document declares, and the TOML writer for its workspace files
  (`src/formats`, written without any third-party crate).

It is not meant to be used on its own. See
[Graph Backends](https://github.com/vitali87/code-graph-rag/blob/main/docs/architecture/embedded-graph.md)
for how code-graph-rag uses it.
