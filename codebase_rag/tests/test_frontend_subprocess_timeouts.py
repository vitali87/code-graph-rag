# Gate: every frontend helper-tool subprocess call is bounded (issue #1462).
#
# `available()` protects against a toolchain that is ABSENT. It does nothing
# for one that is present but wedged -- a stale build artifact, a lock, a
# network-backed module fetch -- and an unbounded `subprocess.run` in a
# frontend then blocks indexing with no diagnostic and no bound.
#
# A gate rather than per-call fixes, because the two offending calls were
# written alongside six that DID pass a timeout: the convention already
# existed and was silently not followed, which is exactly the drift a
# structural assertion catches and a point fix does not.
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO_ROOT / "codebase_rag" / "parsers"

# Frontend packages that shell out to a compiler or language server.
_FRONTEND_DIRS = (
    "go_frontend",
    "java_frontend",
    "csharp_frontend",
    "py_frontend",
    "cpp_frontend",
)

# Parser helpers that also invoke external language tools but sit directly
# under `parsers/` rather than in a *_frontend package. Both are bounded today;
# they are scanned so that STAYS true, which is the point of a gate rather than
# a sweep. Greptile found them missing by adding an unbounded call to each and
# observing the test still passed.
_FRONTEND_FILES = (
    "java_lombok.py",
    "stdlib_extractor.py",
)


def _scanned_paths() -> list[Path]:
    """Every file the gate covers: the frontend packages plus the loose helpers."""
    paths = [
        p for d in _FRONTEND_DIRS for p in sorted((_FRONTEND_ROOT / d).rglob("*.py"))
    ]
    paths += [_FRONTEND_ROOT / name for name in _FRONTEND_FILES]
    return paths


def _unbounded_run_calls(tree: ast.AST) -> list[int]:
    """Lines of `*.run(...)` calls with no `timeout=`.

    Matches on the `run` attribute rather than on `subprocess.run`
    specifically, so a module-level `from subprocess import run` is still
    seen. Over-reporting is the safe direction: a false positive costs one
    explicit timeout, a false negative costs an unbounded hang.
    """
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else None
        if name is None and isinstance(target, ast.Name):
            name = target.id
        if name != "run":
            continue
        if "timeout" not in {kw.arg for kw in node.keywords if kw.arg}:
            found.append(node.lineno)
    return found


def test_every_frontend_subprocess_call_is_bounded() -> None:
    """An unbounded helper-tool call can hang indexing indefinitely.

    The protocol's invariant is that a missing toolchain degrades to the
    tree-sitter backbone, never worse. A wedged toolchain must degrade the
    same way, and only a timeout makes that possible.
    """
    offenders: list[str] = []
    for path in _scanned_paths():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        offenders.extend(f"{rel}:{line}" for line in _unbounded_run_calls(tree))

    assert not offenders, (
        "frontend helper-tool calls with no timeout (issue #1462); a wedged "
        "toolchain blocks indexing with no bound:\n" + "\n".join(offenders)
    )


def test_the_scan_reaches_the_frontend_packages() -> None:
    """A control: the gate above passes vacuously if it parses nothing.

    An empty file list and a clean tree are indistinguishable in that
    assertion, which is the failure mode this repo keeps meeting.
    """
    seen = _scanned_paths()

    assert len(seen) >= len(_FRONTEND_DIRS), f"only {len(seen)} files scanned"
    # Every named loose helper must actually exist, or a rename silently
    # removes it from the gate's coverage and nothing reports the hole.
    missing = [
        name for name in _FRONTEND_FILES if not (_FRONTEND_ROOT / name).is_file()
    ]
    assert not missing, f"scanned helpers that no longer exist: {missing}"


def test_the_detector_recognises_bounded_and_unbounded_calls() -> None:
    """A second control: the matcher must fire and stay quiet correctly.

    Proves the gate can fail, rather than being green because its detection
    is broken.
    """
    assert _unbounded_run_calls(ast.parse("subprocess.run(['x'])")) == [1]
    assert _unbounded_run_calls(ast.parse("run(['x'])")) == [1]
    assert _unbounded_run_calls(ast.parse("subprocess.run(['x'], timeout=5)")) == []
