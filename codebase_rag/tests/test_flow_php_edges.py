"""FLOWS_TO lean-walk coverage for PHP (issue #1174). PHP was absent from
`IO_SINKS`, so its modules carried `flow_covered: false` and every flow query over
PHP reported UNKNOWN. These tests exercise the descriptor-driven lean walk: a
superglobal / `getenv` source reaching an `echo`/`file_put_contents`/`fwrite` sink
emits a resource->resource edge, tainted arguments cross into callees, returned
taint feeds the fixpoint, and a branch merge preserves taint on any path."""

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
_STDOUT = "resource::STDOUT::<dynamic>"


def _run_flow(tmp_path: Path, source: str) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if "php" not in parsers:
        pytest.skip("php parser not available")
    (tmp_path / "a.php").write_text(source, encoding="utf-8")
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


def test_php_superglobal_get_flows_to_echo(tmp_path: Path) -> None:
    # `$_GET["q"]` is untrusted HTTP input (NETWORK); `echo` writes it to STDOUT.
    source = '<?php\nfunction leak() {\n  $g = $_GET["q"];\n  echo $g;\n}\n'
    assert ("resource::NETWORK::q", _STDOUT) in _run_flow(tmp_path, source)


def test_php_env_superglobal_flows_to_print(tmp_path: Path) -> None:
    # `$_ENV["K"]` is a process-env source; `print` is the single-operand keyword sink.
    source = '<?php\nfunction leak() {\n  $e = $_ENV["K"];\n  print $e;\n}\n'
    assert ("resource::ENV::K", _STDOUT) in _run_flow(tmp_path, source)


def test_php_getenv_flows_to_file_put_contents(tmp_path: Path) -> None:
    # `getenv` reads ENV; `file_put_contents(path, data)` writes the tainted data
    # (arg 1) to the file named by arg 0.
    source = (
        "<?php\nfunction leak() {\n"
        '  $s = getenv("K");\n'
        '  file_put_contents("out.txt", $s);\n'
        "}\n"
    )
    assert ("resource::ENV::K", "resource::FILE::out.txt") in _run_flow(
        tmp_path, source
    )


def test_php_tainted_argument_into_callee(tmp_path: Path) -> None:
    # A tainted value passed as an argument reaches a sink INSIDE the callee: the
    # parameter-to-sink summary composes across function bodies (issue #1169 machinery).
    source = (
        "<?php\nfunction sink($m) { echo $m; }\n"
        "function leak() {\n"
        '  $s = getenv("K");\n'
        "  sink($s);\n"
        "}\n"
    )
    assert ("resource::ENV::K", _STDOUT) in _run_flow(tmp_path, source)


def test_php_return_taint_fixpoint(tmp_path: Path) -> None:
    # A callee that RETURNS a tainted value taints the caller's binding through the
    # return-taint fixpoint, even though `src` is defined before its taint is known.
    source = (
        '<?php\nfunction src() { return getenv("K"); }\n'
        "function leak() {\n"
        "  $v = src();\n"
        "  echo $v;\n"
        "}\n"
    )
    assert ("resource::ENV::K", _STDOUT) in _run_flow(tmp_path, source)


def test_php_branch_merge_preserves_taint(tmp_path: Path) -> None:
    # Taint assigned on ONE branch of an if/else survives the MAY join, so the sink
    # after the merge still sees it (validates the PHP `body`/multi-`alternative`
    # if-walk).
    source = (
        "<?php\nfunction leak($x) {\n"
        '  if ($x) { $s = getenv("K"); } else { $s = "safe"; }\n'
        "  echo $s;\n"
        "}\n"
    )
    assert ("resource::ENV::K", _STDOUT) in _run_flow(tmp_path, source)


def test_php_elseif_chain_preserves_taint(tmp_path: Path) -> None:
    # The taint is set in an `elseif` arm; the flat sibling-`alternative` walk must
    # still union it into the join.
    source = (
        "<?php\nfunction leak($x, $y) {\n"
        '  if ($x) { $s = "a"; }\n'
        '  elseif ($y) { $s = getenv("K"); }\n'
        '  else { $s = "b"; }\n'
        "  echo $s;\n"
        "}\n"
    )
    assert ("resource::ENV::K", _STDOUT) in _run_flow(tmp_path, source)


def test_php_tainted_write_through_fopen_handle(tmp_path: Path) -> None:
    # `fopen` binds a FILE handle; `fwrite($f, $data)` REVERSES the libc arg order
    # (handle arg 0, data arg 1), so the tainted data flows to the file.
    source = (
        "<?php\nfunction leak() {\n"
        '  $s = getenv("K");\n'
        '  $f = fopen("out.txt", "w");\n'
        "  fwrite($f, $s);\n"
        "}\n"
    )
    assert ("resource::ENV::K", "resource::FILE::out.txt") in _run_flow(
        tmp_path, source
    )


def test_php_builtin_names_are_case_insensitive(tmp_path: Path) -> None:
    # PHP function names are case-insensitive, so `GETENV`/`FILE_PUT_CONTENTS`
    # resolve to the same sinks as their lowercase spellings (CodeRabbit review).
    source = (
        "<?php\nfunction leak() {\n"
        '  $s = GETENV("K");\n'
        '  FILE_PUT_CONTENTS("out.txt", $s);\n'
        "}\n"
    )
    assert ("resource::ENV::K", "resource::FILE::out.txt") in _run_flow(
        tmp_path, source
    )


def test_php_curl_exec_response_is_a_network_read_source(tmp_path: Path) -> None:
    # `curl_exec` is a READ_WRITE sink; its response is an untrusted NETWORK read
    # source, so echoing it flows NETWORK -> STDOUT (validates READ_WRITE entering
    # the read-sink map, CodeRabbit review).
    source = "<?php\nfunction leak($ch) {\n  $r = curl_exec($ch);\n  echo $r;\n}\n"
    assert ("resource::NETWORK::<dynamic>", _STDOUT) in _run_flow(tmp_path, source)


def test_php_untainted_io_emits_no_flow(tmp_path: Path) -> None:
    source = (
        "<?php\nfunction leak() {\n"
        '  $f = fopen("out.txt", "w");\n'
        '  fwrite($f, "literal");\n'
        '  echo "constant";\n'
        "}\n"
    )
    assert _run_flow(tmp_path, source) == set()
