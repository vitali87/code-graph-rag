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

# Graph-Code: A Graph-Based RAG System for Python Codebases

A sophisticated Retrieval-Augmented Generation (RAG) system that analyzes Python repositories, builds knowledge graphs, and enables natural language querying of codebase structure and relationships.

<div align="center">
  <img src="https://github.com/ChawlaAvi/code-graph-rag/blob/main/assets/code-rag-demo.gif" alt="ag-ui Logo" style="max-width: 20px; height: auto;" />
</div>

## 🚀 Features

- **AST-based Code Analysis**: Deep parsing of Python files to extract classes, functions, methods, and their relationships
- **Knowledge Graph Storage**: Uses Memgraph to store codebase structure as an interconnected graph
- **Natural Language Querying**: Ask questions about your codebase in plain English
- **AI-Powered Cypher Generation**: Leverages Google Gemini to translate natural language to Cypher queries
- **Code Snippet Retrieval**: Retrieves actual source code snippets for found functions/methods
- **Dependency Analysis**: Parses `pyproject.toml` to understand external dependencies

## 🏗️ Architecture

The system consists of two main components:

1. **Repository Parser** (`repo_parser.py`): Analyzes Python codebases and ingests data into Memgraph
2. **RAG System** (`codebase_rag/`): Interactive CLI for querying the stored knowledge graph

### Core Components

- **Graph Database**: Memgraph for storing code structure as nodes and relationships
- **LLM Integration**: Google Gemini for natural language processing
- **Code Analysis**: AST traversal for extracting code elements
- **Query Tools**: Specialized tools for graph querying and code retrieval

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
```bash
uv sync
```

3. **Set up environment variables**:
```bash
cp .env.example .env
# Edit .env with your configuration
```

Required environment variables:
```env
GEMINI_API_KEY=your-api-key
GEMINI_MODEL_ID=gemeini-model-handle
MEMGRAPH_HOST=localhost
MEMGRAPH_PORT=7687
```

4. **Start Memgraph database**:
```bash
docker-compose up -d
```

## 🎯 Usage

### Step 1: Parse a Repository

Parse and ingest a Python repository into the knowledge graph:

```bash
python repo_parser.py /path/to/your/python/repo --clean
```

Options:
- `--clean`: Clear existing data before parsing
- `--host`: Memgraph host (default: localhost)
- `--port`: Memgraph port (default: 7687)

### Step 2: Query the Codebase

Start the interactive RAG CLI:

```bash
python -m codebase_rag.main --repo-path /path/to/your/repo
```

Example queries:
- "Show me all classes that contain 'user' in their name"
- "Find functions related to database operations"
- "What methods does the User class have?"
- "Show me functions that handle authentication"

## 📊 Graph Schema

The knowledge graph uses the following node types and relationships:

### Node Types
- **Project**: Root node representing the entire repository
- **Package**: Python packages (directories with `__init__.py`)
- **Module**: Individual Python files
- **Class**: Class definitions
- **Function**: Module-level functions
- **Method**: Class methods
- **Folder**: Regular directories
- **File**: Non-Python files
- **ExternalPackage**: External dependencies

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
GEMINI_MODEL_ID = "gemini-2.5-pro-preview-06-05"
TARGET_REPO_PATH = "."
GEMINI_API_KEY = "required"
```

## 🏃‍♂️ Development

### Project Structure
```
graph-code/
├── repo_parser.py              # Repository analysis and ingestion
├── codebase_rag/              # RAG system package
│   ├── main.py                # CLI entry point
│   ├── config.py              # Configuration management
│   ├── prompts.py             # LLM prompts and schemas
│   ├── schemas.py             # Pydantic models
│   ├── services/              # Core services
│   │   ├── graph_db.py        # Memgraph integration
│   │   └── llm.py             # Gemini LLM integration
│   └── tools/                 # RAG tools
│       ├── codebase_query.py  # Graph querying tool
│       └── code_retrieval.py  # Code snippet retrieval
├── docker-compose.yaml        # Memgraph setup
└── pyproject.toml            # Project dependencies
```

### Key Dependencies
- **pydantic-ai**: AI agent framework
- **pymgclient**: Memgraph Python client
- **loguru**: Advanced logging
- **python-dotenv**: Environment variable management

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