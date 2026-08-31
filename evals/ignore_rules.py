# The eligible-file rules for a graded target, read from the same place the
# indexer reads them so grading covers the file set indexing covers.
#
# Both sides of every arm must consult this. Applying it to the cgr capture
# alone would drop true positives while the oracle still emitted rows for an
# excluded file, scoring as a recall regression that reads like a grading bug
# rather than a scope fix (issue #1520).
from pathlib import Path

from codebase_rag.config import load_ignore_patterns

IgnoreRules = tuple[frozenset[str] | None, frozenset[str] | None]


def ignore_rules(target: Path) -> IgnoreRules:
    """`(exclude_paths, unignore_paths)` for `target`, or `(None, None)`.

    `None` rather than an empty frozenset because that is what the indexer's
    own callers pass when a repository configures nothing, and what
    `GraphUpdater` and `should_skip_path` treat as "no rules"; an empty set
    is not equivalent everywhere downstream.
    """
    patterns = load_ignore_patterns(target)
    return (patterns.exclude or None, patterns.unignore or None)
