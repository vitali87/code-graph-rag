# Path-based source classification shared by the dead-code engine and the
# endpoint emission passes (issue #910), so both judge "is this a test file"
# with one heuristic.

from . import constants as cs


def matches_test_path(
    path: str, patterns: tuple[str, ...] = cs.TEST_PATH_PATTERNS
) -> bool:
    """True when a repo-relative path names test code.

    Patterns match against a leading-slash-normalized path so a dir pattern
    like ``/tests/`` also matches a ROOT ``tests/`` dir (Rust integration
    tests, a top-level tests/ folder), not just a nested ``src/tests/``. The
    leading slash keeps ``contests/`` from matching ``/tests/``. Callers
    pass POSIX-style relative paths; an absolute path would let directories
    outside the repo (a ``/tmp/pytest-*/`` parent) misclassify everything.
    """
    normalized = (
        path if path.startswith(cs.SEPARATOR_SLASH) else cs.SEPARATOR_SLASH + path
    )
    return any(pattern in normalized for pattern in patterns)
