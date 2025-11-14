# Claude Code Setup for Graph-Code MCP Server

Connect Graph-Code to Claude Code for powerful codebase analysis and editing.

## Quick Setup

```bash
claude mcp add --transport stdio graph-code \
  --env TARGET_REPO_PATH=/absolute/path/to/your/project \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-4 \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /absolute/path/to/code-graph-rag graph-code mcp-server
```

**Replace**:
- `/absolute/path/to/your/project` - Your codebase to analyze
- `/absolute/path/to/code-graph-rag` - Where you cloned this repo
- `your-api-key` - Your OpenAI API key

## Prerequisites

```bash
# 1. Install code-graph-rag
git clone https://github.com/yourusername/code-graph-rag.git
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

## Available Tools

- **index_repository** - Build knowledge graph
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

Add separate instances for different projects:

```bash
claude mcp add --transport stdio graph-code-backend \
  --env TARGET_REPO_PATH=/path/to/backend \
  -- uv run --directory /path/to/code-graph-rag graph-code mcp-server

claude mcp add --transport stdio graph-code-frontend \
  --env TARGET_REPO_PATH=/path/to/frontend \
  -- uv run --directory /path/to/code-graph-rag graph-code mcp-server
```

## Troubleshooting

**Can't find uv/graph-code**: Use absolute paths from `which uv`

**TARGET_REPO_PATH error**: Use absolute paths, not relative

**Memgraph connection failed**: Ensure `docker ps` shows Memgraph running

**Tools not showing**: Run `claude mcp list` to verify installation

## Remove

```bash
claude mcp remove graph-code
```
