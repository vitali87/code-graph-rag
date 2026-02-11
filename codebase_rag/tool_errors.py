from __future__ import annotations

# (H) Generic error wrapper
ERROR_WRAPPER = "Error: {message}"

# (H) File operation errors
FILE_NOT_FOUND = "File not found."
FILE_NOT_FOUND_OR_DIR = "File not found or is a directory: {path}"
BINARY_FILE = "File '{path}' is a binary file. Use the 'analyze_document' tool for this file type."
UNICODE_DECODE = (
    "File '{path}' could not be read as text. It may be a binary file. "
    "If it is a document (e.g., PDF), use the 'analyze_document' tool."
)

# (H) Document analyzer errors
DOCUMENT_UNSUPPORTED = (
    "Error: Document analysis is not supported for the current LLM provider."
)
DOC_FILE_NOT_FOUND = "Error: File not found at '{path}'."
DOC_SECURITY_RISK = "Error: Security risk: file path {path} is outside the project root"
DOC_ACCESS_OUTSIDE_ROOT = (
    "Error: Security risk: Attempted to access file outside of project root: {path}"
)
DOC_API_VALIDATION = "Error: API validation failed: {error}"
DOC_API_ERROR = "Error: API error: {error}"
DOC_IMAGE_PROCESS = (
    "Error: Unable to process the image file. "
    "The image may be corrupted or in an unsupported format."
)
DOC_ANALYSIS_FAILED = "Error: An error occurred during analysis: {error}"
DOC_DURING_ANALYSIS = "Error: Document analysis failed: {error}"

# (H) Directory errors
DIRECTORY_INVALID = "Error: '{path}' is not a valid directory."
DIRECTORY_EMPTY = "Error: The directory '{path}' is empty."
DIRECTORY_LIST_FAILED = "Error: Could not list contents of '{path}'."

# (H) Shell command errors
COMMAND_NOT_ALLOWED = "Command '{cmd}' is not in the allowlist.{suggestion} Available commands: {available}"
COMMAND_EMPTY = "Empty command provided."
COMMAND_DANGEROUS = "Rejected dangerous command: {cmd}"
COMMAND_DANGEROUS_BLOCKED = "Blocked dangerous command '{cmd}': {reason}"
COMMAND_DANGEROUS_PATTERN = "Command matches dangerous pattern: {reason}"
COMMAND_TIMEOUT = "Command '{cmd}' timed out after {timeout} seconds."
COMMAND_SUBSHELL_NOT_ALLOWED = "Subshell execution not allowed: {pattern}"
COMMAND_INVALID_SYNTAX = "Invalid command syntax: {segment}"

# (H) Code retrieval errors
CODE_ENTITY_NOT_FOUND = "Entity not found in graph."
CODE_MISSING_LOCATION = "Graph entry is missing location data."

# (H) Tool operation errors
WRITE_QUERY_MODE_BLOCKED = "Write operations are not allowed in query mode"

# (H) File writer errors
FILE_WRITER_SECURITY = (
    "Security risk: Attempted to create file outside of project root: {path}"
)
FILE_WRITER_CREATE = "Error creating file {path}: {error}"

# (H) Export errors
EXPORT_FAILED = "Failed to export graph: {error}"

# (H) MCP tool errors
MCP_TOOL_RETURNED_NONE = "Tool returned None"
MCP_INVALID_RESPONSE = "Code snippet tool returned an invalid response"
MCP_PATH_NOT_EXISTS = "Target repository path does not exist: {path}"
MCP_PATH_NOT_DIR = "Target repository path is not a directory: {path}"
MCP_PROJECT_NOT_FOUND = (
    "Project '{project_name}' not found. Available projects: {projects}"
)

# (H) CLI validation errors
INVALID_POSITIVE_INT = "{value!r} is not a valid positive integer"
