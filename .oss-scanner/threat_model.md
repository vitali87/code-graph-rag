# Threat model

This is the scanner's summary of
[docs/architecture/security.md](../docs/architecture/security.md), which is the
project's full security model. Where the two differ, that page wins.

## What this project does and where untrusted input enters

code-graph-rag (`cgr`, `code-graph-rag` on PyPI) parses a codebase into a
knowledge graph (an embedded SQLite graph by default, or Memgraph or Neo4j),
optionally embeds it for semantic search, and answers questions about it. Users
reach it in two ways:

- the interactive CLI agent (`cgr start`), an LLM agent with tools that read,
  search and edit files, run allowlisted shell commands, query the graph and
  search the web
- the MCP server (`cgr mcp-server`), which serves the graph and the same file,
  edit and indexing tools to a coding agent such as Claude Code, over stdio by
  default or over streamable HTTP on `127.0.0.1:8080`

The person running `cgr` is trusted, as are their host, their account, the
environment and flags they start it with, the toolchains on their PATH and the
model providers they configure. Everything below is not:

- **The repository being indexed.** Every file in it is attacker-controlled:
  source in every supported language, build and config files
  (`compile_commands.json`, `go.mod`, `pom.xml`, `.csproj`, `pyproject.toml`,
  `package.json`), `.gitignore` and `.cgrignore`, symlinks, file names, and a
  `.git` directory if it arrives with one. `cgr` is normally started from
  inside that repository (`--repo-path` defaults to the current directory), so
  anything it reads from the working directory is repository content too,
  including a `.env` or `.cgr.md` the repository ships.
- **The LLM's tool calls.** The model reads repository content and web results,
  so it must be assumed prompt-injected. Every argument it passes to a tool
  (paths, shell commands, Cypher, edit blocks, search queries) is untrusted.
- **MCP clients and requests** reaching the HTTP transport, and any web page in
  the user's browser that can send requests to `127.0.0.1`.
- **Web search results** fetched by the research agent.
- **Graph and vector store contents**, when the store is a shared or networked
  Memgraph, Neo4j or Qdrant that someone else can write to.

## Components that matter most / least

These matter most.

- The shell tool, `codebase_rag/tools/shell_command.py`, with its tables in
  `codebase_rag/constants/security.py` and the allowlist in
  `codebase_rag/config.py`. It parses a command line itself, then runs each
  segment with `create_subprocess_exec` in the project root. The guards are
  the allowlist, the subshell, dangerous-command and git-exec checks, the
  launcher checks, and the approval rules (`_requires_approval`,
  `_is_confined_read`). A command that runs a program off the allowlist,
  writes or executes something without approval, or reads outside the
  project root without approval, is a bypass.
- Path confinement for the file tools: `validate_project_path` in
  `codebase_rag/decorators.py`, and `file_reader.py`, `file_writer.py`,
  `file_editor.py`, `directory_lister.py`, `structural_editor.py` and
  `code_retrieval.py` in `codebase_rag/tools/`, plus the MCP handlers that wrap
  them in `codebase_rag/mcp/tools.py`. Reads and writes must stay inside the
  project root, symlinks included (`codebase_rag/utils/path_utils.py`).
- Indexing an untrusted repository must parse it, not run it. That covers the
  tree-sitter parsers in `codebase_rag/parsers/`, the repository walk and
  ignore handling, `parser_loader.py`, and every subprocess or import that
  indexing triggers with default settings: the Go frontend
  (`parsers/go_frontend/`, on by default when `go` is installed), the libclang
  layer of the C/C++ frontend (`parsers/cpp_frontend/`, on by default with the
  `cpp` extra and a compilation database), Lombok preprocessing
  (`parsers/java_lombok.py`, which runs delombok in the default Java mode
  whenever a Lombok JAR is configured or found in `~/.m2` and the repository's
  build files mention Lombok), and `parsers/stdlib_extractor.py`. The javac
  frontend (`JAVA_FRONTEND=javac`) is opt-in but passes `-proc:none`, so it
  must not run repository code either.
- The read-only guarantee for model-written Cypher:
  `codebase_rag/services/llm.py`, `codebase_rag/cypher_guard.py`, the
  `fetch_read_only` path in the graph service, and project scoping in
  `codebase_rag/tools/codebase_query.py`. A generated query must not write,
  delete or call a procedure off the allowlist, and with `project` set must not
  return another project's rows.
- The embedded graph engine, `codebase_rag/embedded_graph/`, which compiles
  Cypher to SQLite SQL (literal quoting in `sql.py`, bound parameters, `=~`
  regex with a deadline, the query timeout and memory cap), and its Rust half,
  `rust/cgr_graph_rs/` (lexer, parser, ingest and decoding, built with PyO3).
- The MCP HTTP server, `codebase_rag/mcp/server.py`: the loopback default, the
  refusal to bind elsewhere without `MCP_HTTP_AUTH_TOKEN`, and the bearer check.
- The release and CI supply chain in `.github/workflows/`, `Dockerfile`,
  `build_binary.py` and `scripts/release.sh`, which publish to PyPI and GHCR.

These matter less but are still in scope.

- The research and web search tools (`tools/research.py`, `tools/web_search.py`)
  and the taint gate in `codebase_rag/taint.py`, beyond the limits that
  security.md already concedes.
- The vector stores (`vector_store.py`, `sqlite_vectors.py`) and the
  Memgraph and Neo4j adapters, including filters built from project names.
- `cgr daemon` and the Compose stack it renders (`codebase_rag/stack/`,
  `codebase_rag/docker-compose.yaml`), which must bind loopback by default.
- Watch mode, workspaces (`codebase_rag/workspaces/`), exports and the
  remaining CLI commands.

The least important are `docs/`, `benchmarks/`, `optimize/`, `evals/`,
`examples/`, `scripts/` other than the release path, `fuzz/`, and
`codebase_rag/tests/`.

## How to exercise it

The repository is at `/src` and installed in editable mode in `/src/.venv`,
which is on PATH, with every extra except `semantic` and `milvus` (so no torch
and no local embedding model) and the dev group. The Rust crate is built from
`rust/cgr_graph_rs`. Go, a JDK and ripgrep are installed, and the go/types
helper's modules are in the module cache. Nothing needs the network, and no
LLM provider is reachable, so exercise the tools directly rather than through
`cgr start`.

- Index a hostile repository with
  `cd /tmp && cgr start --repo-path /tmp/target --update-graph`, which parses
  it, runs the default frontends (the Go helper included), syncs the embedded
  graph at `~/.cgr/graph.sqlite` and exits. It needs no LLM and no database
  server. Run it with the working directory inside the repository as well,
  since that is how users run it. `cgr index --repo-path /tmp/target -o /tmp/out`
  writes the same index as protobuf files instead.
- The shell tool: `create_shell_command_tool` and `ShellCommander` in
  `codebase_rag/tools/shell_command.py`. The fastest way in is the tests:
  `pytest codebase_rag/tests/test_shell_command.py -n 4`, and
  `test_shell_confined_reads.py`, `test_noninteractive_tool_approvals.py`,
  `test_permission_mode.py`.
- Path confinement: `test_file_reader.py`, `test_file_writer.py`,
  `test_file_editor.py`, `test_directory_lister.py`,
  `test_symlink_containment.py`, `test_symlink_policy.py`,
  `test_mcp_read_file.py`, `test_mcp_write_file.py`.
- Cypher: `test_cypher_guard.py`, `test_cypher_validation.py`,
  `test_cypher_project_scope.py`, `test_embedded_graph_engine.py`,
  `test_embedded_graph_rust*.py`. The crate's own tests run with
  `cargo test --manifest-path rust/cgr_graph_rs/Cargo.toml`.
- MCP over HTTP: `cgr mcp-server --transport http` serves `/mcp` on
  `127.0.0.1:8080`; set `MCP_HTTP_AUTH_TOKEN` to require a bearer token. See
  `test_mcp_http_auth.py`, `test_mcp_http_defaults.py`,
  `test_mcp_input_bounds.py`.
- Frontends and indexing subprocesses: `test_go_frontend.py`,
  `test_java_frontend.py`, `test_stdlib_extractor.py`,
  `test_frontend_subprocess_timeouts.py`.
- The atheris harnesses in `fuzz/` cover parsing, the shell classifier and
  incremental updates.
- Run the unit suite with `pytest -n 4 -m "not integration"`; it is large, so
  pass the files you need. Tests marked `integration` need Docker and cannot
  run here.

## How you rate severity

The worst case is an attacker who controls a repository's contents (or a web
page the agent reads) reaching the developer's machine through `cgr`.

- **Critical:** code execution or a file write outside the project root
  triggered just by indexing an untrusted repository, or by opening it in the
  MCP server, with default settings and no approval. Also unauthenticated use
  of the MCP tools from another host or from a web page, and a change an
  outside contributor can make to what the release workflows publish.
- **High:** a command the shell guards should refuse or ask about that runs
  without approval in the default (non-YOLO) mode, such as a program off the
  allowlist, a write, or an exec form of git, rg, sed, awk or find. Also a file
  tool or MCP tool reading or writing outside the project root (traversal,
  symlinks, a race), a model-written Cypher query that writes, deletes or
  calls a disallowed procedure, and secrets from the environment or
  configuration written to the graph, logs or exports.
- **Medium:** a scoped query returning another project's data; data reaching
  a web search backend past the taint gate in a way security.md does not
  already concede; SQL injection in the embedded engine that stays read-only;
  and a crash, hang or unbounded memory or CPU use while indexing a crafted
  repository or running a crafted query (parser recursion, a regex or query
  timeout that does not hold, a Rust panic across the FFI boundary).
- **Low:** a bypass of the checks YOLO mode keeps, local information
  disclosure such as absolute paths, missing hardening, and defects that only
  a setting the operator must change on purpose can reach.

Every report should say which entry point it uses (CLI agent, MCP stdio, MCP
HTTP, indexing), which settings it needs, and whether the user has to approve
anything.

## Reports and patches

Give the version from `pyproject.toml` and the commit, then a self-contained
reproducer: a script that builds the hostile repository or the exact tool
call, and what happens on the host. Patches should follow `CONTRIBUTING.md`:
no new string literals in code files (constants go in
`codebase_rag/constants/`, log text in `codebase_rag/logs.py`, tool error
text in `codebase_rag/tool_errors.py`), and a regression test under
`codebase_rag/tests/` that fails before the fix.

## Anything to leave alone

- Prompt injection that only changes what the model says or proposes, and any
  action the user approved. Approval is the control; an approved command or
  write is trusted by design.
- YOLO mode (Shift+Tab) and `--no-confirm` turn approval off on purpose; only
  the checks documented as remaining in them are in scope (rated low above).
- The C# Roslyn modes (`CSHARP_FRONTEND=auto|hybrid|roslyn`), which the
  operator has to select and which security.md documents as running
  `dotnet restore`, MSBuild and source generators with the user's privileges.
  The tree-sitter default is in scope.
- Network use that security.md documents: model and embedding providers,
  DuckDuckGo or Serpdive search, tiktoken's encoding download, and toolchains
  fetching dependencies.
- The bundled Memgraph and Qdrant containers being unauthenticated when bound
  to loopback, and stale compose files from before the loopback default.
  Local users and processes on the same host are not a boundary.
- The shared MCP bearer token giving every holder the same access; there is no
  per-user authorisation.
- Defects in dependencies (tree-sitter grammars, libclang, the MCP SDK,
  SQLite) that no code-graph-rag code path reaches.
- `docs/`, `benchmarks/`, `optimize/`, `evals/`, `examples/` and
  `codebase_rag/tests/`.
