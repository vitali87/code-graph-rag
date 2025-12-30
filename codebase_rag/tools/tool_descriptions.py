from __future__ import annotations

from enum import StrEnum

from codebase_rag.constants import MCPToolName


class AgenticToolName(StrEnum):
    QUERY_GRAPH = "query_graph"
    READ_FILE = "read_file"
    CREATE_FILE = "create_file"
    REPLACE_CODE = "replace_code"
    LIST_DIRECTORY = "list_directory"
    ANALYZE_DOCUMENT = "analyze_document"
    EXECUTE_SHELL = "execute_shell"
    SEMANTIC_SEARCH = "semantic_search"
    GET_FUNCTION_SOURCE = "get_function_source"
    GET_CODE_SNIPPET = "get_code_snippet"


ANALYZE_DOCUMENT = (
    "Analyzes documents (PDFs, images) to answer questions about their content."
)

CODEBASE_QUERY = (
    "Query the codebase knowledge graph using natural language questions. "
    "Ask in plain English about classes, functions, methods, dependencies, or code structure. "
    "Examples: 'Find all functions that call each other', "
    "'What classes are in the user module', "
    "'Show me functions with the longest call chains'."
)

DIRECTORY_LISTER = "Lists the contents of a directory to explore the codebase."

FILE_WRITER = (
    "Creates a new file with content. IMPORTANT: Check file existence first! "
    "Overwrites completely WITHOUT showing diff. "
    "Use only for new files, not existing file modifications."
)

SHELL_COMMAND = (
    "Executes shell commands from allowlist. "
    "Read-only commands run without approval; write operations require user confirmation."
)

CODE_RETRIEVAL = (
    "Retrieves the source code for a specific function, class, or method "
    "using its full qualified name."
)

FILE_READER = (
    "Reads the content of text-based files. "
    "For documents like PDFs or images, use the 'analyze_document' tool instead."
)

FILE_EDITOR = (
    "Surgically replaces specific code blocks in files. "
    "Requires exact target code and replacement. "
    "Only modifies the specified block, leaving rest of file unchanged. "
    "True surgical patching."
)

# (H) MCP tool descriptions
MCP_INDEX_REPOSITORY = (
    "Parse and ingest the repository into the Memgraph knowledge graph. "
    "This builds a comprehensive graph of functions, classes, dependencies, and relationships."
)

MCP_QUERY_CODE_GRAPH = (
    "Query the codebase knowledge graph using natural language. "
    "Ask questions like 'What functions call UserService.create_user?' or "
    "'Show me all classes that implement the Repository interface'."
)

MCP_GET_CODE_SNIPPET = (
    "Retrieve source code for a function, class, or method by its qualified name. "
    "Returns the source code, file path, line numbers, and docstring."
)

MCP_SURGICAL_REPLACE_CODE = (
    "Surgically replace an exact code block in a file using diff-match-patch. "
    "Only modifies the exact target block, leaving the rest unchanged."
)

MCP_READ_FILE = (
    "Read the contents of a file from the project. Supports pagination for large files."
)

MCP_WRITE_FILE = "Write content to a file, creating it if it doesn't exist."

MCP_LIST_DIRECTORY = "List contents of a directory in the project."

MCP_PARAM_NATURAL_LANGUAGE_QUERY = "Your question in plain English about the codebase"
MCP_PARAM_QUALIFIED_NAME = (
    "Fully qualified name (e.g., 'app.services.UserService.create_user')"
)
MCP_PARAM_FILE_PATH = "Relative path to the file from project root"
MCP_PARAM_TARGET_CODE = "Exact code block to replace"
MCP_PARAM_REPLACEMENT_CODE = "New code to insert"
MCP_PARAM_OFFSET = "Line number to start reading from (0-based, optional)"
MCP_PARAM_LIMIT = "Maximum number of lines to read (optional)"
MCP_PARAM_CONTENT = "Content to write to the file"
MCP_PARAM_DIRECTORY_PATH = "Relative path to directory from project root (default: '.')"


MCP_TOOLS: dict[MCPToolName, str] = {
    MCPToolName.INDEX_REPOSITORY: MCP_INDEX_REPOSITORY,
    MCPToolName.QUERY_CODE_GRAPH: MCP_QUERY_CODE_GRAPH,
    MCPToolName.GET_CODE_SNIPPET: MCP_GET_CODE_SNIPPET,
    MCPToolName.SURGICAL_REPLACE_CODE: MCP_SURGICAL_REPLACE_CODE,
    MCPToolName.READ_FILE: MCP_READ_FILE,
    MCPToolName.WRITE_FILE: MCP_WRITE_FILE,
    MCPToolName.LIST_DIRECTORY: MCP_LIST_DIRECTORY,
}

AGENTIC_TOOLS: dict[AgenticToolName, str] = {
    AgenticToolName.QUERY_GRAPH: CODEBASE_QUERY,
    AgenticToolName.READ_FILE: FILE_READER,
    AgenticToolName.CREATE_FILE: FILE_WRITER,
    AgenticToolName.REPLACE_CODE: FILE_EDITOR,
    AgenticToolName.LIST_DIRECTORY: DIRECTORY_LISTER,
    AgenticToolName.ANALYZE_DOCUMENT: ANALYZE_DOCUMENT,
    AgenticToolName.EXECUTE_SHELL: SHELL_COMMAND,
    AgenticToolName.SEMANTIC_SEARCH: CODE_RETRIEVAL,
    AgenticToolName.GET_FUNCTION_SOURCE: CODE_RETRIEVAL,
    AgenticToolName.GET_CODE_SNIPPET: CODE_RETRIEVAL,
}
