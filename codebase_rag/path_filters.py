# Path-based source classification shared by the dead-code engine, the
# tests-reaching walks and the endpoint emission passes (issues #910, #2618),
# so every consumer judges "is this a test file" with one heuristic.

from functools import lru_cache

from . import constants as cs


# Every symbol of a file asks about the same path, so a dead-code scan over
# a large graph pays for each distinct file once, not once per node.
@lru_cache(maxsize=cs.TEST_PATH_CACHE_SIZE)
def matches_test_path(path: str) -> bool:
    """True when a repo-relative POSIX path names test code.

    Each directory segment and the file name are judged as whole words
    (see the TEST_NAME_* constants), so ``shortest_paths/`` and
    ``latest_prices.py`` stay production while ``Tests/``, ``Foo.Tests/``
    and ``snappy_unittest.cc`` are tests. A leading slash is the repo root.
    Callers pass repo-relative paths: an absolute one would let a directory
    outside the repo (a ``/tmp/pytest-*/`` parent) classify everything
    under it.
    """
    segments = [s for s in path.split(cs.SEPARATOR_SLASH) if s]
    if not segments:
        return False
    *dirs, file_name = segments
    return _is_test_file(file_name) or any(
        _is_test_dir(segment, at_root=index == 0) for index, segment in enumerate(dirs)
    )


def _is_test_dir(segment: str, at_root: bool) -> bool:
    folded = segment.strip(cs.CHAR_UNDERSCORE).lower()
    if folded in cs.TEST_DIR_NAMES or (at_root and folded in cs.TEST_ROOT_DIR_NAMES):
        return True
    head, *qualifiers = segment.split(cs.SEPARATOR_DOT)
    return _is_test_name(head) or any(
        _is_test_qualifier(part) or _is_test_name(part) for part in qualifiers
    )


def _is_test_file(file_name: str) -> bool:
    # Only the parts between the stem and the extension qualify it
    # (foo.test.ts); the extension itself never does.
    stem, *suffixes = file_name.split(cs.SEPARATOR_DOT)
    return _is_test_name(stem) or any(
        _is_test_qualifier(part) for part in suffixes[:-1]
    )


def _is_test_qualifier(part: str) -> bool:
    return part.lower() in cs.TEST_NAME_QUALIFIERS


def _is_test_name(name: str) -> bool:
    words = name.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE).split(cs.CHAR_UNDERSCORE)
    return any(
        word.lower() in cs.TEST_NAME_WORDS or _is_camel_test_word(word)
        for word in words
    )


def _is_camel_test_word(word: str) -> bool:
    if word.endswith(cs.TEST_NAME_CAMEL_SUFFIXES):
        return True
    prefix = cs.TEST_NAME_CAMEL_PREFIX
    return (
        word.startswith(prefix)
        and len(word) > len(prefix)
        and word[len(prefix)].isupper()
    )
