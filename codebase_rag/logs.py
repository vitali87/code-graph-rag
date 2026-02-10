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

# (H) Function ingest logs
FUNC_FOUND = "  Found Function: {name} (qn: {qn})"
FUNC_EXPECTED_NODE = "Expected Node but got {actual_type}: {value}"
METHOD_FOUND = "    Found Method: {name} (qn: {qn})"
EXPORT_FOUND = "  Found {export_type}: {name} (qn: {qn})"

# (H) Definition processor logs
DEF_PARSING_AST = "Parsing and Caching AST for {language}: {path}"
DEF_UNSUPPORTED_LANGUAGE = "Unsupported language '{language}' for {path}"
DEF_NO_PARSER = "No parser available for {language}"
DEF_PARSE_FAILED = "Failed to parse or ingest {path}: {error}"
DEF_PARSING_DEPENDENCY = "  Parsing dependency file: {path}"
DEF_FOUND_DEPENDENCY = "    Found dependency: {name} (spec: {spec})"

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

# (H) Ignore pattern logs
CGRIGNORE_LOADED = (
    "Loaded {exclude_count} exclude and {unignore_count} unignore patterns from {path}"
)
CGRIGNORE_READ_FAILED = "Failed to read {path}: {error}"

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

# (H) No-docs check logs
NO_DOCS_VIOLATIONS_FOUND = (
    "No-docs violations found (module docstrings or inline comments):"
)
NO_DOCS_ERROR = "  {error}"

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
MG_DELETING_PROJECT = "--- Deleting project: {project_name} ---"
MG_PROJECT_DELETED = "--- Project {project_name} deleted. ---"
MG_ENSURING_CONSTRAINTS = "Ensuring constraints..."
MG_CONSTRAINTS_DONE = "Constraints checked/created."
MG_ENSURING_INDEXES = "Ensuring label-property indexes for MERGE performance..."
MG_INDEXES_DONE = "Indexes checked/created."
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

# (H) Generic function timing log
FUNC_TIMING = "{func} completed in {time:.2f}ms"

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
INDEXING_FAILED = "Indexing failed"
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

# (H) Dependency parser logs
DEP_PARSE_ERROR_PYPROJECT = "Error parsing pyproject.toml {path}: {error}"
DEP_PARSE_ERROR_REQUIREMENTS = "Error parsing requirements.txt {path}: {error}"
DEP_PARSE_ERROR_PACKAGE_JSON = "Error parsing package.json {path}: {error}"
DEP_PARSE_ERROR_CARGO = "Error parsing Cargo.toml {path}: {error}"
DEP_PARSE_ERROR_GOMOD = "Error parsing go.mod {path}: {error}"
DEP_PARSE_ERROR_GEMFILE = "Error parsing Gemfile {path}: {error}"
DEP_PARSE_ERROR_COMPOSER = "Error parsing composer.json {path}: {error}"
DEP_PARSE_ERROR_CSPROJ = "Error parsing .csproj {path}: {error}"

# (H) Import processor logs
IMP_TOOL_NOT_AVAILABLE = "External tool '{tool}' not available for stdlib introspection"
IMP_CACHE_LOADED = "Loaded stdlib cache from {path}"
IMP_CACHE_LOAD_ERROR = "Could not load stdlib cache: {error}"
IMP_CACHE_SAVED = "Saved stdlib cache to {path}"
IMP_CACHE_SAVE_ERROR = "Could not save stdlib cache: {error}"
IMP_CACHE_CLEARED = "Cleared stdlib cache from disk"
IMP_CACHE_CLEAR_ERROR = "Could not clear stdlib cache from disk: {error}"
IMP_PARSED_COUNT = "Parsed {count} imports in {module}"
IMP_CREATED_RELATIONSHIP = (
    "  Created IMPORTS relationship: {from_module} -> {to_module} (from {full_name})"
)
IMP_PARSE_FAILED = "Failed to parse imports in {module}: {error}"
IMP_IMPORT = "  Import: {local} -> {full}"
IMP_ALIASED_IMPORT = "  Aliased import: {alias} -> {full}"
IMP_WILDCARD_IMPORT = "  Wildcard import: * -> {module}"
IMP_FROM_IMPORT = "  From import: {local} -> {full}"
IMP_JS_DEFAULT = "JS default import: {name} -> {module}.default"
IMP_JS_NAMED = "JS named import: {local} -> {module}.{name}"
IMP_JS_NAMESPACE = "JS namespace import: {name} -> {module}"
IMP_JS_REQUIRE = "JS require: {var} -> {module}"
IMP_JS_REEXPORT = "JS re-export: {exported} -> {module}.{original}"
IMP_JS_NAMESPACE_REEXPORT = "JS namespace re-export: * -> {module}"
IMP_JAVA_WILDCARD = "Java wildcard import: {path}.*"
IMP_JAVA_STATIC = "Java static import: {name} -> {path}"
IMP_JAVA_IMPORT = "Java import: {name} -> {path}"
IMP_RUST = "Rust import: {name} -> {path}"
IMP_GO = "Go import: {package} -> {path}"
IMP_CPP_INCLUDE = "C++ include: {local} -> {full} (system: {system})"
IMP_CPP_MODULE = "C++20 module import: {local} -> {full}"
IMP_CPP_MODULE_IMPL = "C++20 module implementation: {name}"
IMP_CPP_MODULE_IFACE = "C++20 module interface: {name}"
IMP_CPP_PARTITION = "C++20 module partition import: {partition} -> {full}"
IMP_GENERIC = "Generic import parsing for {language}: {node_type}"

# (H) Structure processor logs
STRUCT_IDENTIFIED_PACKAGE = "  Identified Package: {package_qn}"
STRUCT_IDENTIFIED_FOLDER = "  Identified Folder: '{relative_root}'"

# (H) Class ingest logs
CLASS_CPP_MODULE_INTERFACE = "  Found C++ Module Interface: {qn}"
CLASS_CPP_MODULE_IMPL = "  Found C++ Module Implementation: {qn}"
CLASS_FOUND_INTERFACE = "  Found Interface: {name} (qn: {qn})"
CLASS_FOUND_ENUM = "  Found Enum: {name} (qn: {qn})"
CLASS_FOUND_TYPE = "  Found Type: {name} (qn: {qn})"
CLASS_FOUND_STRUCT = "  Found Struct: {name} (qn: {qn})"
CLASS_FOUND_UNION = "  Found Union: {name} (qn: {qn})"
CLASS_FOUND_TEMPLATE = "  Found Template {node_type}: {name} (qn: {qn})"
CLASS_FOUND_EXPORTED_STRUCT = "  Found Exported Struct: {name} (qn: {qn})"
CLASS_FOUND_EXPORTED_UNION = "  Found Exported Union: {name} (qn: {qn})"
CLASS_FOUND_EXPORTED_TEMPLATE = "  Found Exported Template Class: {name} (qn: {qn})"
CLASS_FOUND_EXPORTED_CLASS = "  Found Exported Class: {name} (qn: {qn})"
CLASS_FOUND_CLASS = "  Found Class: {name} (qn: {qn})"
CLASS_FOUND_INLINE_MODULE = "  Found Inline Module: {name} (qn: {qn})"
CLASS_PASS_4 = "--- Pass 4: Processing Method Override Relationships ---"
CLASS_METHOD_OVERRIDE = "Method override: {method_qn} OVERRIDES {parent_method_qn}"
CLASS_CPP_INHERITANCE = "Found C++ inheritance: {parent_name} -> {parent_qn}"

# (H) Java type inference logs
JAVA_VAR_TYPE_MAP_BUILT = "Built Java variable type map with {count} entries"
JAVA_VAR_TYPE_MAP_FAILED = "Failed to build Java variable type map: {error}"
JAVA_PARAM = "Parameter: {name} -> {type}"
JAVA_VARARGS_PARAM = "Varargs parameter: {name} -> {type}"
JAVA_LOCAL_VAR_INFERRED = "Local variable (inferred): {name} -> {type}"
JAVA_LOCAL_VAR_DECLARED = "Local variable (declared): {name} -> {type}"
JAVA_CLASS_FIELD = "Class field: {name} -> {type}"
JAVA_ASSIGNMENT = "Assignment: {name} -> {type}"
JAVA_NO_METHOD_NAME = "No method name found in call node"
JAVA_RESOLVING_CALL = "Resolving Java method call: method={method}, object={object}"
JAVA_RESOLVING_STATIC = "Resolving static/local method: {method}"
JAVA_FOUND_STATIC = "Found static/local method: {result}"
JAVA_STATIC_NOT_FOUND = "Static/local method not found: {method}"
JAVA_RESOLVING_OBJ_TYPE = "Resolving object type for: {object}"
JAVA_OBJ_TYPE_UNKNOWN = "Could not determine type of object: {object}"
JAVA_OBJ_TYPE_RESOLVED = "Object type resolved to: {type}"
JAVA_FOUND_INSTANCE = "Found instance method: {result}"
JAVA_INSTANCE_NOT_FOUND = "Instance method not found: {type}.{method}"
JAVA_ENHANCED_FOR_VAR = "Enhanced for loop variable: {name} -> {type}"
JAVA_ENHANCED_FOR_VAR_ALT = "Enhanced for loop variable (alt): {name} -> {type}"

# (H) JS type inference logs
JS_VAR_DECLARATOR_FOUND = "Found variable declarator: {var_name} in {module_qn}"
JS_VAR_INFERRED = "Inferred JS variable: {var_name} -> {var_type}"
JS_VAR_INFER_FAILED = "Could not infer type for variable: {var_name}"
JS_VAR_TYPE_MAP_BUILT = "Built JS variable type map with {count} variables (found {declarator_count} declarators total)"
JS_INFER_VALUE_NODE = "Inferring type from value node type: {node_type}"
JS_CALL_EXPR_FUNC_NODE = "Call expression func_node type: {func_type}"
JS_EXTRACTED_METHOD_CALL = "Extracted method call: {method_call}"
JS_TYPE_INFERRED = "JS type inference: {method_call}() returns {inferred_type}"
JS_RETURN_TYPE_INFER_FAILED = "Could not infer return type for {method_call}()"
JS_NO_PATTERN_MATCHED = (
    "No type inference pattern matched for value node type: {node_type}"
)
JS_METHOD_CALL_INVALID = "Method call {method_call} doesn't have 2 parts"
JS_CLASS_RESOLVE_FAILED = (
    "Could not resolve class name {class_name} in module {module_qn}"
)
JS_CLASS_RESOLVED = "Resolved {class_name} to {class_qn}"
JS_LOOKING_FOR_METHOD = "Looking for method {method_qn} in function registry"
JS_METHOD_AST_NOT_FOUND = "Could not find AST node for method {method_qn}"
JS_RETURN_ANALYZED = (
    "Analyzed return statements for {method_qn}, got type: {return_type}"
)
JS_METHOD_RETURN_ERROR = (
    "Error inferring JS method return type for {method_call}: {error}"
)

# (H) Lua type inference logs
LUA_VAR_TYPE_MAP_BUILT = "Built Lua variable type map with {count} variables"
LUA_VAR_INFERRED = "Inferred Lua variable: {var_name} -> {var_type}"
LUA_TYPE_INFERENCE_RETURN = (
    "Lua type inference: {class_name}:{method_name}() returns {class_qn}"
)

# (H) Python type inference logs
PY_BUILD_VAR_MAP_FAILED = "Failed to build local variable type map: {error}"
PY_PARAM_TYPE_INFERRED = "Inferred parameter type: {param} -> {type}"
PY_TYPE_INFER_ATTEMPT = (
    "Attempting to infer type for parameter '{param}' in module '{module}'"
)
PY_AVAILABLE_CLASSES = "Available classes in scope: {classes}"
PY_BEST_MATCH = "Best match for '{param}' is '{match}' with score {score}"
PY_INSTANCE_VAR_INFERRED = "Inferred instance variable: {attr} -> {type}"
PY_LOOP_VAR_INFERRED = "Inferred loop variable type: {var} -> {type}"
PY_TYPE_SIMPLE = "Inferred type (simple): {var} -> {type}"
PY_TYPE_COMPLEX = "Inferred type (complex): {var} -> {type}"
PY_TYPE_INFERRED = "Inferred type: {var} -> {type}"
PY_RECURSION_GUARD = "Recursion guard (method call): skipping {method}"
PY_RECURSION_GUARD_QN = "Recursion guard: skipping {method_qn}"
PY_RESOLVED_METHOD = "Resolved {class_name}.{method_name} to {method_qn}"
PY_INFER_ATTR_FAILED = "Failed to analyze instance variables for {attr}: {error}"
PY_INFER_RETURN_FAILED = "Failed to infer return type for {method}: {error}"
PY_VAR_FROM_CONTEXT = "Found variable type from method context: {var} -> {type}"
PY_VAR_CANNOT_INFER = "Cannot infer type for variable reference: {var}"
PY_NO_CONTAINING_CLASS = "No containing class found for method"
PY_NO_INIT_METHOD = "No __init__ method found in class"
PY_FOUND_INIT = "Found __init__ method, analyzing self assignments..."
PY_FOUND_CLASS_AT_LEVEL = "Found class_definition at level {level}"
PY_SEARCHING_LEVEL = "Level {level}: node type = {node_type}"
PY_NO_CLASS_IN_HIERARCHY = "No class_definition found in parent hierarchy"
PY_SEARCHING_INIT = "Searching for __init__ method in class with {count} children"
PY_CHILD_TYPE = "  Child type: {type}"
PY_NO_CLASS_BODY = "  No class body (block) found"
PY_SEARCHING_BODY = "  Searching in class body with {count} children"
PY_BODY_CHILD = "    Body child type: {type}"
PY_FOUND_METHOD = "      Found method: {name}"
PY_FOUND_INIT_METHOD = "      Found __init__ method!"
PY_INIT_NOT_FOUND = "  No __init__ method found in class body"

# (H) JS/TS ingest logs
JS_PROTOTYPE_INHERITANCE = "Prototype inheritance: {child_qn} INHERITS {parent_qn}"
JS_PROTOTYPE_INHERITANCE_FAILED = "Failed to detect prototype inheritance: {error}"
JS_PROTOTYPE_METHOD_FOUND = "  Found Prototype Method: {method_name} (qn: {method_qn})"
JS_PROTOTYPE_METHOD_DEFINES = "Prototype method: {constructor_qn} DEFINES {method_qn}"
JS_PROTOTYPE_METHODS_FAILED = "Failed to detect prototype methods: {error}"
JS_OBJECT_METHOD_FOUND = "  Found Object Method: {method_name} (qn: {method_qn})"
JS_OBJECT_METHODS_PROCESS_FAILED = "Failed to process object literal methods: {error}"
JS_OBJECT_METHODS_DETECT_FAILED = "Failed to detect object literal methods: {error}"
JS_OBJECT_ARROW_FOUND = (
    "  Found Object Arrow Function: {function_name} (qn: {function_qn})"
)
JS_ASSIGNMENT_ARROW_FOUND = (
    "  Found Assignment Arrow Function: {function_name} (qn: {function_qn})"
)
JS_ASSIGNMENT_FUNC_EXPR_FOUND = (
    "  Found Assignment Function Expression: {function_name} (qn: {function_qn})"
)
JS_ASSIGNMENT_ARROW_QUERY_FAILED = (
    "Failed to process assignment arrow functions query: {error}"
)
JS_ASSIGNMENT_ARROW_DETECT_FAILED = (
    "Failed to detect assignment arrow functions: {error}"
)

# (H) JS/TS module system logs
JS_COMMONJS_DESTRUCTURE_FAILED = (
    "Failed to process CommonJS destructuring pattern: {error}"
)
JS_MISSING_IMPORT_PATTERNS_FAILED = "Failed to detect missing import patterns: {error}"
JS_COMMONJS_VAR_DECLARATOR_FAILED = (
    "Failed to process variable declarator for CommonJS: {error}"
)
JS_COMMONJS_IMPORT_FAILED = "Failed to process CommonJS import {imported_name}: {error}"
JS_MISSING_IMPORT_PATTERN = (
    "Missing pattern: {module_qn} IMPORTS {imported_name} from {resolved_source_module}"
)
JS_COMMONJS_EXPORTS_QUERY_FAILED = "Failed to process CommonJS exports query: {error}"
JS_COMMONJS_EXPORTS_DETECT_FAILED = "Failed to detect CommonJS exports: {error}"
JS_ES6_EXPORTS_QUERY_FAILED = "Failed to process ES6 exports query: {error}"
JS_ES6_EXPORTS_DETECT_FAILED = "Failed to detect ES6 exports: {error}"

# (H) MCP tool logs
MCP_INDEXING_REPO = "[MCP] Indexing repository at: {path}"
MCP_CLEARING_DB = "[MCP] Clearing existing database to avoid conflicts..."
MCP_DB_CLEARED = "[MCP] Database cleared. Starting fresh indexing..."
MCP_CLEARING_PROJECT = "[MCP] Clearing existing data for project '{project_name}'..."
MCP_ERROR_INDEXING = "[MCP] Error indexing repository: {error}"
MCP_LISTING_PROJECTS = "[MCP] Listing all projects..."
MCP_ERROR_LIST_PROJECTS = "[MCP] Error listing projects: {error}"
MCP_DELETING_PROJECT = "[MCP] Deleting project: {project_name}"
MCP_ERROR_DELETE_PROJECT = "[MCP] Error deleting project: {error}"
MCP_WIPING_DATABASE = "[MCP] Wiping entire database!"
MCP_ERROR_WIPE = "[MCP] Error wiping database: {error}"
MCP_QUERY_CODE_GRAPH = "[MCP] query_code_graph: {query}"
MCP_QUERY_RESULTS = "[MCP] Query returned {count} results"
MCP_ERROR_QUERY = "[MCP] Error querying code graph: {error}"
MCP_GET_CODE_SNIPPET = "[MCP] get_code_snippet: {name}"
MCP_ERROR_CODE_SNIPPET = "[MCP] Error retrieving code snippet: {error}"
MCP_SURGICAL_REPLACE = "[MCP] surgical_replace_code in {path}"
MCP_ERROR_REPLACE = "[MCP] Error replacing code: {error}"
MCP_READ_FILE = "[MCP] read_file: {path} (offset={offset}, limit={limit})"
MCP_ERROR_READ = "[MCP] Error reading file: {error}"
MCP_WRITE_FILE = "[MCP] write_file: {path}"
MCP_ERROR_WRITE = "[MCP] Error writing file: {error}"
MCP_LIST_DIR = "[MCP] list_directory: {path}"
MCP_ERROR_LIST_DIR = "[MCP] Error listing directory: {error}"
QUERY_MODE_WRITE_BLOCKED = "[MCP] Write operation blocked in query mode for: {path}"

# (H) MCP server logs
MCP_SERVER_INFERRED_ROOT = "[GraphCode MCP] Using inferred project root: {path}"
MCP_SERVER_NO_ROOT = (
    "[GraphCode MCP] No project root configured, using current directory: {path}"
)
MCP_SERVER_ROOT_RESOLVED = "[GraphCode MCP] Project root resolved to: {path}"
MCP_SERVER_USING_ROOT = "[GraphCode MCP] Using project root: {path}"
MCP_SERVER_CONFIG_ERROR = "[GraphCode MCP] Configuration error: {error}"
MCP_SERVER_INIT_SERVICES = "[GraphCode MCP] Initializing services..."
MCP_SERVER_INIT_SUCCESS = "[GraphCode MCP] Services initialized successfully"
MCP_SERVER_CALLING_TOOL = "[GraphCode MCP] Calling tool: {name}"
MCP_SERVER_UNKNOWN_TOOL = "[GraphCode MCP] Unknown tool: {name}"
MCP_SERVER_TOOL_ERROR = "[GraphCode MCP] Error executing tool '{name}': {error}"
MCP_SERVER_STARTING = "[GraphCode MCP] Starting MCP server..."
MCP_SERVER_MODE = "[GraphCode MCP] Server running in mode: {mode}"
MCP_SERVER_CREATED = "[GraphCode MCP] Server created, starting stdio transport..."
MCP_SERVER_CONNECTED = "[GraphCode MCP] Connected to Memgraph at {host}:{port}"
MCP_SERVER_FATAL_ERROR = "[GraphCode MCP] Fatal error: {error}"
MCP_SERVER_SHUTDOWN = "[GraphCode MCP] Shutting down server..."
MCP_TOOLS_REGISTRY_MODE = (
    "[GraphCode MCP] MCPToolsRegistry initialized in '{mode}' mode"
)

# (H) Exclude prompt logs
EXCLUDE_INVALID_INDEX = "Invalid index: {index} (out of range)"
EXCLUDE_INVALID_INPUT = "Invalid input: '{input}' (expected number)"

# (H) Model switching logs
MODEL_SWITCHED = "Model switched to: {model}"
MODEL_SWITCH_FAILED = "Failed to switch model: {error}"
MODEL_CURRENT = "Current model: {model}"

# (H) Path parse logs
METHOD_PATH_CALC_FAILED = "Failed to calculate paths for method {qn}: {error}"
NO_ABSOLUTE_PATH_FALLBACK = (
    "No absolute_path found for {qn}, falling back to relative path"
)
