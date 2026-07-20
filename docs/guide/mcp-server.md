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
  --env CYPHER_MODEL=gpt-4 \
  --env CYPHER_API_KEY=your-api-key \
  -- code-graph-rag mcp-server
```

**If installed from source:**

```bash
claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH=/absolute/path/to/your/project \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-4 \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server
```

### Using Current Directory

```bash
cd /path/to/your/project

claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH="$(pwd)" \
  --env CYPHER_PROVIDER=google \
  --env CYPHER_MODEL=gemini-2.0-flash \
  --env CYPHER_API_KEY=your-google-api-key \
  -- uv run --directory /absolute/path/to/code-graph-rag code-graph-rag mcp-server
```

## Prerequisites

```bash
git clone https://github.com/vitali87/code-graph-rag.git
cd code-graph-rag
uv sync

docker run -p 7687:7687 -p 7444:7444 memgraph/memgraph-platform
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
| `query_code_graph` | Query the codebase knowledge graph using natural language. Use semantic_search unless you know the exact names of classes/functions you are searching for. Ask questions like 'What functions call UserService.create_user?' or 'Show me all classes that implement the Repository interface'. |
| `get_code_snippet` | Retrieve source code for a function, class, or method by its qualified name. Returns the source code, file path, line numbers, and docstring. |
| `surgical_replace_code` | Surgically replace an exact code block in a file using diff-match-patch. Only modifies the exact target block, leaving the rest unchanged. |
| `read_file` | Read the contents of a file from the project. Supports pagination for large files. |
| `write_file` | Write content to a file, creating it if it doesn't exist. |
| `list_directory` | List contents of a directory in the project. |
| `semantic_search` | Performs a semantic search for functions based on a natural language query describing their purpose, returning a list of potential matches with similarity scores. Requires the 'semantic' extra to be installed. |
| `structural_search` | Search code structurally by AST pattern using ast-grep syntax (not text/regex). Returns file paths, line and column numbers, and the matched code. Requires the 'ast-grep' extra to be installed. |
| `structural_replace` | Rewrite code structurally by AST pattern using ast-grep syntax. Metavariables captured by the pattern are substituted into the rewrite. Defaults to dry_run (returns a diff); set dry_run=false to write changes. Requires the 'ast-grep' extra to be installed. |
| `ask_agent` | Ask the Code Graph RAG agent a question about the codebase. Uses the full RAG pipeline to analyze the code graph and provide a detailed answer. Use this for general questions about architecture, functionality, and code relationships. |
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
    --env CYPHER_MODEL=gpt-4 \
    --env CYPHER_API_KEY=sk-...
    ```

=== "Google Gemini"

    ```bash
    --env CYPHER_PROVIDER=google \
    --env CYPHER_MODEL=gemini-2.5-flash \
    --env CYPHER_API_KEY=...
    ```

=== "Ollama (free, local)"

    ```bash
    --env CYPHER_PROVIDER=ollama \
    --env CYPHER_MODEL=llama3.2
    ```

## Multi-Repository Setup

Add separate named instances for different projects:

```bash
claude mcp add --transport stdio code-graph-rag-backend \
  --env TARGET_REPO_PATH=/path/to/backend \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-4 \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server

claude mcp add --transport stdio code-graph-rag-frontend \
  --env TARGET_REPO_PATH=/path/to/frontend \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-4 \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server
```

!!! warning
    Only one repository can be indexed at a time per MCP instance. When you index a new repository, the previous repository's data is automatically cleared.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Can't find uv/code-graph-rag | Use absolute paths from `which uv` |
| Wrong repository analyzed | Set `TARGET_REPO_PATH` to an absolute path |
| Memgraph connection failed | Ensure `docker ps` shows Memgraph running |
| Tools not showing | Run `claude mcp list` to verify installation |

## Remove

```bash
claude mcp remove code-graph-rag
```
