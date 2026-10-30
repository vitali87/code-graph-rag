EXTRACTING = "Extracting TraceEval archive {path}"
UNSAFE_MEMBER = "Refusing archive member outside the extraction root: {name}"
ARCHIVE_HASH_MISMATCH = (
    "TraceEval archive {path} has sha256 {actual}, expected {expected}; "
    "the pinned ground truth is not what this checkout holds"
)
PROGRAM_MISSING = "Skipping {path}: no callgraph.json"
SELECTED = "TraceEval {split} split: {count} programs ({languages})"
RUNNING = "Indexing each program with cgr ({workers} workers)"
PROGRAM_FAILED = "cgr failed on {language}/{program}: {error}"
FAILED_SUMMARY = "{count} programs failed to index; their truth edges count as missed"
DONE = "Scored {count} programs"
