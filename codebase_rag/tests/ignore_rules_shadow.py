"""Check every ignore decision a test run makes against the Rust matcher.

Loaded as a plugin (`-p codebase_rag.tests.ignore_rules_shadow`), it wraps
the deciding functions of `codebase_rag/utils/path_utils.py`, wherever they
were imported, so each call also asks the frame's `IgnoreRules` the same
question with the same exclude and unignore sets. The Python answer is the
one returned (or the exception raised), so the tests run as they always do;
a Rust answer that differs, or a raise on one side only, fails the test that
made the call, in its fixtures too, and the run (`shadows.py`).
`test_engine_ignore_rules.py` runs the existing ignore tests
under it (docs/reports/PHASE2_FRAME.md, section 13, step 4).

`should_skip_path` keeps its containment check in Python: it reads the file
system, which the walk (step 5) will do itself. The rest of its decision is
the Rust `should_skip_rel_path`. A call made while a test has patched the
built-in names or endings (to stand in for an older cgr) is not compared:
the Rust rules hold their own copies, which `test_engine_ignore_rules.py`
holds equal to the real ones.
"""

from __future__ import annotations

import functools
import inspect
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.tests.shadows import SHOWN, ShadowFailures
from codebase_rag.utils import path_utils

SUMMARY = (
    "ignore-rules shadow: {compared} decisions compared, {differed} differed,"
    " {skipped} made with patched constants"
)
FAILED = "the Rust ignore rules answered differently:\n"

_differences: list[str] = []
_FAILURES = ShadowFailures("ignore_rules_shadow", _differences, FAILED)
# Decisions compared, and decisions made while the constants were patched.
_compared = [0]
_skipped = [0]

Arguments = Mapping[str, object]


@functools.cache
def _rules(
    exclude: frozenset[str], unignore: frozenset[str]
) -> engine_frame.IgnoreRules:
    rules = engine_frame.new_ignore_rules(sorted(exclude), sorted(unignore))
    assert rules is not None, "the extension lacks IgnoreRules; rebuild the crate"
    return rules


def _constants_as_rust_holds_them() -> bool:
    rules_type = type(_rules(frozenset(), frozenset()))
    unconditional = frozenset(getattr(rules_type, "UNCONDITIONAL_IGNORE_SUFFIXES"))
    rescuable = frozenset(getattr(rules_type, "RESCUABLE_IGNORE_SUFFIXES"))
    return (
        frozenset(cs.IGNORE_PATTERNS)
        == frozenset(getattr(rules_type, "BUILTIN_IGNORED_DIRS"))
        and frozenset(cs.VCS_METADATA_DIRS)
        == frozenset(getattr(rules_type, "VCS_METADATA_DIRS"))
        and frozenset(cs.UNCONDITIONAL_IGNORE_FILENAME_ENDINGS) == unconditional
        and frozenset(cs.IGNORE_FILENAME_ENDINGS) == unconditional | rescuable
    )


def _set(patterns: object) -> frozenset[str]:
    if patterns is None:
        return frozenset()
    assert isinstance(patterns, Iterable)
    return frozenset(str(pattern) for pattern in patterns)


def _both(arguments: Arguments) -> engine_frame.IgnoreRules:
    return _rules(_set(arguments["exclude_paths"]), _set(arguments["unignore_paths"]))


def _matches_ignore_patterns(arguments: Arguments) -> bool:
    rules = _rules(_set(arguments["patterns"]), frozenset())
    return rules.matches_exclude(str(arguments["rel_path_str"]))


def _should_keep_dir(arguments: Arguments) -> bool:
    return _both(arguments).should_keep_dir(
        str(arguments["dirname"]), str(arguments["dir_prefix"])
    )


def _should_skip_rel_file(arguments: Arguments) -> bool:
    dir_parts = arguments["dir_parts"]
    assert isinstance(dir_parts, Iterable)
    return _both(arguments).should_skip_rel_file(
        str(arguments["rel_path_str"]), tuple(dir_parts)
    )


def _should_skip_path(arguments: Arguments) -> bool:
    path, repo_path, is_file = (
        arguments["path"],
        arguments["repo_path"],
        arguments["is_file"],
    )
    assert isinstance(path, Path) and isinstance(repo_path, Path)
    resolved_is_file = path.is_file() if is_file is None else bool(is_file)
    # The containment check reads the file system; it stays in Python.
    try:
        path.resolve().relative_to(repo_path.resolve())
    except ValueError:
        return True
    rel_path = path_utils.cached_relative_path(path, repo_path).as_posix()
    return _both(arguments).should_skip_rel_path(rel_path, resolved_is_file)


def _unignore_names_this_file(arguments: Arguments) -> bool:
    rules = _rules(frozenset(), _set(arguments["patterns"]))
    return rules.unignore_names_this_file(str(arguments["rel_path_str"]))


def _unignore_could_match_within(arguments: Arguments) -> bool:
    rules = _rules(frozenset(), frozenset({str(arguments["pattern"])}))
    return rules.unignore_could_match_within(str(arguments["rel_dir"]))


def _unignore_names_vcs_dir(arguments: Arguments) -> bool:
    rules = _rules(frozenset(), _set(arguments["unignore_paths"]))
    return rules.unignore_names_vcs_dir(str(arguments["rel_dir"]))


# Each path_utils function, and the Rust question that answers it.
_SHADOWED: dict[str, Callable[[Arguments], bool]] = {
    "matches_ignore_patterns": _matches_ignore_patterns,
    "should_keep_dir": _should_keep_dir,
    "should_skip_rel_file": _should_skip_rel_file,
    "should_skip_path": _should_skip_path,
    "unignore_names_this_file": _unignore_names_this_file,
    "unignore_could_match_within": _unignore_could_match_within,
    "unignore_names_vcs_dir": _unignore_names_vcs_dir,
}


def _outcome(call: Callable[[], object]) -> tuple[object, Exception | None]:
    try:
        return call(), None
    except Exception as error:
        return None, error


def _shown(answer: object, error: Exception | None) -> str:
    return f"raises {type(error).__name__}" if error is not None else repr(answer)


def _shadow(
    name: str, original: Callable[..., bool], rust: Callable[[Arguments], bool]
) -> Callable[..., bool]:
    signature = inspect.signature(original)

    def compare(
        expected: object,
        raised: Exception | None,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        actual, rust_raised = _outcome(lambda: rust(bound.arguments))
        _compared[0] += 1
        if (raised is None) != (rust_raised is None) or expected != actual:
            called = ", ".join(f"{k}={v!r}" for k, v in bound.arguments.items())
            _differences.append(
                f"{name}({called}): Python {_shown(expected, raised)},"
                f" Rust {_shown(actual, rust_raised)}"
            )

    @functools.wraps(original)
    def shadow(*args: object, **kwargs: object) -> bool:
        expected, raised = _outcome(lambda: original(*args, **kwargs))
        if _constants_as_rust_holds_them():
            compare(expected, raised, args, kwargs)
        else:
            _skipped[0] += 1
        if raised is not None:
            raise raised
        assert isinstance(expected, bool)
        return expected

    return shadow


def _install() -> None:
    originals = {name: getattr(path_utils, name) for name in _SHADOWED}
    shadows = {
        name: _shadow(name, originals[name], rust) for name, rust in _SHADOWED.items()
    }
    # Modules that imported a function by name before this plugin loaded
    # hold the original; the rest will import the shadow from path_utils.
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        for name, original in originals.items():
            if namespace.get(name) is original:
                namespace[name] = shadows[name]


_install()


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(_FAILURES, _FAILURES.name)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    terminalreporter.write_line(
        SUMMARY.format(
            compared=_compared[0], differed=len(_differences), skipped=_skipped[0]
        )
    )
    for difference in _differences[:SHOWN]:
        terminalreporter.write_line(difference)
