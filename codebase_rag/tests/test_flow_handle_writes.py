"""Handle-based writes in the lean FLOWS_TO walk (issue #1204). A tainted value
written through a resource handle (`f := os.Create(..); f.Write(secret)`) must
emit a flow edge to the handle's resource, instead of the false NO_FLOW the walk
produced when it modelled only direct call sinks. First landing: Go."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


_LANG_BY_EXT = {".go": "go", ".rs": "rust"}


def _run_flow(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    needed = {
        _LANG_BY_EXT[Path(rel).suffix]
        for rel in files
        if Path(rel).suffix in _LANG_BY_EXT
    }
    missing = [lang for lang in needed if lang not in parsers]
    if missing:
        pytest.skip(f"parser(s) not available: {missing}")
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


_ENV_FILE = ("resource::ENV::K", "resource::FILE::out.txt")


def test_go_tainted_write_through_file_handle(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("out.txt")\n'
            "\tf.Write([]byte(s))\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_go_tainted_write_string_through_file_handle(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("out.txt")\n'
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_go_untainted_handle_write_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\tf, _ := os.Create("out.txt")\n'
            '\tf.WriteString("literal")\n'
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_go_handle_reassignment_tracks_the_new_resource(tmp_path: Path) -> None:
    # Rebinding the handle var to a second file redirects the write; the taint
    # must reach the SECOND file only, never the first.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("first.txt")\n'
            '\tf, _ = os.Create("second.txt")\n'
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::second.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::first.txt") not in flows


def test_go_read_method_is_not_a_write_sink(tmp_path: Path) -> None:
    # A read through the handle is not a taint sink; only writes emit flow edges.
    # The arg is genuinely tainted (`[]byte(s)` preserves taint), so this validates
    # that `Read` is classified as a READ method, not merely that the arg is clean.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("out.txt")\n'
            "\tf.Read([]byte(s))\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_go_read_only_open_handle_write_emits_no_flow(tmp_path: Path) -> None:
    # `os.Open` is read-only, so `f.Write` on it is not a real write sink and must
    # emit no edge (constructor access mode gates write emission).
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Open("out.txt")\n'
            "\tf.Write([]byte(s))\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_go_conditional_rebind_emits_to_both_files(tmp_path: Path) -> None:
    # `f` may hold either handle after the if, so the write must reach BOTH files
    # (path-sensitive MAY merge of the handle bindings).
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak(cond bool) {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("first.txt")\n'
            "\tif cond {\n"
            '\t\tf, _ = os.Create("second.txt")\n'
            "\t}\n"
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::second.txt") in flows


def test_go_for_rebind_preserves_outer_flow(tmp_path: Path) -> None:
    # A rebind inside a for body may or may not run; the outer handle's flow must
    # survive the loop join alongside the loop-body handle.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak(items []int) {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("first.txt")\n'
            "\tfor range items {\n"
            '\t\tf, _ = os.Create("loop.txt")\n'
            "\t}\n"
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::loop.txt") in flows


def test_go_switch_rebind_preserves_outer_flow(tmp_path: Path) -> None:
    # A rebind in one switch arm (no default) leaves the outer handle live on the
    # implicit no-match path; the write must reach both.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak(n int) {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("first.txt")\n'
            "\tswitch n {\n"
            "\tcase 1:\n"
            '\t\tf, _ = os.Create("case1.txt")\n'
            "\t}\n"
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::case1.txt") in flows


# --- Rust (issue #1204, second language) ---
_RUST_ENV_FILE = ("resource::ENV::K", "resource::FILE::out.txt")


def test_rust_tainted_write_through_file_handle_unwrap(tmp_path: Path) -> None:
    # `File::create(p).unwrap()` yields the handle; `s.as_bytes()` carries s's
    # taint (value-preserving conversion), so the write reaches the file.
    files = {
        "m.rs": (
            "fn leak() {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::create("out.txt").unwrap();\n'
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE in _run_flow(tmp_path, files)


def test_rust_tainted_write_through_file_handle_try(tmp_path: Path) -> None:
    # The `?` operator unwraps the Result to the inner handle just like `.unwrap()`.
    files = {
        "m.rs": (
            "use std::io::Write;\n"
            "fn leak() -> std::io::Result<()> {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::create("out.txt")?;\n'
            "    f.write_all(s.as_bytes())?;\n"
            "    Ok(())\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE in _run_flow(tmp_path, files)


def test_rust_imported_file_create(tmp_path: Path) -> None:
    # `use std::fs::File;` + `File::create(..)` resolves through the import map to
    # the registered `std::fs::File::create` constructor.
    files = {
        "m.rs": (
            "use std::fs::File;\n"
            "fn leak() {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = File::create("out.txt").unwrap();\n'
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE in _run_flow(tmp_path, files)


def test_rust_read_only_open_handle_write_emits_no_flow(tmp_path: Path) -> None:
    # `File::open` is read-only, so a write through it is not a real sink.
    files = {
        "m.rs": (
            "fn leak() {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::open("out.txt").unwrap();\n'
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE not in _run_flow(tmp_path, files)


def test_rust_untainted_handle_write_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "m.rs": (
            "fn leak() {\n"
            '    let mut f = std::fs::File::create("out.txt").unwrap();\n'
            '    f.write_all("literal".as_bytes()).unwrap();\n'
            "}\n"
        )
    }
    assert _RUST_ENV_FILE not in _run_flow(tmp_path, files)


def test_rust_conditional_rebind_emits_to_both_files(tmp_path: Path) -> None:
    # Path-sensitive handle bindings apply to Rust too: a rebind in the if arm
    # leaves both handles live, so the write reaches both files.
    files = {
        "m.rs": (
            "fn leak(cond: bool) {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::create("first.txt").unwrap();\n'
            "    if cond {\n"
            '        f = std::fs::File::create("second.txt").unwrap();\n'
            "    }\n"
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::second.txt") in flows
