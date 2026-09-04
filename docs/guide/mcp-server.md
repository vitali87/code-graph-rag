---
description: "Integrate Code-Graph-RAG with Claude Code as an MCP server for natural language codebase analysis."
---

# MCP Server (Claude Code Integration)

Code-Graph-RAG can run as an MCP (Model Context Protocol) server, enabling seamless integration with Claude Code and other MCP clients.

## Quick Setup

**If installed via pip** (and `code-graph-rag` is on your PATH):

```bash
claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH=/absolute/path/to/your/project \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-5.6-luna \
  --env CYPHER_API_KEY=your-api-key \
  -- code-graph-rag mcp-server
```

**If installed from source:**

```bash
claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH=/absolute/path/to/your/project \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-5.6-luna \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server
```

### Using Current Directory

```bash
cd /path/to/your/project

claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH="$(pwd)" \
  --env CYPHER_PROVIDER=google \
  --env CYPHER_MODEL=gemini-3.5-flash-lite \
  --env CYPHER_API_KEY=your-google-api-key \
  -- uv run --directory /absolute/path/to/code-graph-rag code-graph-rag mcp-server
```

## Prerequisites

```bash
git clone https://github.com/vitali87/code-graph-rag.git
cd code-graph-rag
uv sync

cgr daemon up
```

## Available Tools

<!-- SECTION:mcp_tools -->
| Tool | Description |
|----|-----------|
| `list_projects` | List all indexed projects in the knowledge graph database. Returns a list of project names that have been indexed. |
| `delete_project` | Delete a specific project from the knowledge graph database. This removes all nodes associated with the project while preserving other projects. Use list_projects first to see available projects. |
| `wipe_database` | WARNING: Completely wipe the entire database, removing ALL indexed projects. This cannot be undone. Use delete_project for removing individual projects. |
| `index_repository` | WARNING: Clears all data for the current project including its embeddings. Parse and ingest the repository into the Memgraph knowledge graph. Use update_repository for incremental updates. Only use when explicitly requested. |
| `update_repository` | Update the repository in the Memgraph knowledge graph without clearing existing data. Use this for incremental updates. |
| `reingest` | Re-ingest specific files into the knowledge graph after editing them. Re-parses only the given files and the files that depend on them, and re-resolves calls in that set only, so an edit lands in the graph in the time it takes to parse the affected dependents (hundreds of milliseconds for a typical file, seconds for a hub imported by dozens) instead of a full update_repository pass. Paths are relative to the project root; files that no longer exist are removed from the graph; paths the project's ignore rules exclude are skipped and reported rather than indexed. Repository-wide passes are not re-run: code-quality findings (smells, vulnerabilities, patterns) and URL-to-endpoint links are rebuilt only by update_repository. Returns the files re-parsed, the dependents re-parsed with them, the files removed, the paths skipped, and the elapsed milliseconds. |
| `resolve` | Resolve a name or a location to qualified names in the graph. `target` is a qualified name, a bare name like `helper` or `Store.get`, or `path:line` (repo-relative path, 1-based line) for the definitions spanning that line, innermost first. Exact matches come first, then dotted-suffix matches, then same-name matches. Deterministic: fixed graph queries, no LLM, same graph gives the same JSON. Use this instead of query_code_graph whenever you know the exact name or location.  |
| `definition` | File, line span, docstring and source of one definition by qualified name (`found` is false when the graph has no such node). Deterministic: fixed graph queries, no LLM, same graph gives the same JSON. Use this instead of query_code_graph whenever you know the exact name or location.  |
| `callers` | Call sites that invoke a qualified name, one row per site with the caller, file, line, column, argument count and keyword names taken from the CALLS edges; `depth` > 1 follows the callers' callers (`through` names the callee each site invokes). Deterministic: fixed graph queries, no LLM, same graph gives the same JSON. Use this instead of query_code_graph whenever you know the exact name or location.  |
| `callees` | Call sites inside a qualified name, one row per site with the callee and the location of the call; `depth` > 1 follows the callees' callees. Deterministic: fixed graph queries, no LLM, same graph gives the same JSON. Use this instead of query_code_graph whenever you know the exact name or location.  |
| `implementors` | Types that inherit from or implement a class, interface or trait (INHERITS / IMPLEMENTS edges). Deterministic: fixed graph queries, no LLM, same graph gives the same JSON. Use this instead of query_code_graph whenever you know the exact name or location.  |
| `overrides` | Methods overriding a method, and the method it overrides (OVERRIDES edges in both directions). Deterministic: fixed graph queries, no LLM, same graph gives the same JSON. Use this instead of query_code_graph whenever you know the exact name or location.  |
| `importers` | Modules that import a module, with each import statement's line, column, bound alias and imported symbol. Deterministic: fixed graph queries, no LLM, same graph gives the same JSON. Use this instead of query_code_graph whenever you know the exact name or location.  |
| `tests_reaching` | Test functions and methods from which a qualified name is reachable through CALLS / REFERENCES / INSTANTIATES, with the distance and the symbol each test reaches it through: what to run after editing it. Deterministic: fixed graph queries, no LLM, same graph gives the same JSON. Use this instead of query_code_graph whenever you know the exact name or location.  |
| `query_code_graph` | Prefer the deterministic tools (resolve, definition, callers, callees, implementors, overrides, importers, tests_reaching) when you know the exact name or location: they run fixed queries with no LLM. Use this for open-ended questions. Query the codebase knowledge graph using natural language. Ask questions like 'What functions call UserService.create_user?' or 'Show me all classes that implement the Repository interface'. Pass `project` to restrict results to one indexed project; use list_projects for the available names. Omit it to search them all. The scope is enforced on the results before they are capped, so it holds regardless of the query generated and the result limit is spent on rows from the requested project. |
| `get_code_snippet` | Retrieve source code for a function, class, or method by its qualified name. Returns the source code, file path, line numbers, and docstring. |
| `surgical_replace_code` | Surgically replace an exact code block in a file using diff-match-patch. Only modifies the exact target block, leaving the rest unchanged. After a successful write on an indexed project, the touched files are re-ingested and a structural delta is appended to the result as JSON: symbols added, removed and renamed, callers left dangling, call sites passing too many arguments, signature changes with a verdict per call site, new duplicates of existing functions, new import cycles, and the tests reaching the edited symbols. A project that is not indexed appends nothing, and a failed analysis appends an error note instead of a delta. Read it before the next edit. |
| `read_file` | Read the contents of a file from the project. Supports pagination for large files. |
| `write_file` | Write content to a file, creating it if it doesn't exist. After a successful write on an indexed project, the touched files are re-ingested and a structural delta is appended to the result as JSON: symbols added, removed and renamed, callers left dangling, call sites passing too many arguments, signature changes with a verdict per call site, new duplicates of existing functions, new import cycles, and the tests reaching the edited symbols. A project that is not indexed appends nothing, and a failed analysis appends an error note instead of a delta. Read it before the next edit. |
| `list_directory` | List contents of a directory in the project. |
| `semantic_search` | Performs a semantic search for functions based on a natural language query describing their purpose, returning a list of potential matches with similarity scores. Requires the 'semantic' extra to be installed. Pass `project` to restrict results to one indexed project; use list_projects for the available names. Omit it to search them all. |
| `structural_search` | Search code structurally by AST pattern using ast-grep syntax (not text/regex). Returns file paths, line and column numbers, and the matched code. Requires the 'ast-grep' extra to be installed. |
| `structural_replace` | Rewrite code structurally by AST pattern using ast-grep syntax. Metavariables captured by the pattern are substituted into the rewrite. Defaults to dry_run (returns a diff); set dry_run=false to write changes. Requires the 'ast-grep' extra to be installed. After a successful write on an indexed project, the touched files are re-ingested and a structural delta is appended to the result as JSON: symbols added, removed and renamed, callers left dangling, call sites passing too many arguments, signature changes with a verdict per call site, new duplicates of existing functions, new import cycles, and the tests reaching the edited symbols. A project that is not indexed appends nothing, and a failed analysis appends an error note instead of a delta. Read it before the next edit. |
| `find_duplicate_code` | Finds structurally duplicated functions and methods (copy-pastes, including renamed and lightly edited copies) by comparing AST fingerprints stored in the graph. Returns clone groups with file:line locations, largest first: 'exact' groups are certain copies, 'similar' pairs carry a branch-overlap score. Use it to answer DRY questions ('where is this logic repeated?') and before writing a new helper to check whether an implementation already exists. Tune with 'threshold' (0-1 similarity, default 0.8) and 'min_size' (skeleton nodes, filters trivial getters). |
| `get_function_source` | Retrieves the source code for a specific function or method using its internal node ID, typically obtained from a semantic search result. |
| `ask_agent` | Ask the Code Graph RAG agent a question about the codebase. Uses the full RAG pipeline to analyse the code graph and provide a detailed answer. Use this for general questions about architecture, functionality, and code relationships. |
| `flow_verdict` | Answer a source-to-sink data-flow reachability question with one of three verdicts: FOUND (a FLOWS_TO path exists, returned as qualified names), NO_FLOW (no path, and every module of the project was inside flow-analysis coverage), or UNKNOWN (no path found, but part of the project sits outside coverage; the uncovered files are named). An absent path must never be read as a verified absence when coverage gaps exist. |
| `explain_traceback` | Correlate a Python traceback with the code graph: each frame is resolved to its Function/Method/Module node and returned with its graph neighbourhood (callers, callees, and FLOWS_TO sources feeding it). Frames outside the repository or unknown to the graph carry an unresolved reason instead. Use this to ground a failure report in the indexed code before deciding where to look. |
| `rank_root_causes` | Rank the sites that can explain a Python traceback's failure, best first. The anchor (failing) is the innermost frame the graph resolves; anchor_is_crash_site is false when the actual crash line sits deeper (a library frame, or a frame the graph cannot match), so the ranking reads as relative to the deepest resolvable frame. Candidates score by three additive signals: being a FLOWS_TO source into the failing frame (a possible producer of the failing value), sitting on the crashing stack itself, and reaching the failing frame through CALLS edges (closer callers score higher). Each candidate carries its file, definition line, reasons, and the call path to the failure. When the project has no FLOWS_TO edges the ranking degrades to a CALLS-only walk and flow_used is false; flow_gaps always names the files outside flow-analysis coverage. |
<!-- /SECTION:mcp_tools -->

## Example Usage

```
> Index this repository
> What functions call UserService.create_user?
> Update the login function to add rate limiting
```

## LLM Provider Options

=== "OpenAI"

    ```bash
    --env CYPHER_PROVIDER=openai \
    --env CYPHER_MODEL=gpt-5.6-luna \
    --env CYPHER_API_KEY=sk-...
    ```

=== "Google Gemini"

    ```bash
    --env CYPHER_PROVIDER=google \
    --env CYPHER_MODEL=gemini-3.5-flash-lite \
    --env CYPHER_API_KEY=...
    ```

=== "Ollama (free, local)"

    ```bash
    --env CYPHER_PROVIDER=ollama \
    --env CYPHER_MODEL=qwen2.5-coder
    ```

## Multi-Repository Setup

Add separate named instances for different projects:

```bash
claude mcp add --transport stdio code-graph-rag-backend \
  --env TARGET_REPO_PATH=/path/to/backend \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-5.6-luna \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server

claude mcp add --transport stdio code-graph-rag-frontend \
  --env TARGET_REPO_PATH=/path/to/frontend \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-5.6-luna \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server
```

!!! warning
    Only one repository can be indexed at a time per MCP instance. When you index a new repository, the previous repository's data is automatically cleared.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Can't find uv/code-graph-rag | Use absolute paths from `which uv` |
| Wrong repository analysed | Set `TARGET_REPO_PATH` to an absolute path |
| Memgraph connection failed | Ensure `docker ps` shows Memgraph running |
| Tools not showing | Run `claude mcp list` to verify installation |

## Remove

```bash
claude mcp remove code-graph-rag
```
