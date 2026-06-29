# Claude Code Setup for Code-Graph-RAG MCP Server

Connect Code-Graph-RAG to Claude Code for powerful codebase analysis and editing.

## Quick Setup

Configure the MCP server from your project directory:

```bash
# Navigate to your project first
cd /path/to/your/project

# Add MCP server with project path
claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH="$(pwd)" \
  --env CYPHER_PROVIDER=google \
  --env CYPHER_MODEL=gemini-2.0-flash \
  --env CYPHER_API_KEY=your-google-api-key \
  -- uv run --directory /absolute/path/to/code-graph-rag code-graph-rag mcp-server
```

**Replace**:
- `/absolute/path/to/code-graph-rag` - Where you cloned this repo
- `your-google-api-key` - Your Google AI API key

The `"$(pwd)"` automatically uses your current directory as the target repository.

## Alternative: Explicit Path

Specify the repository path explicitly:

```bash
claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH=/absolute/path/to/your/project \
  --env CYPHER_PROVIDER=google \
  --env CYPHER_MODEL=gemini-2.0-flash \
  --env CYPHER_API_KEY=your-google-api-key \
  -- uv run --directory /absolute/path/to/code-graph-rag code-graph-rag mcp-server
```

**Replace**:
- `/absolute/path/to/your/project` - Your codebase to analyze
- `/absolute/path/to/code-graph-rag` - Where you cloned this repo
- `your-google-api-key` - Your Google AI API key

## Prerequisites

```bash
# 1. Install code-graph-rag
git clone https://github.com/vitali87/code-graph-rag.git
cd code-graph-rag
uv sync

# 2. Start Memgraph
docker run -p 7687:7687 -p 7444:7444 memgraph/memgraph-platform
```

## Usage

```
> Index this repository
> What functions call UserService.create_user?
> Show me how authentication works
> Update the login function to add rate limiting
```

**Important**: Only one repository can be indexed at a time. When you index a new repository, the previous repository's data is automatically cleared from the database. If you need to switch between multiple projects, you'll need to re-index when switching.

## Available Tools

- **index_repository** - Build knowledge graph (clears previous repository data)
- **query_code_graph** - Natural language queries
- **get_code_snippet** - Retrieve code by name
- **surgical_replace_code** - Precise code edits
- **read_file / write_file** - File operations
- **list_directory** - Browse directories

## LLM Provider Options

**OpenAI** (recommended):
```bash
--env CYPHER_PROVIDER=openai \
--env CYPHER_MODEL=gpt-4 \
--env CYPHER_API_KEY=sk-...
```

**Google Gemini**:
```bash
--env CYPHER_PROVIDER=google \
--env CYPHER_MODEL=gemini-2.5-flash \
--env CYPHER_API_KEY=...
```

**Ollama** (free, local):
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

## Troubleshooting

**Can't find uv/code-graph-rag**: Use absolute paths from `which uv`

**Wrong repository analyzed**:
- Without `TARGET_REPO_PATH`: MCP uses the directory where Claude Code is opened
- With `TARGET_REPO_PATH`: MCP always uses that specific path (must be absolute)

**Memgraph connection failed**: Ensure `docker ps` shows Memgraph running

**Tools not showing**: Run `claude mcp list` to verify installation

## Remove

```bash
claude mcp remove code-graph-rag
```
