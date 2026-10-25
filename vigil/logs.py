BAD_REQUIREMENT = "Skipped a requirement vigil cannot parse: {req!r}"
CANNOT_WIDEN = "Cannot widen {req!r} to admit {latest}: unsupported clause"
REQUIREMENT_NOT_FOUND = (
    "Expected {req!r} exactly once in pyproject.toml; refusing to rewrite a guess"
)
PYPI_MISS = "PyPI has no JSON for {package} (HTTP {status}); skipped"
CORPUS_MOVED = (
    "Corpus {name} is at {head}, not its pinned {commit}; delete the checkout"
)
SYNCING = "uv sync in {path} (extras: {extras})"
RUNNING_TESTS = "Running {count} test files in {where}"
DIGESTS = "{corpus}: base {base} / candidate {cand}"
OUTCOME = "{verdict} {item}: {headline}"
COMMITTED = "Committed {sha} on {branch}"
INDEXING = "Indexing {repo} into the embedded graph so vigil can ask it what each package reaches"
NEW_ITEMS = "{new} new of {total} outdated packages"
NOTHING_TO_RUN = "Nothing new to run"
WROTE_REPORT = "Wrote {path}"
CANDIDATE_READY = "Candidate worktree {path} on {branch}: edit there, then `vigil gate`"
