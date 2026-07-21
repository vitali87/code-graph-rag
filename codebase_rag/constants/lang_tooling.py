# Add-language grammar tooling messages, prompts, and file names.

LANG_GRAMMARS_DIR = "grammars"
LANG_CONFIG_FILE = "codebase_rag/language_spec.py"
LANG_TREE_SITTER_JSON = "tree-sitter.json"
LANG_NODE_TYPES_JSON = "node-types.json"
LANG_SRC_DIR = "src"
LANG_GIT_MODULES_PATH = ".git/modules/{path}"
LANG_DEFAULT_GRAMMAR_URL = "https://github.com/tree-sitter/tree-sitter-{name}"
LANG_TREE_SITTER_URL_MARKER = "github.com/tree-sitter/tree-sitter"

LANG_DEFAULT_FUNCTION_NODES = ("function_definition", "method_definition")
LANG_DEFAULT_CLASS_NODES = ("class_declaration",)
LANG_DEFAULT_MODULE_NODES = ("compilation_unit",)
LANG_DEFAULT_CALL_NODES = ("invocation_expression",)
LANG_FALLBACK_METHOD_NODE = "method_declaration"

LANG_FUNCTION_KEYWORDS = frozenset(
    {
        "function",
        "method",
        "constructor",
        "destructor",
        "lambda",
        "arrow_function",
        "anonymous_function",
        "closure",
    }
)
LANG_CLASS_KEYWORDS = frozenset(
    {
        "class",
        "interface",
        "struct",
        "enum",
        "trait",
        "object",
        "type",
        "impl",
        "union",
    }
)
LANG_CALL_KEYWORDS = frozenset({"call", "invoke", "invocation"})
LANG_MODULE_KEYWORDS = frozenset(
    {"program", "source_file", "compilation_unit", "module", "chunk"}
)
LANG_EXCLUSION_KEYWORDS = frozenset({"access", "call"})

LANG_MSG_USING_DEFAULT_URL = "Using default tree-sitter URL: {url}"
LANG_MSG_CUSTOM_URL_WARNING = (
    "WARNING: You are adding a grammar from a custom URL. "
    "This may execute code from the repository. Only proceed if you trust the source."
)
LANG_MSG_ADDING_SUBMODULE = "Adding submodule from {url}..."
LANG_MSG_SUBMODULE_SUCCESS = "Successfully added submodule at {path}"
LANG_MSG_SUBMODULE_EXISTS = (
    "Submodule already exists at {path}. Forcing re-installation..."
)
LANG_MSG_REMOVING_ENTRY = "   -> Removing existing submodule entry..."
LANG_MSG_READDING_SUBMODULE = "   -> Re-adding submodule..."
LANG_MSG_REINSTALL_SUCCESS = "Successfully re-installed submodule at {path}"
LANG_MSG_AUTO_DETECTED_LANG = "Auto-detected language: {name}"
LANG_MSG_USING_LANG_NAME = "Using language name: {name}"
LANG_MSG_AUTO_DETECTED_EXT = "Auto-detected file extensions: {extensions}"
LANG_MSG_FOUND_NODE_TYPES = "Found {count} total node types in grammar"
LANG_MSG_SEMANTIC_CATEGORIES = "Tree-sitter semantic categories:"
LANG_MSG_CATEGORY_FORMAT = "  {category}: {subtypes} ({count} total)"
LANG_MSG_MAPPED_CATEGORIES = "\nMapped to our categories:"
LANG_MSG_FUNCTIONS = "Functions: {nodes}"
LANG_MSG_CLASSES = "Classes: {nodes}"
LANG_MSG_MODULES = "Modules: {nodes}"
LANG_MSG_CALLS = "Calls: {nodes}"
LANG_MSG_LANG_ADDED = "\nLanguage '{name}' has been added to the configuration!"
LANG_MSG_UPDATED_CONFIG = "Updated {path}"
LANG_MSG_REVIEW_PROMPT = "Please review the detected node types:"
LANG_MSG_REVIEW_HINT = "   The auto-detection is good but may need manual adjustments."
LANG_MSG_EDIT_HINT = "   Edit the configuration in: {path}"
LANG_MSG_COMMON_ISSUES = "Look for these common issues:"
LANG_MSG_ISSUE_MISCLASSIFIED = (
    "   - Remove misclassified types (e.g., table_constructor in functions)"
)
LANG_MSG_ISSUE_MISSING = "   - Add missing types that should be included"
LANG_MSG_ISSUE_CLASS_TYPES = (
    "   - Verify class_node_types includes all relevant class-like constructs"
)
LANG_MSG_ISSUE_CALL_TYPES = (
    "   - Check call_node_types covers all function call patterns"
)
LANG_MSG_LIST_HINT = (
    "You can run 'cgr language list-languages' to see the current config."
)
LANG_MSG_LANG_NOT_FOUND = "Language '{name}' not found."
LANG_MSG_AVAILABLE_LANGS = "Available languages: {langs}"
LANG_MSG_REMOVED_FROM_CONFIG = "Removed language '{name}' from configuration file."
LANG_MSG_REMOVING_SUBMODULE = "Removing git submodule '{path}'..."
LANG_MSG_CLEANED_MODULES = "Cleaned up git modules directory: {path}"
LANG_MSG_SUBMODULE_REMOVED = "Successfully removed submodule '{path}'"
LANG_MSG_NO_SUBMODULE = "No submodule found at '{path}'"
LANG_MSG_KEEPING_SUBMODULE = "Keeping submodule (--keep-submodule flag used)"
LANG_MSG_LANG_REMOVED = "Language '{name}' has been removed successfully!"
LANG_MSG_NO_MODULES_DIR = "No grammars modules directory found."
LANG_MSG_NO_GITMODULES = "No .gitmodules file found."
LANG_MSG_NO_ORPHANS = "No orphaned modules found!"
LANG_MSG_FOUND_ORPHANS = "Found {count} orphaned module(s): {modules}"
LANG_MSG_REMOVED_ORPHAN = "Removed orphaned module: {module}"
LANG_MSG_CLEANUP_COMPLETE = "Cleanup complete!"
LANG_MSG_CLEANUP_CANCELLED = "Cleanup cancelled."

LANG_ERR_MISSING_ARGS = "Error: Either language_name or --grammar-url must be provided"
LANG_ERR_REINSTALL_FAILED = "Failed to reinstall submodule: {error}"
LANG_ERR_MANUAL_REMOVE_HINT = "You may need to remove it manually and try again:"
LANG_ERR_REPO_NOT_FOUND = "Error: Repository not found at {url}"
LANG_ERR_CUSTOM_URL_HINT = "Try using a custom URL with: --grammar-url <your-repo-url>"
LANG_ERR_GIT = "Git error: {error}"
LANG_ERR_NODE_TYPES_WARNING = (
    "Warning: node-types.json not found in any expected location for {name}"
)
LANG_ERR_TREE_SITTER_JSON_WARNING = "Warning: tree-sitter.json not found in {path}"
LANG_ERR_NO_GRAMMARS_WARNING = "Warning: No grammars found in tree-sitter.json"
LANG_ERR_PARSE_NODE_TYPES = "Error parsing node-types.json: {error}"
LANG_ERR_UPDATE_CONFIG = "Error updating config file: {error}"
LANG_ERR_CONFIG_NOT_FOUND = "Could not find LANGUAGE_SPECS dictionary end"
LANG_ERR_REMOVE_CONFIG = "Failed to update config file: {error}"
LANG_ERR_REMOVE_SUBMODULE = "Failed to remove submodule: {error}"

LANG_PROMPT_LANGUAGE_NAME = "Language name (e.g., 'c-sharp', 'python')"
LANG_PROMPT_COMMON_NAME = "What is the common name for this language?"
LANG_PROMPT_EXTENSIONS = (
    "What file extensions should be associated with this language? (comma-separated)"
)
LANG_PROMPT_FUNCTIONS = "Select nodes representing FUNCTIONS (comma-separated)"
LANG_PROMPT_CLASSES = "Select nodes representing CLASSES (comma-separated)"
LANG_PROMPT_MODULES = "Select nodes representing MODULES (comma-separated)"
LANG_PROMPT_CALLS = "Select nodes representing FUNCTION CALLS (comma-separated)"
LANG_PROMPT_CONTINUE = "Do you want to continue?"
LANG_PROMPT_REMOVE_ORPHANS = "Do you want to remove these orphaned modules?"

LANG_FALLBACK_MANUAL_ADD = (
    "FALLBACK: Please manually add the following entry to "
    "'LANGUAGE_SPECS' in 'codebase_rag/language_spec.py':"
)

LANG_TABLE_TITLE = "Configured Languages"
LANG_TABLE_COL_LANGUAGE = "Language"
LANG_TABLE_COL_EXTENSIONS = "Extensions"
LANG_TABLE_COL_FUNCTION_TYPES = "Function Types"
LANG_TABLE_COL_CLASS_TYPES = "Class Types"
LANG_TABLE_COL_CALL_TYPES = "Call Types"
LANG_TABLE_PLACEHOLDER = "—"

LANG_MSG_AVAILABLE_NODES = "Available nodes for mapping:"
LANG_ELLIPSIS = "..."
LANG_GIT_SUFFIX = ".git"
LANG_GITMODULES_FILE = ".gitmodules"
LANG_CALL_KEYWORD_EXCLUDE = "call"

LANG_GITMODULES_REGEX = r"path = (grammars/tree-sitter-[^\\n]+)"
