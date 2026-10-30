MISSING_INDEXER = (
    "No SCIP indexer for {language}: '{binary}' is not on PATH (pass a "
    "prebuilt index with --index to grade one anyway)"
)
MISSING_PROJECT_FILE = (
    "The {language} SCIP indexer needs {file} at the project root {target}"
)
INDEXING = "Running {binary} over {target}"
INDEXER_FAILED = "{binary} exited {code}:\n{stderr}"
INDEX_READ = "SCIP index: {tool} {version}, {documents} documents"
ORACLE_DONE = (
    "SCIP oracle: {callables} first-party callables, {edges} call edges "
    "({dispatch} with overrides)"
)
CGR_RUNNING = "Building cgr graph for {target} (project={project})"
CGR_DONE = "cgr {language} CALLS edges: {edges} ({unmatched} callables SCIP lacks)"
UNKNOWN_CORPUS = "Unknown corpus '{name}'; known: {known}"
NEED_TARGET = "Pass --corpus, or both --language and --target"
