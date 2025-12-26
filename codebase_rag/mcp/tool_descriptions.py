INDEX_REPOSITORY = (
    "Parse and ingest the repository into the Memgraph knowledge graph. "
    "This builds a comprehensive graph of functions, classes, dependencies, and relationships."
)

QUERY_CODE_GRAPH = (
    "Query the codebase knowledge graph using natural language. "
    "Ask questions like 'What functions call UserService.create_user?' or "
    "'Show me all classes that implement the Repository interface'."
)

GET_CODE_SNIPPET = (
    "Retrieve source code for a function, class, or method by its qualified name. "
    "Returns the source code, file path, line numbers, and docstring."
)

SURGICAL_REPLACE_CODE = (
    "Surgically replace an exact code block in a file using diff-match-patch. "
    "Only modifies the exact target block, leaving the rest unchanged."
)

READ_FILE = (
    "Read the contents of a file from the project. Supports pagination for large files."
)

WRITE_FILE = "Write content to a file, creating it if it doesn't exist."

LIST_DIRECTORY = "List contents of a directory in the project."

PARAM_NATURAL_LANGUAGE_QUERY = "Your question in plain English about the codebase"
PARAM_QUALIFIED_NAME = (
    "Fully qualified name (e.g., 'app.services.UserService.create_user')"
)
PARAM_FILE_PATH = "Relative path to the file from project root"
PARAM_TARGET_CODE = "Exact code block to replace"
PARAM_REPLACEMENT_CODE = "New code to insert"
PARAM_OFFSET = "Line number to start reading from (0-based, optional)"
PARAM_LIMIT = "Maximum number of lines to read (optional)"
PARAM_CONTENT = "Content to write to the file"
PARAM_DIRECTORY_PATH = "Relative path to directory from project root (default: '.')"
