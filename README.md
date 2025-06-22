<div align="center">
  <picture>
    <source srcset="assets/logo-dark.png" media="(prefers-color-scheme: dark)">
    <source srcset="assets/logo-light.png" media="(prefers-color-scheme: light)">
    <img src="assets/logo-dark.png" alt="Graph-Code Logo" width="480">
  </picture>

  <p>
  <a href="https://github.com/vitali87/code-graph-rag/stargazers">
    <img src="https://img.shields.io/github/stars/vitali87/code-graph-rag?style=social" alt="GitHub stars" />
  </a>
  <a href="https://github.com/vitali87/code-graph-rag/network/members">
    <img src="https://img.shields.io/github/forks/vitali87/code-graph-rag?style=social" alt="GitHub forks" />
  </a>
  <a href="https://github.com/vitali87/code-graph-rag/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/vitali87/code-graph-rag" alt="License" />
  </a>
</p>
</div>

# Graph-Code: A Multi-Language Graph-Based RAG System

A sophisticated Retrieval-Augmented Generation (RAG) system that analyzes multi-language codebases using Tree-sitter, builds comprehensive knowledge graphs, and enables natural language querying of codebase structure and relationships.

<div align="center">
  <img src="https://github.com/ChawlaAvi/code-graph-rag/blob/main/assets/code-rag-demo.gif" alt="ag-ui Logo" style="max-width: 20px; height: auto;" />
</div>

## 🚀 Features

- **🌍 Multi-Language Support**: Supports Python, JavaScript, TypeScript, Rust, and Go codebases
- **🌳 Tree-sitter Parsing**: Uses Tree-sitter for robust, language-agnostic AST parsing
- **📊 Knowledge Graph Storage**: Uses Memgraph to store codebase structure as an interconnected graph
- **🗣️ Natural Language Querying**: Ask questions about your codebase in plain English
- **🤖 AI-Powered Cypher Generation**: Leverages Google Gemini to translate natural language to Cypher queries
- **📝 Code Snippet Retrieval**: Retrieves actual source code snippets for found functions/methods
- **🔗 Dependency Analysis**: Parses `pyproject.toml` to understand external dependencies
- **🎯 Nested Function Support**: Handles complex nested functions and class hierarchies
- **🔄 Language-Agnostic Design**: Unified graph schema across all supported languages

## 🏗️ Architecture

The system consists of two main components:

1. **Graph Updater** (`codebase_rag/graph_updater.py`): Multi-language Tree-sitter based parser that analyzes codebases and ingests data into Memgraph
2. **RAG System** (`codebase_rag/`): Interactive CLI for querying the stored knowledge graph

### Core Components

- **🌳 Tree-sitter Integration**: Language-agnostic parsing using Tree-sitter grammars
- **📊 Graph Database**: Memgraph for storing code structure as nodes and relationships  
- **🤖 LLM Integration**: Google Gemini for natural language processing
- **🔍 Code Analysis**: Advanced AST traversal for extracting code elements across languages
- **🛠️ Query Tools**: Specialized tools for graph querying and code retrieval
- **⚙️ Language Configuration**: Configurable mappings for different programming languages

## 📋 Prerequisites

- Python 3.12+
- Docker & Docker Compose (for Memgraph)
- Google Gemini API key
- `uv` package manager

## 🛠️ Installation

1. **Clone the repository**:
```bash
git clone <repository-url>
cd graph-code
```

2. **Install dependencies**:

For basic Python support:
```bash
uv sync
```

For full multi-language support:
```bash
uv sync --extra treesitter-full
```

This installs Tree-sitter grammars for:
- **Python** (.py)
- **JavaScript** (.js, .jsx) 
- **TypeScript** (.ts, .tsx)
- **Rust** (.rs)
- **Go** (.go)

3. **Set up environment variables**:
```bash
cp .env.example .env
# Edit .env with your configuration
```

Required environment variables:
```env
GEMINI_API_KEY=your-api-key
GEMINI_MODEL_ID=gemini-2.5-pro
MODEL_CYPHER_ID=gemini-2.5-flash-lite-preview-06-17
MEMGRAPH_HOST=localhost
MEMGRAPH_PORT=7687
```

4. **Start Memgraph database**:
```bash
docker-compose up -d
```

## 🎯 Usage

### Step 1: Parse a Repository

Parse and ingest a multi-language repository into the knowledge graph:

```bash
python -m codebase_rag.main --repo-path /path/to/your/repo --update-graph
```

Or use the standalone graph updater:
```bash
python -c "
from codebase_rag.graph_updater import GraphUpdater, MemgraphIngestor
from pathlib import Path

with MemgraphIngestor('localhost', 7687) as ingestor:
    ingestor.clean_database()  # Optional: clear existing data
    ingestor.ensure_constraints()
    updater = GraphUpdater(ingestor, Path('/path/to/your/repo'))
    updater.run()
"
```

**Supported Languages**: The system automatically detects and processes files based on extensions:
- **Python**: `.py` files
- **JavaScript**: `.js`, `.jsx` files
- **TypeScript**: `.ts`, `.tsx` files  
- **Rust**: `.rs` files
- **Go**: `.go` files

### Step 2: Query the Codebase

Start the interactive RAG CLI:

```bash
python -m codebase_rag.main --repo-path /path/to/your/repo
```

Example queries (works across all supported languages):
- "Show me all classes that contain 'user' in their name"
- "Find functions related to database operations"
- "What methods does the User class have?"
- "Show me functions that handle authentication"
- "List all TypeScript components"
- "Find Rust structs and their methods"
- "Show me Go interfaces and implementations"

## 📊 Graph Schema

The knowledge graph uses the following node types and relationships:

### Node Types
- **Project**: Root node representing the entire repository
- **Package**: Language packages (Python: `__init__.py`, etc.)
- **Module**: Individual source code files (`.py`, `.js`, `.ts`, `.rs`, `.go`)
- **Class**: Class/Struct/Enum definitions across all languages
- **Function**: Module-level functions and standalone functions
- **Method**: Class methods and associated functions
- **Folder**: Regular directories
- **File**: All files (source code and others)
- **ExternalPackage**: External dependencies

### Language-Specific Mappings
- **Python**: `function_definition`, `class_definition`
- **JavaScript/TypeScript**: `function_declaration`, `arrow_function`, `class_declaration`
- **Rust**: `function_item`, `struct_item`, `enum_item`, `impl_item`
- **Go**: `function_declaration`, `method_declaration`, `type_declaration`

### Relationships
- `CONTAINS_PACKAGE/MODULE/FILE/FOLDER`: Hierarchical containment
- `DEFINES`: Module defines classes/functions
- `DEFINES_METHOD`: Class defines methods
- `DEPENDS_ON_EXTERNAL`: Project depends on external packages

## 🔧 Configuration

Configuration is managed through environment variables and the `config.py` file:

```python
MEMGRAPH_HOST = "localhost"
MEMGRAPH_PORT = 7687
GEMINI_MODEL_ID = "gemini-2.5-pro"  # Main RAG orchestrator model
MODEL_CYPHER_ID = "gemini-2.5-flash-lite-preview-06-17"  # Cypher generation model
TARGET_REPO_PATH = "."
GEMINI_API_KEY = "required"
```

## 🏃‍♂️ Development

### Project Structure
```
graph-code/
├── codebase_rag/              # RAG system package
│   ├── main.py                # CLI entry point
│   ├── config.py              # Configuration management
│   ├── graph_updater.py       # Tree-sitter based multi-language parser
│   ├── language_config.py     # Language-specific configurations
│   ├── prompts.py             # LLM prompts and schemas
│   ├── schemas.py             # Pydantic models
│   ├── services/              # Core services
│   │   └── llm.py             # Gemini LLM integration
│   └── tools/                 # RAG tools
│       ├── codebase_query.py  # Graph querying tool
│       ├── code_retrieval.py  # Code snippet retrieval
│       └── file_reader.py     # File content reading
├── docker-compose.yaml        # Memgraph setup
├── pyproject.toml            # Project dependencies & language extras
└── README.md                 # This file
```

### Key Dependencies
- **tree-sitter**: Core Tree-sitter library for language-agnostic parsing
- **tree-sitter-{language}**: Language-specific grammars (Python, JS, TS, Rust, Go)
- **pydantic-ai**: AI agent framework for RAG orchestration
- **pymgclient**: Memgraph Python client for graph database operations
- **loguru**: Advanced logging with structured output
- **python-dotenv**: Environment variable management

## 🌍 Multi-Language Support

### Supported Languages & Features

| Language   | Extensions    | Functions | Classes/Structs | Modules | Package Detection |
|------------|---------------|-----------|-----------------|---------|-------------------|
| Python     | `.py`         | ✅        | ✅              | ✅      | `__init__.py`    |
| JavaScript | `.js`, `.jsx` | ✅        | ✅              | ✅      | -                |
| TypeScript | `.ts`, `.tsx` | ✅        | ✅              | ✅      | -                |
| Rust       | `.rs`         | ✅        | ✅ (structs/enums) | ✅    | -                |
| Go         | `.go`         | ✅        | ✅ (structs)    | ✅      | -                |

### Language-Specific Features

**Python**: Full support including nested functions, methods, classes, and package structure
**JavaScript/TypeScript**: Functions, arrow functions, classes, and method definitions
**Rust**: Functions, structs, enums, impl blocks, and associated functions
**Go**: Functions, methods, type declarations, and struct definitions

### Installation Options

```bash
# Basic Python-only support
uv sync

# Full multi-language support  
uv sync --extra treesitter-full

# Individual language support (if needed)
uv add tree-sitter-python tree-sitter-javascript tree-sitter-typescript tree-sitter-rust tree-sitter-go
```

### Language Configuration

The system uses a configuration-driven approach for language support. Each language is defined in `codebase_rag/language_config.py` with:

- **File extensions**: Which files to process
- **AST node types**: How to identify functions, classes, etc.
- **Module structure**: How modules/packages are organized
- **Name extraction**: How to extract names from AST nodes

Adding support for new languages requires only configuration changes, no code modifications.

## 🐛 Debugging

1. **Check Memgraph connection**:
   - Ensure Docker containers are running: `docker-compose ps`
   - Verify Memgraph is accessible on port 7687

2. **View database in Memgraph Lab**:
   - Open http://localhost:3000
   - Connect to memgraph:7687

3. **Enable debug logging**:
   - The RAG orchestrator runs in debug mode by default
   - Check logs for detailed execution traces

## 🤝 Contributing

1. Follow the established code structure
2. Keep files under 100 lines (as per user rules)
3. Use type annotations
4. Follow conventional commit messages
5. Use DRY principles

## 🙋‍♂️ Support

For issues or questions:
1. Check the logs for error details
2. Verify Memgraph connection
3. Ensure all environment variables are set
4. Review the graph schema matches your expectations

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=vitali87/code-graph-rag&type=Date)](https://www.star-history.com/#vitali87/code-graph-rag&Date)