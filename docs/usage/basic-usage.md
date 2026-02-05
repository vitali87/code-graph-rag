# Basic Usage Guide

Complete guide to using Graph-Code for querying and analyzing your codebase.

## Step 1: Parse a Repository

Parse and ingest a repository into the knowledge graph:

### First Repository (Clean Start)

```bash
graph-code start --repo-path /path/to/repo1 --update-graph --clean
```

### Additional Repositories (Preserve Existing Data)

```bash
graph-code start --repo-path /path/to/repo2 --update-graph
graph-code start --repo-path /path/to/repo3 --update-graph
```

### Control Batch Size

```bash
# Flush every 5,000 records instead of default
graph-code start --repo-path /path/to/repo --update-graph --batch-size 5000
```

The system automatically detects and processes files for all supported languages.

---

## Step 2: Query the Codebase

Start the interactive RAG CLI:

```bash
graph-code start --repo-path /path/to/your/repo
```

### Example Queries

```
> Show me all authentication functions
> How does the user login flow work?
> What functions call process_payment?
> Find all database queries in the codebase
> Show me the dependency tree for UserService
```

---

## Common CLI Arguments

| Argument | Description | Example |
|----------|-------------|---------|
| `--repo-path` | Path to repository | `--repo-path /path/to/repo` |
| `--update-graph` | Update knowledge graph | `--update-graph` |
| `--clean` | Clean database before updating | `--clean` |
| `--batch-size` | Override Memgraph flush batch size | `--batch-size 5000` |
| `--orchestrator` | Specify orchestrator model | `--orchestrator anthropic/claude-3-5-sonnet-latest` |
| `--cypher` | Specify cypher model | `--cypher openai/gpt-4o-mini` |

---

## Related Documentation

- **[Advanced: Real-Time Updates](../advanced/real-time-updates.md)** - Auto-sync graph with code changes
- **[Advanced: Graph Export](../advanced/graph-export.md)** - Export and analyze graph data
- **[Advanced: Code Optimization](../advanced/code-optimization.md)** - AI-powered optimization
- **[LLM Configuration](../llm/configuration.md)** - Configure LLM providers
