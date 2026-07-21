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

### Analysing Relationships

- "Find all functions that call each other"
- "What classes are in the user module"
- "Show me functions with the longest call chains"
- "What functions call UserService.create_user?"
- "Show me all classes that implement the Repository interface"

### C++ Specific Queries

- "Find all C++ operator overloads in the Matrix class"
- "Show me C++ template functions with their specialisations"
- "List all C++ namespaces and their contained classes"
- "Find C++ lambda expressions used in algorithms"

### Code Editing Queries

- "Add logging to all database connection functions"
- "Refactor the User class to use dependency injection"
- "Convert these Python functions to async/await pattern"
- "Add error handling to authentication methods"
- "Optimise this function for better performance"

## Semantic Code Search

Search for functions by describing what they do, rather than by exact names:

- "error handling functions"
- "authentication code"
- "database connection setup"

Semantic search uses UniXcoder embeddings and requires the `semantic` extra:

```bash
pip install 'code-graph-rag[semantic]'
```

Qdrant remains the default vector store. To use Milvus Lite for semantic
vectors, install the `milvus` extra (`code-graph-rag[semantic,milvus]`), then
set `CGR_VECTOR_STORE_BACKEND=milvus` and `MILVUS_URI` to a local `.db` file
before indexing.

To compute embeddings on an OpenAI-compatible endpoint (OpenAI, Ollama, vLLM)
instead of locally, set `CGR_EMBEDDING_PROVIDER=openai`; see
[Semantic Search](../sdk/semantic-search.md) for configuration.

## Agentic Tools

The interactive agent has access to these tools:

<!-- SECTION:agentic_tools -->
| Tool | Description |
|----|-----------|
| `query_graph` | Query the codebase knowledge graph using natural language questions. Ask in plain English about classes, functions, methods, dependencies, or code structure. Examples: 'Find all functions that call each other', 'What classes are in the user module', 'Show me functions with the longest call chains'. |
| `read_file` | Reads the content of text-based files. Images and PDFs the user references are attached inline; read them directly. |
| `create_file` | Creates a new file with content. IMPORTANT: Check file existence first! Overwrites completely WITHOUT showing diff. Use only for new files, not existing file modifications. |
| `replace_code` | Surgically replaces specific code blocks in files. Requires exact target code and replacement. Only modifies the specified block, leaving rest of file unchanged. True surgical patching. |
| `list_directory` | Lists the contents of a directory to explore the codebase. |
| `execute_shell` | Executes shell commands from allowlist. Read-only commands run without approval; write operations require user confirmation. |
| `semantic_search` | Performs a semantic search for functions based on a natural language query describing their purpose, returning a list of potential matches with similarity scores. |
| `get_function_source` | Retrieves the source code for a specific function or method using its internal node ID, typically obtained from a semantic search result. |
| `get_code_snippet` | Retrieves the source code for a specific function, class, or method using its full qualified name. |
| `structural_search` | Search code by AST pattern using ast-grep syntax (not text/regex). Patterns use metavariables: $NAME matches one node, $$$NAME matches many (e.g. 'print($A)', 'def $F($$$ARGS): $$$BODY'). Returns file:line:column and the matched code. Optional 'language' (e.g. 'python', 'typescript', 'csharp') restricts the search. |
| `structural_replace` | Rewrite code by AST pattern using ast-grep syntax. Give a 'pattern' to match and a 'rewrite' template; metavariables captured by the pattern ($A, $$$ARGS) are substituted into the rewrite. Defaults to dry_run=True, which returns a diff without touching files; call again with dry_run=false to apply. Optional 'language' restricts the rewrite to one language. |
<!-- /SECTION:agentic_tools -->

## Intelligent File Editing

The agent uses AST-based function targeting with Tree-sitter for precise code modifications:

- **Visual diff preview** before changes
- **Surgical patching** that only modifies target code blocks
- **Multi-language support** across all supported languages
- **Security sandbox** preventing edits outside project directory
- **Smart function matching** with qualified names and line numbers
