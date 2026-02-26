---
description: "Query your codebase with natural language using Code-Graph-RAG's interactive CLI."
---

# Interactive Querying

Code-Graph-RAG lets you ask questions about your codebase in plain English. The system translates your questions into Cypher queries, executes them against the knowledge graph, and returns relevant results with source code snippets.

## Starting the CLI

```bash
cgr start --repo-path /path/to/your/repo
```

## Example Queries

### Finding Code Elements

- "Show me all classes that contain 'user' in their name"
- "Find functions related to database operations"
- "What methods does the User class have?"
- "Show me functions that handle authentication"
- "List all TypeScript components"
- "Find Rust structs and their methods"
- "Show me Go interfaces and implementations"

### Analyzing Relationships

- "Find all functions that call each other"
- "What classes are in the user module"
- "Show me functions with the longest call chains"
- "What functions call UserService.create_user?"
- "Show me all classes that implement the Repository interface"

### C++ Specific Queries

- "Find all C++ operator overloads in the Matrix class"
- "Show me C++ template functions with their specializations"
- "List all C++ namespaces and their contained classes"
- "Find C++ lambda expressions used in algorithms"

### Code Editing Queries

- "Add logging to all database connection functions"
- "Refactor the User class to use dependency injection"
- "Convert these Python functions to async/await pattern"
- "Add error handling to authentication methods"
- "Optimize this function for better performance"

## Semantic Code Search

Search for functions by describing what they do, rather than by exact names:

- "error handling functions"
- "authentication code"
- "database connection setup"

Semantic search uses UniXcoder embeddings and requires the `semantic` extra:

```bash
pip install 'code-graph-rag[semantic]'
```

## Agentic Tools

The interactive agent has access to these tools:

| Tool | Description |
|------|-------------|
| `query_graph` | Query the knowledge graph using natural language |
| `read_file` | Read the content of text-based files |
| `create_file` | Create a new file with content |
| `replace_code` | Surgically replace specific code blocks |
| `list_directory` | List directory contents |
| `analyze_document` | Analyze documents (PDFs, images) |
| `execute_shell` | Execute shell commands from allowlist |
| `semantic_search` | Semantic function search by description |
| `get_function_source` | Retrieve source code by node ID |
| `get_code_snippet` | Retrieve source code by qualified name |

## Intelligent File Editing

The agent uses AST-based function targeting with Tree-sitter for precise code modifications:

- **Visual diff preview** before changes
- **Surgical patching** that only modifies target code blocks
- **Multi-language support** across all supported languages
- **Security sandbox** preventing edits outside project directory
- **Smart function matching** with qualified names and line numbers
