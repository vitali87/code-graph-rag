<div align="center">
  <picture>
    <source srcset="assets/logo-dark-any.png" media="(prefers-color-scheme: dark)">
    <source srcset="assets/logo-light-any.png" media="(prefers-color-scheme: light)">
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
  <a href="https://codecov.io/gh/vitali87/code-graph-rag">
    <img src="https://codecov.io/gh/vitali87/code-graph-rag/branch/main/graph/badge.svg" alt="Code Coverage" />
  </a>
  <a href="https://mseep.ai/app/vitali87-code-graph-rag">
    <img src="https://mseep.net/pr/vitali87-code-graph-rag-badge.png" alt="MseeP.ai Security Assessment" height="20" />
  </a>
  <a href="https://code-graph-rag.com">
    <img src="https://img.shields.io/badge/Enterprise-Support%20%26%20Services-6366f1" alt="Enterprise Support" />
  </a>
</p>
</div>

# Graph-Code: A Graph-Based RAG System for Any Codebases

An accurate Retrieval-Augmented Generation (RAG) system that analyzes multi-language codebases using Tree-sitter, builds comprehensive knowledge graphs, and enables natural language querying of codebase structure and relationships as well as editing capabilities.

---

## 📚 Quick Links

- 🚀 **[Quick Start Guide](docs/getting-started/quick-start.md)** - Get running in 30 seconds
- ⚙️ **[LLM Configuration](docs/llm/configuration.md)** - Configure any LLM provider (100+ supported)
- 🔌 **[Claude Code Integration](docs/getting-started/claude-code-setup.md)** - Use as MCP server
- 📖 **[Supported Providers](docs/llm/supported-providers.md)** - Full list of LLM models
- 🤝 **[Contributing](CONTRIBUTING.md)** - How to contribute


![demo](./assets/demo.gif)

## Latest News 🔥

- **[NEW]** **Universal LLM Support**: We've migrated to a universal backend powered by **LiteLLM**, supporting 100+ providers including OpenAI, Anthropic, Gemini, DeepSeek, and Ollama!
  - 📚 [LLM Configuration Guide](docs/llm/configuration.md) - Learn how to set up any provider.
  - ✅ [Supported Models List](docs/llm/supported-providers.md) - View the full list of supported models.

- **[NEW]** **MCP Server Integration**: Graph-Code now works as an MCP server with Claude Code! Query and edit your codebase using natural language directly from Claude Code. [Setup Guide](docs/getting-started/claude-code-setup.md)
- [2025/10/21] **Semantic Code Search**: Added intent-based code search using UniXcoder embeddings. Find functions by describing what they do (e.g., "error handling functions", "authentication code") rather than by exact names.

## 🚀 Features

- **Multi-Language Support**:

<!-- SECTION:supported_languages -->
| Language | Status | Extensions | Functions | Classes/Structs | Modules | Package Detection | Additional Features |
|--------|------|----------|---------|---------------|-------|-----------------|-------------------|
| C++ | Fully Supported | .cpp, .h, .hpp, .cc, .cxx, .hxx, .hh, .ixx, .cppm, .ccm | ✓ | ✓ | ✓ | ✓ | Constructors, destructors, operator overloading, templates, lambdas, C++20 modules, namespaces |
| Java | Fully Supported | .java | ✓ | ✓ | ✓ | - | Generics, annotations, modern features (records/sealed classes), concurrency, reflection |
| JavaScript | Fully Supported | .js, .jsx | ✓ | ✓ | ✓ | - | ES6 modules, CommonJS, prototype methods, object methods, arrow functions |
| Lua | Fully Supported | .lua | ✓ | - | ✓ | - | Local/global functions, metatables, closures, coroutines |
| Python | Fully Supported | .py | ✓ | ✓ | ✓ | ✓ | Type inference, decorators, nested functions |
| Rust | Fully Supported | .rs | ✓ | ✓ | ✓ | ✓ | impl blocks, associated functions |
| TypeScript | Fully Supported | .ts, .tsx | ✓ | ✓ | ✓ | - | Interfaces, type aliases, enums, namespaces, ES6/CommonJS modules |
| C# | In Development | .cs | ✓ | ✓ | ✓ | - | Classes, interfaces, generics (planned) |
| Go | In Development | .go | ✓ | ✓ | ✓ | - | Methods, type declarations |
| PHP | In Development | .php | ✓ | ✓ | ✓ | - | Classes, functions, namespaces |
| Scala | In Development | .scala, .sc | ✓ | ✓ | ✓ | - | Case classes, objects |
<!-- /SECTION:supported_languages -->
- **🌳 Tree-sitter Parsing**: Uses Tree-sitter for robust, language-agnostic AST parsing
- **📊 Knowledge Graph Storage**: Uses Memgraph to store codebase structure as an interconnected graph
- **🗣️ Natural Language Querying**: Ask questions about your codebase in plain English
- **🤖 AI-Powered Cypher Generation**: Supports both cloud models (Google Gemini), local models (Ollama), and OpenAI models for natural language to Cypher translation
- **🤖 OpenAI Integration**: Leverage OpenAI models to enhance AI functionalities.
- **📝 Code Snippet Retrieval**: Retrieves actual source code snippets for found functions/methods
- **✍️ Advanced File Editing**: Surgical code replacement with AST-based function targeting, visual diff previews, and exact code block modifications
- **⚡️ Shell Command Execution**: Can execute terminal commands for tasks like running tests or using CLI tools.
- **🚀 Interactive Code Optimization**: AI-powered codebase optimization with language-specific best practices and interactive approval workflow
- **📚 Reference-Guided Optimization**: Use your own coding standards and architectural documents to guide optimization suggestions
- **🔗 Dependency Analysis**: Parses `pyproject.toml` to understand external dependencies
- **🎯 Nested Function Support**: Handles complex nested functions and class hierarchies
- **🔄 Language-Agnostic Design**: Unified graph schema across all supported languages

## 🏗️ Architecture

The system consists of two main components:

1. **Multi-language Parser**: Tree-sitter based parsing system that analyzes codebases and ingests data into Memgraph
2. **RAG System** (`codebase_rag/`): Interactive CLI for querying the stored knowledge graph


## 📋 Prerequisites

- Python 3.12+
- Docker & Docker Compose (for Memgraph)
- **cmake** (required for building pymgclient dependency)
- **ripgrep** (`rg`) (required for shell command text searching)
- **For cloud models**: Google Gemini API key
- **For local models**: Ollama installed and running
- `uv` package manager

### Installing cmake and ripgrep

On macOS (Homebrew):
```bash
brew update
brew install cmake ripgrep
```

On Linux (Ubuntu/Debian):
```bash
sudo apt update
sudo apt install -y cmake ripgrep
```

On Linux (CentOS/RHEL/Rocky/Alma):
```bash
sudo yum install -y epel-release || sudo dnf install -y epel-release
sudo yum install -y cmake ripgrep || sudo dnf install -y cmake ripgrep
```

On Windows (Chocolatey):
```bash
choco install cmake ripgrep -y
```

On Windows (Scoop):
```bash
scoop install cmake ripgrep
```

On Windows (winget):
```bash
winget install Kitware.CMake
winget install BurntSushi.ripgrep.MSVC
```

Universal fallback (if ripgrep missing):
```bash
cargo install ripgrep
```

## 🛠️ Installation

```bash
git clone https://github.com/vitali87/code-graph-rag.git

cd code-graph-rag
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

For development (including tests and pre-commit hooks):
```bash
make dev
```

This installs all dependencies and sets up pre-commit hooks automatically.

This installs Tree-sitter grammars for all supported languages (see Multi-Language Support section).

3. **Set up environment variables**:

Create a `.env` file and configure your LLM provider:

```bash
cp .env.example .env
# Edit .env with your configuration
```

**Quick configuration examples:**

```bash
# Ollama (Free, Local)
ORCHESTRATOR_PROVIDER=ollama
ORCHESTRATOR_MODEL=llama3

# Google Gemini (Free Tier)
GEMINI_API_KEY=your-key-here
ORCHESTRATOR_PROVIDER=gemini
ORCHESTRATOR_MODEL=gemini-1.5-flash

# OpenAI
OPENAI_API_KEY=sk-proj-your-key
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-4o-mini
```

📚 **For detailed configuration options**, see:
- [Quick Start Guide](docs/getting-started/quick-start.md) - 30-second setup
- [Complete LLM Configuration Guide](docs/llm/configuration.md) - All providers and advanced features
- [Supported Providers List](docs/llm/supported-providers.md) - 100+ models

4. **Start Memgraph database**:
```bash
docker-compose up -d
```

## 🛠️ Makefile Commands

Use the Makefile for common development tasks:

<!-- SECTION:makefile_commands -->
| Command | Description |
|-------|-----------|
| `make help` | Show this help message |
| `make all` | Install everything for full development environment (deps, grammars, hooks, tests) |
| `make install` | Install project dependencies with full language support |
| `make python` | Install project dependencies for Python only |
| `make dev` | Setup development environment (install deps + pre-commit hooks) |
| `make test` | Run unit tests only (fast, no Docker) |
| `make test-parallel` | Run unit tests in parallel (fast, no Docker) |
| `make test-integration` | Run integration tests (requires Docker) |
| `make test-all` | Run all tests including integration and e2e (requires Docker) |
| `make test-parallel-all` | Run all tests in parallel including integration and e2e (requires Docker) |
| `make clean` | Clean up build artifacts and cache |
| `make build-grammars` | Build grammar submodules |
| `make watch` | Watch repository for changes and update graph in real-time |
| `make readme` | Regenerate README.md from codebase |
| `make lint` | Run ruff check |
| `make format` | Run ruff format |
| `make typecheck` | Run type checking with ty |
| `make check` | Run all checks: lint, typecheck, test |
| `make pre-commit` | Run all pre-commit checks locally (comprehensive test before commit) |
<!-- /SECTION:makefile_commands -->

## 🎯 Usage

### Quick Start

1. **Parse your repository**:
```bash
graph-code start --repo-path /path/to/repo --update-graph --clean
```

2. **Query your codebase**:
```bash
graph-code start --repo-path /path/to/repo
```

3. **Ask questions**:
```
> Show me all authentication functions
> How does the user login flow work?
> What functions call process_payment?
```

📚 **[Complete Usage Guide](docs/usage/basic-usage.md)** - Detailed usage instructions

### Advanced Features

- 🔄 **[Real-Time Updates](docs/advanced/real-time-updates.md)** - Auto-sync graph with code changes
- 📊 **[Graph Export](docs/advanced/graph-export.md)** - Export and analyze graph data
- 🚀 **[Code Optimization](docs/advanced/code-optimization.md)** - AI-powered code improvements

---

## 🔌 MCP Server (Claude Code Integration)

Graph-Code can run as an MCP (Model Context Protocol) server, enabling seamless integration with Claude Code and other MCP clients.

### Quick Setup

```bash
claude mcp add --transport stdio graph-code \
  --env TARGET_REPO_PATH=/absolute/path/to/your/project \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-4 \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag graph-code mcp-server
```

### Available Tools

<!-- SECTION:mcp_tools -->
| Tool | Description |
|----|-----------|
| `list_projects` | List all indexed projects in the knowledge graph database. Returns a list of project names that have been indexed. |
| `delete_project` | Delete a specific project from the knowledge graph database. This removes all nodes associated with the project while preserving other projects. Use list_projects first to see available projects. |
| `wipe_database` | WARNING: Completely wipe the entire database, removing ALL indexed projects. This cannot be undone. Use delete_project for removing individual projects. |
| `index_repository` | Parse and ingest the repository into the Memgraph knowledge graph. This builds a comprehensive graph of functions, classes, dependencies, and relationships. Note: This preserves other projects - only the current project is re-indexed. |
| `query_code_graph` | Query the codebase knowledge graph using natural language. Ask questions like 'What functions call UserService.create_user?' or 'Show me all classes that implement the Repository interface'. |
| `get_code_snippet` | Retrieve source code for a function, class, or method by its qualified name. Returns the source code, file path, line numbers, and docstring. |
| `surgical_replace_code` | Surgically replace an exact code block in a file using diff-match-patch. Only modifies the exact target block, leaving the rest unchanged. |
| `read_file` | Read the contents of a file from the project. Supports pagination for large files. |
| `write_file` | Write content to a file, creating it if it doesn't exist. |
| `list_directory` | List contents of a directory in the project. |
<!-- /SECTION:mcp_tools -->

### Example Usage

```
> Index this repository
> What functions call UserService.create_user?
> Update the login function to add rate limiting
```

For detailed setup, see [Claude Code Setup Guide](docs/getting-started/claude-code-setup.md).

## 📊 Graph Schema

The knowledge graph uses the following node types and relationships:

### Node Types

<!-- SECTION:node_schemas -->
| Label | Properties |
|-----|----------|
| Project | `{name: string}` |
| Package | `{qualified_name: string, name: string, path: string}` |
| Folder | `{path: string, name: string}` |
| File | `{path: string, name: string, extension: string}` |
| Module | `{qualified_name: string, name: string, path: string}` |
| Class | `{qualified_name: string, name: string, decorators: list[string]}` |
| Function | `{qualified_name: string, name: string, decorators: list[string]}` |
| Method | `{qualified_name: string, name: string, decorators: list[string]}` |
| Interface | `{qualified_name: string, name: string}` |
| Enum | `{qualified_name: string, name: string}` |
| Type | `{qualified_name: string, name: string}` |
| Union | `{qualified_name: string, name: string}` |
| ModuleInterface | `{qualified_name: string, name: string, path: string}` |
| ModuleImplementation | `{qualified_name: string, name: string, path: string, implements_module: string}` |
| ExternalPackage | `{name: string, version_spec: string}` |
<!-- /SECTION:node_schemas -->

### Language-Specific Mappings

<!-- SECTION:language_mappings -->
- **C++**: `class_specifier`, `declaration`, `enum_specifier`, `field_declaration`, `function_definition`, `lambda_expression`, `struct_specifier`, `template_declaration`, `union_specifier`
- **Java**: `annotation_type_declaration`, `class_declaration`, `constructor_declaration`, `enum_declaration`, `interface_declaration`, `method_declaration`, `record_declaration`
- **JavaScript**: `arrow_function`, `class`, `class_declaration`, `function_declaration`, `function_expression`, `generator_function_declaration`, `method_definition`
- **Lua**: `function_declaration`, `function_definition`
- **Python**: `class_definition`, `function_definition`
- **Rust**: `closure_expression`, `enum_item`, `function_item`, `function_signature_item`, `impl_item`, `struct_item`, `trait_item`, `type_item`, `union_item`
- **TypeScript**: `abstract_class_declaration`, `arrow_function`, `class`, `class_declaration`, `enum_declaration`, `function_declaration`, `function_expression`, `function_signature`, `generator_function_declaration`, `interface_declaration`, `internal_module`, `method_definition`, `type_alias_declaration`
- **C#**: `anonymous_method_expression`, `class_declaration`, `constructor_declaration`, `destructor_declaration`, `enum_declaration`, `function_pointer_type`, `interface_declaration`, `lambda_expression`, `local_function_statement`, `method_declaration`, `struct_declaration`
- **Go**: `function_declaration`, `method_declaration`, `type_declaration`
- **PHP**: `anonymous_function`, `arrow_function`, `class_declaration`, `enum_declaration`, `function_definition`, `function_static_declaration`, `interface_declaration`, `trait_declaration`
- **Scala**: `class_definition`, `function_declaration`, `function_definition`, `object_definition`, `trait_definition`
<!-- /SECTION:language_mappings -->

### Relationships

<!-- SECTION:relationship_schemas -->
| Source | Relationship | Target |
|------|------------|------|
| Project, Package, Folder | CONTAINS_PACKAGE | Package |
| Project, Package, Folder | CONTAINS_FOLDER | Folder |
| Project, Package, Folder | CONTAINS_FILE | File |
| Project, Package, Folder | CONTAINS_MODULE | Module |
| Module | DEFINES | Class, Function |
| Class | DEFINES_METHOD | Method |
| Module | IMPORTS | Module |
| Module | EXPORTS | Class, Function |
| Module | EXPORTS_MODULE | ModuleInterface |
| Module | IMPLEMENTS_MODULE | ModuleImplementation |
| Class | INHERITS | Class |
| Class | IMPLEMENTS | Interface |
| Method | OVERRIDES | Method |
| ModuleImplementation | IMPLEMENTS | ModuleInterface |
| Project | DEPENDS_ON_EXTERNAL | ExternalPackage |
| Function, Method | CALLS | Function, Method |
<!-- /SECTION:relationship_schemas -->

## 🔧 Configuration

Configuration is managed through environment variables in `.env` file:

### Provider-Specific Settings

#### Orchestrator Model Configuration
- `ORCHESTRATOR_PROVIDER`: Provider name (`google`, `openai`, `ollama`)
- `ORCHESTRATOR_MODEL`: Model ID (e.g., `gemini-2.5-pro`, `gpt-4o`, `llama3.2`)
- `ORCHESTRATOR_API_KEY`: API key for the provider (if required)
- `ORCHESTRATOR_ENDPOINT`: Custom endpoint URL (if required)
- `ORCHESTRATOR_PROJECT_ID`: Google Cloud project ID (for Vertex AI)
- `ORCHESTRATOR_REGION`: Google Cloud region (default: `us-central1`)
- `ORCHESTRATOR_PROVIDER_TYPE`: Google provider type (`gla` or `vertex`)
- `ORCHESTRATOR_THINKING_BUDGET`: Thinking budget for reasoning models
- `ORCHESTRATOR_SERVICE_ACCOUNT_FILE`: Path to service account file (for Vertex AI)

#### Cypher Model Configuration
- `CYPHER_PROVIDER`: Provider name (`google`, `openai`, `ollama`)
- `CYPHER_MODEL`: Model ID (e.g., `gemini-2.5-flash`, `gpt-4o-mini`, `codellama`)
- `CYPHER_API_KEY`: API key for the provider (if required)
- `CYPHER_ENDPOINT`: Custom endpoint URL (if required)
- `CYPHER_PROJECT_ID`: Google Cloud project ID (for Vertex AI)
- `CYPHER_REGION`: Google Cloud region (default: `us-central1`)
- `CYPHER_PROVIDER_TYPE`: Google provider type (`gla` or `vertex`)
- `CYPHER_THINKING_BUDGET`: Thinking budget for reasoning models
- `CYPHER_SERVICE_ACCOUNT_FILE`: Path to service account file (for Vertex AI)

### System Settings
- `MEMGRAPH_HOST`: Memgraph hostname (default: `localhost`)
- `MEMGRAPH_PORT`: Memgraph port (default: `7687`)
- `MEMGRAPH_HTTP_PORT`: Memgraph HTTP port (default: `7444`)
- `LAB_PORT`: Memgraph Lab port (default: `3000`)
- `MEMGRAPH_BATCH_SIZE`: Batch size for Memgraph operations (default: `1000`)
- `TARGET_REPO_PATH`: Default repository path (default: `.`)
- `LOCAL_MODEL_ENDPOINT`: Fallback endpoint for Ollama (default: `http://localhost:11434/v1`)

### Custom Ignore Patterns

You can specify additional directories to exclude by creating a `.cgrignore` file in your repository root:

```
# Comments start with #
vendor
.custom_cache
my_build_output
```

- One directory name per line
- Lines starting with `#` are comments
- Blank lines are ignored
- Patterns are exact directory name matches (not globs)
- Patterns from `.cgrignore` are merged with `--exclude` flags and auto-detected directories

### Key Dependencies

<!-- SECTION:dependencies -->
- **loguru**: Python logging made (stupidly) simple
- **mcp**: Model Context Protocol SDK
- **pydantic-ai**: Agent Framework / shim to use Pydantic with LLMs
- **pydantic-settings**: Settings management using Pydantic
- **litellm**: Library to easily interface with LLM API providers
- **pymgclient**: Memgraph database adapter for Python language
- **python-dotenv**: Read key-value pairs from a .env file and set them as environment variables
- **toml**: Python Library for Tom's Obvious, Minimal Language
- **tree-sitter-python**: Python grammar for tree-sitter
- **tree-sitter**: Python bindings to the Tree-sitter parsing library
- **watchdog**: Filesystem events monitoring
- **typer**: Typer, build great CLIs. Easy to code. Based on Python type hints.
- **rich**: Render rich text, tables, progress bars, syntax highlighting, markdown and more to the terminal
- **prompt-toolkit**: Library for building powerful interactive command lines in Python
- **diff-match-patch**: Repackaging of Google's Diff Match and Patch libraries.
- **click**: Composable command line interface toolkit
- **protobuf**
- **defusedxml**: XML bomb protection for Python stdlib modules
- **huggingface-hub**: Client library to download and publish models, datasets and other repos on the huggingface.co hub
<!-- /SECTION:dependencies -->

## 🤖 Agentic Workflow & Tools

The agent is designed with a deliberate workflow to ensure it acts with context and precision, especially when modifying the file system.

### Core Tools

The agent has access to a suite of tools to understand and interact with the codebase:

<!-- SECTION:agentic_tools -->
| Tool | Description |
|----|-----------|
| `query_graph` | Query the codebase knowledge graph using natural language questions. Ask in plain English about classes, functions, methods, dependencies, or code structure. Examples: 'Find all functions that call each other', 'What classes are in the user module', 'Show me functions with the longest call chains'. |
| `read_file` | Reads the content of text-based files. For documents like PDFs or images, use the 'analyze_document' tool instead. |
| `create_file` | Creates a new file with content. IMPORTANT: Check file existence first! Overwrites completely WITHOUT showing diff. Use only for new files, not existing file modifications. |
| `replace_code` | Surgically replaces specific code blocks in files. Requires exact target code and replacement. Only modifies the specified block, leaving rest of file unchanged. True surgical patching. |
| `list_directory` | Lists the contents of a directory to explore the codebase. |
| `analyze_document` | Analyzes documents (PDFs, images) to answer questions about their content. |
| `execute_shell` | Executes shell commands from allowlist. Read-only commands run without approval; write operations require user confirmation. |
| `semantic_search` | Performs a semantic search for functions based on a natural language query describing their purpose, returning a list of potential matches with similarity scores. |
| `get_function_source` | Retrieves the source code for a specific function or method using its internal node ID, typically obtained from a semantic search result. |
| `get_code_snippet` | Retrieves the source code for a specific function, class, or method using its full qualified name. |
<!-- /SECTION:agentic_tools -->

### Intelligent and Safe File Editing

The agent uses AST-based function targeting with Tree-sitter for precise code modifications. Features include:
- **Visual diff preview** before changes
- **Surgical patching** that only modifies target code blocks
- **Multi-language support** across all supported languages
- **Security sandbox** preventing edits outside project directory
- **Smart function matching** with qualified names and line numbers



## 🌍 Multi-Language Support

### Adding New Languages

Graph-Code makes it easy to add support for any language that has a Tree-sitter grammar. The system automatically handles grammar compilation and integration.

> **⚠️ Recommendation**: While you can add languages yourself, we recommend waiting for official full support to ensure optimal parsing quality, comprehensive feature coverage, and robust integration. The languages marked as "In Development" above will receive dedicated optimization and testing.

> **💡 Request Support**: If you want a specific language to be officially supported, please [submit an issue](https://github.com/vitali87/code-graph-rag/issues) with your language request.

#### Quick Start: Add a Language

Use the built-in language management tool to add any Tree-sitter supported language:

```bash
# Add a language using the standard tree-sitter repository
graph-code language add-grammar <language-name>

# Examples:
graph-code language add-grammar c-sharp
graph-code language add-grammar php
graph-code language add-grammar ruby
graph-code language add-grammar kotlin
```

#### Custom Grammar Repositories

For languages hosted outside the standard tree-sitter organization:

```bash
# Add a language from a custom repository
graph-code language add-grammar --grammar-url https://github.com/custom/tree-sitter-mylang
```

#### What Happens Automatically

When you add a language, the tool automatically:

1. **Downloads the Grammar**: Clones the tree-sitter grammar repository as a git submodule
2. **Detects Configuration**: Auto-extracts language metadata from `tree-sitter.json`
3. **Analyzes Node Types**: Automatically identifies AST node types for:
   - Functions/methods (`method_declaration`, `function_definition`, etc.)
   - Classes/structs (`class_declaration`, `struct_declaration`, etc.)
   - Modules/files (`compilation_unit`, `source_file`, etc.)
   - Function calls (`call_expression`, `method_invocation`, etc.)
4. **Compiles Bindings**: Builds Python bindings from the grammar source
5. **Updates Configuration**: Adds the language to `codebase_rag/language_config.py`
6. **Enables Parsing**: Makes the language immediately available for codebase analysis

#### Example: Adding C# Support

```bash
$ graph-code language add-grammar c-sharp
🔍 Using default tree-sitter URL: https://github.com/tree-sitter/tree-sitter-c-sharp
🔄 Adding submodule from https://github.com/tree-sitter/tree-sitter-c-sharp...
✅ Successfully added submodule at grammars/tree-sitter-c-sharp
Auto-detected language: c-sharp
Auto-detected file extensions: ['cs']
Auto-detected node types:
Functions: ['destructor_declaration', 'method_declaration', 'constructor_declaration']
Classes: ['struct_declaration', 'enum_declaration', 'interface_declaration', 'class_declaration']
Modules: ['compilation_unit', 'file_scoped_namespace_declaration', 'namespace_declaration']
Calls: ['invocation_expression']

✅ Language 'c-sharp' has been added to the configuration!
📝 Updated codebase_rag/language_config.py
```

#### Managing Languages

```bash
# List all configured languages
graph-code language list-languages

# Remove a language (this also removes the git submodule unless --keep-submodule is specified)
graph-code language remove-language <language-name>
```

#### Language Configuration

The system uses a configuration-driven approach for language support. Each language is defined in `codebase_rag/language_config.py` with the following structure:

```python
"language-name": LanguageConfig(
    name="language-name",
    file_extensions=[".ext1", ".ext2"],
    function_node_types=["function_declaration", "method_declaration"],
    class_node_types=["class_declaration", "struct_declaration"],
    module_node_types=["compilation_unit", "source_file"],
    call_node_types=["call_expression", "method_invocation"],
),
```

#### Troubleshooting

**Grammar not found**: If the automatic URL doesn't work, use a custom URL:
```bash
graph-code language add-grammar --grammar-url https://github.com/custom/tree-sitter-mylang
```

**Version incompatibility**: If you get "Incompatible Language version" errors, update your tree-sitter package:
```bash
uv add tree-sitter@latest
```

**Missing node types**: The tool automatically detects common node patterns, but you can manually adjust the configuration in `language_config.py` if needed.

## 📦 Building a binary

You can build a binary of the application using the `build_binary.py` script. This script uses PyInstaller to package the application and its dependencies into a single executable.

```bash
python build_binary.py
```
The resulting binary will be located in the `dist` directory.

## 🐛 Debugging

1. **Check Memgraph connection**:
   - Ensure Docker containers are running: `docker-compose ps`
   - Verify Memgraph is accessible on port 7687

2. **View database in Memgraph Lab**:
   - Open http://localhost:3000
   - Connect to memgraph:7687

3. **For local models**:
   - Verify Ollama is running: `ollama list`
   - Check if models are downloaded: `ollama pull llama3`
   - Test Ollama API: `curl http://localhost:11434/v1/models`
   - Check Ollama logs: `ollama logs`

## 🤝 Contributing

Please see [CONTRIBUTING.md](CONTRIBUTING.md) for detailed contribution guidelines.

Good first PRs are from TODO issues.

## 🙋‍♂️ Support

For issues or questions:
1. Check the logs for error details
2. Verify Memgraph connection
3. Ensure all environment variables are set
4. Review the graph schema matches your expectations

## 💼 Enterprise Services

Graph-Code is open source and free to use. For organizations that need additional support, we offer:

- **Technical Support Contracts** — Custom SLAs, priority issue resolution, and dedicated assistance
- **Integration Consulting** — Help deploying Graph-Code in your infrastructure and integrating with your toolchain
- **Custom Development** — Tailored features, new language support, and workflow optimization for your specific codebase
- **Training & Onboarding** — Get your team up to speed with hands-on training sessions

**[Learn more at code-graph-rag.com →](https://code-graph-rag.com)**

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=vitali87/code-graph-rag&type=Date)](https://www.star-history.com/#vitali87/code-graph-rag&Date)
