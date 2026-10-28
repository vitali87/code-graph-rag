# cgr-graph-rs

Native acceleration for [code-graph-rag](https://github.com/vitali87/code-graph-rag)'s
embedded graph, written in Rust with [PyO3](https://pyo3.rs) and built with
[maturin](https://www.maturin.rs).

code-graph-rag installs it for you on Linux (x86_64 and aarch64, glibc and
musl), macOS and Windows x64. Every part has a Python twin that gives the same
answers, so on any other platform code-graph-rag runs without it.

It provides:

- the Cypher lexer and parser;
- the graph procedures (PageRank, degree centrality, weakly and strongly
  connected components, betweenness centrality, ancestors and descendants);
- the ingestor's batch writes;
- decoding of query results;
- the indexer's definition hashes, Python scope walks and interface
  fingerprints, computed on the trees py-tree-sitter parsed.

It is not meant to be used on its own. See
[Graph Backends](https://github.com/vitali87/code-graph-rag/blob/main/docs/architecture/embedded-graph.md)
for how code-graph-rag uses it.
