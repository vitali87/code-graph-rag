from __future__ import annotations

# (H) Provider logs
PROVIDER_REGISTERED = "Registered provider: {name}"

# (H) Graph loading logs
LOADING_GRAPH = "Loading graph from {path}"
LOADED_GRAPH = "Loaded {nodes} nodes and {relationships} relationships with indexes"
ENSURING_PROJECT = "Ensuring Project: {name}"

# (H) Pass logs
PASS_1_STRUCTURE = "--- Pass 1: Identifying Packages and Folders ---"
PASS_2_FILES = (
    "\n--- Pass 2: Processing Files, Caching ASTs, and Collecting Definitions ---"
)
PASS_3_CALLS = "--- Pass 3: Processing Function Calls from AST Cache ---"
PASS_4_EMBEDDINGS = "--- Pass 4: Generating semantic embeddings ---"

# (H) Analysis logs
FOUND_FUNCTIONS = "\n--- Found {count} functions/methods in codebase ---"
ANALYSIS_COMPLETE = "\n--- Analysis complete. Flushing all data to database... ---"
REMOVING_STATE = "Removing in-memory state for: {path}"
REMOVED_FROM_CACHE = "  - Removed from ast_cache"
REMOVING_QNS = "  - Removing {count} QNs from function_registry"
CLEANED_SIMPLE_NAME = "  - Cleaned simple_name '{name}'"

# (H) Semantic/embedding logs
SEMANTIC_NOT_AVAILABLE = (
    "Semantic search dependencies not available, skipping embedding generation"
)
INGESTOR_NO_QUERY = "Ingestor does not support querying, skipping embedding generation"
NO_FUNCTIONS_FOR_EMBEDDING = "No functions or methods found for embedding generation"
GENERATING_EMBEDDINGS = "Generating embeddings for {count} functions/methods"
EMBEDDING_PROGRESS = "Generated {done}/{total} embeddings"
EMBEDDING_FAILED = "Failed to embed {name}: {error}"
NO_SOURCE_FOR = "No source code found for {name}"
EMBEDDINGS_COMPLETE = "Successfully generated {count} semantic embeddings"
EMBEDDING_GENERATION_FAILED = "Failed to generate semantic embeddings: {error}"
EMBEDDING_STORE_FAILED = "Failed to store embedding for {name}: {error}"
EMBEDDING_SEARCH_FAILED = "Failed to search embeddings: {error}"

# (H) Image logs
IMAGE_COPIED = "Copied image to temporary path: {path}"

# (H) Protobuf service logs
PROTOBUF_INIT = "ProtobufFileIngestor initialized to write to: {path}"
PROTOBUF_NO_MESSAGE_CLASS = (
    "No Protobuf message class found for label '{label}'. Skipping node."
)
PROTOBUF_NO_ONEOF_MAPPING = (
    "No 'oneof' field mapping found for label '{label}'. Skipping node."
)
PROTOBUF_UNKNOWN_REL_TYPE = (
    "Unknown relationship type '{rel_type}'. Setting to UNSPECIFIED."
)
PROTOBUF_INVALID_REL = (
    "Invalid relationship: source_id={source_id}, target_id={target_id}"
)
PROTOBUF_FLUSH_SUCCESS = "Successfully flushed {nodes} unique nodes and {rels} unique relationships to {path}"
PROTOBUF_FLUSHING = "Flushing data to {path}..."

# (H) Parser loader logs
BUILDING_BINDINGS = "Building Python bindings for {lang}..."
BUILD_FAILED = "Failed to build {lang} bindings: stdout={stdout}, stderr={stderr}"
BUILD_SUCCESS = "Successfully built {lang} bindings"
IMPORTING_MODULE = "Attempting to import module: {module}"
LOADED_FROM_SUBMODULE = (
    "Successfully loaded {lang} from submodule bindings using {attr}"
)
NO_LANG_ATTR = (
    "Module {module} imported but has no language attribute. Available: {available}"
)
SUBMODULE_LOAD_FAILED = "Failed to load {lang} from submodule bindings: {error}"
LIB_NOT_AVAILABLE = "Tree-sitter library for {lang} not available."
LOCALS_QUERY_FAILED = "Failed to create locals query for {lang}: {error}"
GRAMMAR_LOADED = "Successfully loaded {lang} grammar."
GRAMMAR_LOAD_FAILED = "Failed to load {lang} grammar: {error}"
INITIALIZED_PARSERS = "Initialized parsers for: {languages}"

# (H) File watcher logs
WATCHER_ACTIVE = "File watcher is now active."
WATCHER_SKIP_NO_QUERY = "Ingestor does not support querying, skipping real-time update."
CHANGE_DETECTED = "Change detected: {event_type} on {path}. Updating graph."
DELETION_QUERY = "Ran deletion query for path: {path}"
RECALC_CALLS = "Recalculating all function call relationships for consistency..."
GRAPH_UPDATED = "Graph updated successfully for change in: {name}"
INITIAL_SCAN = "Performing initial full codebase scan..."
INITIAL_SCAN_DONE = "Initial scan complete. Starting real-time watcher."
WATCHING = "Watching for changes in: {path}"
LOGGER_CONFIGURED = "Logger configured for Real-Time Updater."

# (H) Build logs
BUILD_BINARY = "Building binary: {name}"
BUILD_PROGRESS = "This may take a few minutes..."
BUILD_READY = "Binary is ready for distribution!"
BINARY_INFO = "Binary: {path}"
BINARY_SIZE = "Size: {size:.1f} MB"
BUILD_STDOUT = "STDOUT: {stdout}"
BUILD_STDERR = "STDERR: {stderr}"

# (H) Comment check logs
COMMENTS_FOUND = "Comments without (H) marker found:"
COMMENT_ERROR = "  {error}"

# (H) Graph summary logs
GRAPH_SUMMARY = "Graph Summary:"
GRAPH_TOTAL_NODES = "   Total nodes: {count:,}"
GRAPH_TOTAL_RELS = "   Total relationships: {count:,}"
GRAPH_EXPORTED_AT = "   Exported at: {timestamp}"
GRAPH_NODE_TYPES = "Node Types:"
GRAPH_NODE_COUNT = "   {label}: {count:,} nodes"
GRAPH_REL_TYPES = "Relationship Types:"
GRAPH_REL_COUNT = "   {rel_type}: {count:,} relationships"
GRAPH_FOUND_NODES = "Found {count} '{label}' nodes."
GRAPH_EXAMPLE_NAMES = "   Example {label} names:"
GRAPH_EXAMPLE_NAME = "      - {name}"
GRAPH_MORE_NODES = "      ... and {count} more"
GRAPH_ANALYZING = "Analyzing graph from: {path}"
GRAPH_ANALYSIS_COMPLETE = "Analysis complete!"
GRAPH_ANALYSIS_ERROR = "Error analyzing graph: {error}"
GRAPH_FILE_NOT_FOUND = "Graph file not found: {path}"

# (H) FQN logs
FQN_RESOLVE_FAILED = "Failed to resolve FQN for node at {path}: {error}"
FQN_FIND_FAILED = "Failed to find function by FQN {fqn} in {path}: {error}"
FQN_EXTRACT_FAILED = "Failed to extract function FQNs from {path}: {error}"

# (H) Source extraction logs
SOURCE_FILE_NOT_FOUND = "Source file not found: {path}"
SOURCE_INVALID_RANGE = "Invalid line range: {start}-{end}"
SOURCE_RANGE_EXCEEDS = "Line range {start}-{end} exceeds file length {length} in {path}"
SOURCE_EXTRACT_FAILED = "Failed to extract source from {path}: {error}"
SOURCE_AST_FAILED = "AST extraction failed for {name}: {error}"

# (H) Memgraph logs
MG_CONNECTING = "Connecting to Memgraph at {host}:{port}..."
MG_CONNECTED = "Successfully connected to Memgraph."
MG_EXCEPTION = "An exception occurred: {error}. Flushing remaining items..."
MG_DISCONNECTED = "\nDisconnected from Memgraph."
MG_CYPHER_ERROR = "!!! Cypher Error: {error}"
MG_CYPHER_QUERY = "    Query: {query}"
MG_CYPHER_PARAMS = "    Params: {params}"
MG_BATCH_ERROR = "!!! Batch Cypher Error: {error}"
MG_BATCH_PARAMS_TRUNCATED = "    Params (first 10 of {count}): {params}..."
MG_CLEANING_DB = "--- Cleaning database... ---"
MG_DB_CLEANED = "--- Database cleaned. ---"
MG_ENSURING_CONSTRAINTS = "Ensuring constraints..."
MG_CONSTRAINTS_DONE = "Constraints checked/created."
MG_NODE_BUFFER_FLUSH = (
    "Node buffer reached batch size ({size}). Performing incremental flush."
)
MG_REL_BUFFER_FLUSH = (
    "Relationship buffer reached batch size ({size}). Performing incremental flush."
)
MG_NO_CONSTRAINT = "No unique constraint defined for label '{label}'. Skipping flush."
MG_MISSING_PROP = "Skipping {label} node missing required '{key}' property: {props}"
MG_NODES_FLUSHED = "Flushed {flushed} of {total} buffered nodes."
MG_NODES_SKIPPED = (
    "Skipped {count} buffered nodes due to missing identifiers or constraints."
)
MG_CALLS_FAILED = "Failed to create {count} CALLS relationships - nodes may not exist"
MG_CALLS_SAMPLE = "  Sample {index}: {from_label}.{from_val} -> {to_label}.{to_val}"
MG_RELS_FLUSHED = (
    "Flushed {total} relationships ({success} successful, {failed} failed)."
)
MG_FLUSH_START = "--- Flushing all pending writes to database... ---"
MG_FLUSH_COMPLETE = "--- Flushing complete. ---"
MG_FETCH_QUERY = "Executing fetch query: {query} with params: {params}"
MG_WRITE_QUERY = "Executing write query: {query} with params: {params}"
MG_EXPORTING = "Exporting graph data..."
MG_EXPORTED = "Exported {nodes} nodes and {rels} relationships"

# (H) LLM/Cypher logs
CYPHER_GENERATING = "  [CypherGenerator] Generating query for: '{query}'"
CYPHER_GENERATED = "  [CypherGenerator] Generated Cypher: {query}"
CYPHER_ERROR = "  [CypherGenerator] Error: {error}"

# (H) Tool file logs
TOOL_FILE_READ = "[FileReader] Attempting to read file: {path}"
TOOL_FILE_READ_SUCCESS = "[FileReader] Successfully read text from {path}"
TOOL_FILE_BINARY = "[FileReader] {message}"
TOOL_FILE_WRITE = "[FileWriter] Creating file: {path}"
TOOL_FILE_WRITE_SUCCESS = "[FileWriter] Successfully wrote {chars} characters to {path}"
TOOL_FILE_EDIT = "[FileEditor] Attempting full file replacement: {path}"
TOOL_FILE_EDIT_SUCCESS = "[FileEditor] Successfully replaced entire file: {path}"
TOOL_FILE_EDIT_SURGICAL = (
    "[FileEditor] Attempting surgical block replacement in: {path}"
)
TOOL_FILE_EDIT_SURGICAL_SUCCESS = (
    "[FileEditor] Successfully applied surgical block replacement in: {path}"
)
TOOL_QUERY_RECEIVED = "[Tool:QueryGraph] Received NL query: '{query}'"
TOOL_QUERY_ERROR = "[Tool:QueryGraph] Error during query execution: {error}"
TOOL_SHELL_EXEC = "Executing shell command: {cmd}"
TOOL_SHELL_RETURN = "Return code: {code}"
TOOL_SHELL_STDOUT = "Stdout: {stdout}"
TOOL_SHELL_STDERR = "Stderr: {stderr}"
TOOL_SHELL_KILLED = "Process killed due to timeout."
TOOL_SHELL_ALREADY_TERMINATED = (
    "Process already terminated when timeout kill was attempted."
)
TOOL_SHELL_ERROR = "An error occurred while executing command: {error}"
TOOL_DOC_ANALYZE = "[DocumentAnalyzer] Analyzing '{path}' with question: '{question}'"

# (H) Shell timing log
SHELL_TIMING = "'{func}' executed in {time:.2f}ms"

# (H) File editor logs
EDITOR_NO_PARSER = "No parser available for {path}"
EDITOR_NO_LANG_CONFIG = "No language config found for extension {ext}"
EDITOR_FUNC_NOT_FOUND_AT_LINE = "No function '{name}' found at line {line}"
EDITOR_FUNC_NOT_FOUND_QN = "No function found with qualified name '{name}'"
EDITOR_AMBIGUOUS = (
    "Ambiguous function name '{name}' in {path}. "
    "Found {count} matches: {details}. "
    "Using first match. Consider using qualified name (e.g., 'ClassName.{name}') "
    "or specify line number for precise targeting."
)
EDITOR_FUNC_NOT_IN_FILE = "Function '{name}' not found in {path}."
EDITOR_PATCHES_NOT_CLEAN = "Patches for function '{name}' did not apply cleanly."
EDITOR_NO_CHANGES = "No changes detected after replacement."
EDITOR_REPLACE_SUCCESS = "Successfully replaced function '{name}' in {path}."
EDITOR_PATCH_FAILED = "Some patches failed to apply cleanly to {path}"
EDITOR_PATCH_SUCCESS = "Successfully applied patch to {path}"
EDITOR_PATCH_ERROR = "Error applying patch to {path}: {error}"
EDITOR_FILE_NOT_FOUND = "File not found: {path}"
EDITOR_BLOCK_NOT_FOUND = "Target block not found in {path}"
EDITOR_LOOKING_FOR = "Looking for: {block}"
EDITOR_MULTIPLE_OCCURRENCES = (
    "Multiple occurrences of target block found. Only replacing first occurrence."
)
EDITOR_NO_CHANGES_IDENTICAL = (
    "No changes detected - target and replacement are identical"
)
EDITOR_SURGICAL_FAILED = "Surgical patches failed to apply cleanly"
EDITOR_SURGICAL_ERROR = "Error during surgical block replacement: {error}"

# (H) Directory lister logs
DIR_LISTING = "Listing contents of directory: {path}"
DIR_LIST_ERROR = "Error listing directory {path}: {error}"

# (H) Semantic search logs
SEMANTIC_NO_MATCH = "No semantic matches found for query: {query}"
SEMANTIC_FOUND = "Found {count} semantic matches for: {query}"
SEMANTIC_FAILED = "Semantic search failed for query '{query}': {error}"
SEMANTIC_NODE_NOT_FOUND = "No node found with ID: {id}"
SEMANTIC_INVALID_LOCATION = "Missing or invalid source location info for node {id}"
SEMANTIC_SOURCE_FAILED = "Failed to get source code for node {id}: {error}"
SEMANTIC_TOOL_SEARCH = "[Tool:SemanticSearch] Searching for: '{query}'"
SEMANTIC_TOOL_SOURCE = "[Tool:GetFunctionSource] Retrieving source for node ID: {id}"

# (H) Document analyzer logs
DOC_COPIED = "Copied external file to: {path}"
DOC_SUCCESS = "Successfully received analysis for '{path}'."
DOC_NO_TEXT = "No text found in response: {response}"
DOC_API_ERROR = "Google GenAI API error for '{path}': {error}"
DOC_FAILED = "Failed to analyze document '{path}': {error}"
DOC_RESULT = "[analyze_document] Result type: {type}, content: {preview}..."
DOC_EXCEPTION = "[analyze_document] Exception during analysis: {error}"

# (H) Code retrieval logs
CODE_RETRIEVER_INIT = "CodeRetriever initialized with root: {root}"
CODE_RETRIEVER_SEARCH = "[CodeRetriever] Searching for: {name}"
CODE_RETRIEVER_ERROR = "[CodeRetriever] Error: {error}"
CODE_TOOL_RETRIEVE = "[Tool:GetCode] Retrieving code for: {name}"

# (H) Tool init logs
FILE_EDITOR_INIT = "FileEditor initialized with root: {root}"
FILE_READER_INIT = "FileReader initialized with root: {root}"
SHELL_COMMANDER_INIT = "ShellCommander initialized with root: {root}"
DOC_ANALYZER_INIT = "DocumentAnalyzer initialized with root: {root}"

# (H) Tool error logs
FILE_EDITOR_WARN = "[FileEditor] {msg}"
FILE_EDITOR_ERR = "[FileEditor] {msg}"
FILE_EDITOR_ERR_EDIT = "[FileEditor] Error editing file {path}: {error}"
FILE_READER_ERR = "Error reading file {path}: {error}"
DOC_ANALYZER_API_ERR = "[DocumentAnalyzer] API validation error: {error}"

# (H) File writer logs
FILE_WRITER_INIT = "FileWriter initialized with root: {root}"
FILE_WRITER_CREATE = "[FileWriter] Creating file: {path}"
FILE_WRITER_SUCCESS = "[FileWriter] Successfully wrote {chars} characters to {path}"

# (H) Error logs (used with logger.error/warning)
UNEXPECTED = "An unexpected error occurred: {error}"
EXPORT_ERROR = "Export error: {error}"
PATH_NOT_IN_QUESTION = (
    "Could not find original path in question for replacement: {path}"
)
IMAGE_NOT_FOUND = "Image path found, but does not exist: {path}"
IMAGE_COPY_FAILED = "Failed to copy image to temporary directory: {error}"
FILE_OUTSIDE_ROOT = "Security risk: Attempted to {action} file outside of project root."

# (H) Call processor logs
CALL_PROCESSING_FILE = "Processing calls in cached AST for: {path}"
CALL_PROCESSING_FAILED = "Failed to process calls in {path}: {error}"
CALL_FOUND_NODES = "Found {count} call nodes in {language} for {caller}"
CALL_FOUND = (
    "Found call from {caller} to {call_name} (resolved as {callee_type}:{callee_qn})"
)
CALL_NESTED_FOUND = "Found nested call from {caller} to {call_name} (resolved as {callee_type}:{callee_qn})"
CALL_DIRECT_IMPORT = "Direct import resolved: {call_name} -> {qn}"
CALL_TYPE_INFERRED = "Type-inferred object method resolved: {call_name} -> {method_qn} (via {obj}:{var_type})"
CALL_TYPE_INFERRED_INHERITED = (
    "Type-inferred inherited object method resolved: {call_name} -> {method_qn} "
    "(via {obj}:{var_type})"
)
CALL_IMPORT_STATIC = "Import-resolved static call: {call_name} -> {method_qn}"
CALL_OBJECT_METHOD = "Object method resolved: {call_name} -> {method_qn}"
CALL_INSTANCE_ATTR = (
    "Instance-resolved self-attribute call: {call_name} -> {method_qn} "
    "(via {attr_ref}:{var_type})"
)
CALL_INSTANCE_ATTR_INHERITED = (
    "Instance-resolved inherited self-attribute call: {call_name} -> {method_qn} "
    "(via {attr_ref}:{var_type})"
)
CALL_IMPORT_QUALIFIED = "Import-resolved qualified call: {call_name} -> {method_qn}"
CALL_INSTANCE_QUALIFIED = "Instance-resolved qualified call: {call_name} -> {method_qn} (via {class_name}:{var_type})"
CALL_INSTANCE_INHERITED = "Instance-resolved inherited call: {call_name} -> {method_qn} (via {class_name}:{var_type})"
CALL_WILDCARD = "Wildcard-resolved call: {call_name} -> {qn}"
CALL_SAME_MODULE = "Same-module resolution: {call_name} -> {qn}"
CALL_TRIE_FALLBACK = "Trie-based fallback resolution: {call_name} -> {qn}"
CALL_UNRESOLVED = "Could not resolve call: {call_name}"
CALL_CHAINED = (
    "Resolved chained call: {call_name} -> {method_qn} (via {obj_expr}:{obj_type})"
)
CALL_CHAINED_INHERITED = "Resolved chained inherited call: {call_name} -> {method_qn} (via {obj_expr}:{obj_type})"
CALL_SUPER_NO_CONTEXT = "No class context provided for super() call: {call_name}"
CALL_SUPER_NO_INHERITANCE = "No inheritance info for class {class_qn}"
CALL_SUPER_NO_PARENTS = "No parent classes found for {class_qn}"
CALL_SUPER_RESOLVED = "Resolved super() call: {call_name} -> {method_qn}"
CALL_SUPER_UNRESOLVED = (
    "Could not resolve super() call: {call_name} in parents of {class_qn}"
)
CALL_JAVA_RESOLVED = "Java method call resolved: {call_text} -> {method_qn}"
CALL_UNEXPECTED_PARENT = (
    "Unexpected parent type for node {node}: {parent_type}. Skipping."
)
