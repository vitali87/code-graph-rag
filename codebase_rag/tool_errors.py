from __future__ import annotations

# Generic error wrapper
ERROR_WRAPPER = "Error: {message}"

# File operation errors
FILE_NOT_FOUND = "File not found."
FILE_NOT_FOUND_OR_DIR = "File not found or is a directory: {path}"
BINARY_FILE = "File '{path}' is a binary file. Ask the user to attach it inline if they want it analyzed."
UNICODE_DECODE = (
    "File '{path}' could not be read as text. It may be a binary file. "
    "If it is a document (e.g., PDF), ask the user to attach it inline."
)

# Directory errors
DIRECTORY_INVALID = "Error: '{path}' is not a valid directory."
DIRECTORY_EMPTY = "Error: The directory '{path}' is empty."
DIRECTORY_LIST_FAILED = "Error: Could not list contents of '{path}'."
DIRECTORY_PATH_OUTSIDE_ROOT = (
    "Error: '{path}' is outside the project root ({root}). "
    "Use a relative path from the project root, or the full absolute path within it."
)

# Shell command errors
COMMAND_NOT_ALLOWED = "Command '{cmd}' is not in the allowlist.{suggestion} Available commands: {available}"
COMMAND_EMPTY = "Empty command provided."
COMMAND_DANGEROUS = "Rejected dangerous command: {cmd}"
COMMAND_DANGEROUS_BLOCKED = "Blocked dangerous command '{cmd}': {reason}"
COMMAND_DANGEROUS_PATTERN = "Command matches dangerous pattern: {reason}"
COMMAND_TIMEOUT = "Command '{cmd}' timed out after {timeout} seconds."
COMMAND_SUBSHELL_NOT_ALLOWED = "Subshell execution not allowed: {pattern}"
COMMAND_INVALID_SYNTAX = "Invalid command syntax: {segment}"

# Code retrieval errors
CODE_ENTITY_NOT_FOUND = "Entity not found in graph."
CODE_MISSING_LOCATION = "Graph entry is missing location data."
CODE_SOURCE_FILE_MISSING = (
    "Source file not found on disk for '{path}' "
    "(checked the stored absolute path and the current project root)."
)

# File writer errors
FILE_WRITER_SECURITY = (
    "Security risk: Attempted to create file outside of project root: {path}"
)
FILE_WRITER_CREATE = "Error creating file {path}: {error}"

# Export errors
EXPORT_FAILED = "Failed to export graph: {error}"

# MCP tool errors
MCP_TOOL_RETURNED_NONE = "Tool returned None"
MCP_INVALID_RESPONSE = "Code snippet tool returned an invalid response"
MCP_PATH_NOT_EXISTS = "Target repository path does not exist: {path}"
MCP_PATH_NOT_DIR = "Target repository path is not a directory: {path}"
MCP_PROJECT_NOT_FOUND = (
    "Project '{project_name}' not found. Available projects: {projects}"
)

# CLI validation errors
INVALID_POSITIVE_INT = "{value!r} is not a valid positive integer"
INVALID_NON_NEGATIVE_FLOAT = "Value must be non-negative, got {value}"
