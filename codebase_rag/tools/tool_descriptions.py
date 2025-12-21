from __future__ import annotations

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
