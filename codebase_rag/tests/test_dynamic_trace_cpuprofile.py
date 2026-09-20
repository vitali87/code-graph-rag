# V8 .cpuprofile files (node --cpu-prof) encode observed call stacks as a
# node tree; the converter must turn project-scoped parent/child links into
# interchange call records, seeing through runtime-internal frames, mapping
# 0-based lines to 1-based, and attributing module toplevels (issue #1247).

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.cpuprofile import convert_cpuprofile
from codebase_rag.trace.records import read_trace_file


def _frame(function_name, url, line):
    return {
        "functionName": function_name,
        "scriptId": "1",
        "url": url,
        "lineNumber": line,
        "columnNumber": 0,
    }


def _node(node_id, frame, children=(), hit_count=0):
    return {
        "id": node_id,
        "callFrame": frame,
        "hitCount": hit_count,
        "children": list(children),
    }


def _profile(tmp_path, dependency_directory="node_modules"):
    """(root)->(main toplevel)->runAll->[handle->greet, forEach->callback]."""
    # Build file URLs with as_uri() so a Windows drive path yields a valid
    # `file:///C:/...` URI (drive kept out of the authority) on any platform.
    main = (tmp_path / "main.js").as_uri()
    registry = (tmp_path / "src" / "registry.js").as_uri()
    vendored = (tmp_path / dependency_directory / "lib" / "index.js").as_uri()
    return {
        "nodes": [
            _node(1, _frame("(root)", "", 0), children=[2]),
            _node(2, _frame("", main, 0), children=[3], hit_count=1),
            _node(3, _frame("runAll", main, 2), children=[4, 6, 8], hit_count=2),
            _node(4, _frame("handle", registry, 6), children=[5], hit_count=3),
            # The registry dispatch static analysis cannot see.
            _node(5, _frame("greet", registry, 10), hit_count=7),
            # A runtime-internal frame between two project frames must be
            # walked through, like the JVM agent's stack walk.
            _node(6, _frame("forEach", "node:internal/per_context", 40), children=[7]),
            _node(7, _frame("callback", main, 8), hit_count=4),
            # Vendored code under the repo root stays out of scope.
            _node(8, _frame("vendored", vendored, 1), hit_count=9),
        ],
        "startTime": 0,
        "endTime": 1000,
        "samples": [],
        "timeDeltas": [],
    }


def _convert(tmp_path, workload=None):
    profile_path = tmp_path / "main.cpuprofile"
    profile_path.write_text(json.dumps(_profile(tmp_path)))
    output = tmp_path / "trace.jsonl"
    count = convert_cpuprofile(
        profile_path, repo_root=tmp_path, output=output, workload=workload
    )
    header, records = read_trace_file(output)
    return count, header, list(records)


def test_converts_project_edges_with_one_based_lines(tmp_path):
    count, header, records = _convert(tmp_path)

    assert header.language == cs.TRACE_LANGUAGE_JS
    assert header.repo_root == str(tmp_path)
    assert count == len(records)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}

    dispatch = edges[("handle", "greet")]
    assert dispatch.caller.path.endswith("src/registry.js")
    assert dispatch.caller.line == 7
    assert dispatch.callee.line == 11


def test_edge_counts_are_callee_subtree_samples(tmp_path):
    _count, _header, records = _convert(tmp_path)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}

    # handle's subtree: own 3 + greet 7.
    assert edges[("runAll", "handle")].count == 10
    assert edges[("handle", "greet")].count == 7


def test_toplevel_frame_maps_to_module_qualname(tmp_path):
    _count, _header, records = _convert(tmp_path)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}

    assert (cs.TRACE_QUALNAME_MODULE, "runAll") in edges


def test_runtime_internal_frames_are_walked_through(tmp_path):
    _count, _header, records = _convert(tmp_path)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}

    assert ("runAll", "callback") in edges
    assert not any("forEach" in pair for edge in edges for pair in edge)


def test_vendored_and_internal_frames_never_appear(tmp_path):
    _count, _header, records = _convert(tmp_path)

    for record in records:
        assert "node_modules" not in record.caller.path
        assert "node_modules" not in record.callee.path
        assert not record.caller.path.startswith("node:")
        assert not record.callee.path.startswith("node:")


@pytest.mark.parametrize(
    ("directory", "excluded_on_posix"),
    [
        ("node_modules", True),
        ("NODE_MODULES", False),
        ("Node_Modules", False),
        ("site-packages", True),
        ("SITE-PACKAGES", False),
        (".venv", True),
        (".VENV", False),
    ],
)
def test_dependency_directory_case_follows_platform(
    tmp_path: Path, directory: str, excluded_on_posix: bool
) -> None:
    count = _convert_raw(tmp_path, _profile(tmp_path, directory))
    _header, records = read_trace_file(tmp_path / "out.jsonl")
    edges = {(record.caller.qualname, record.callee.qualname) for record in records}
    expected = {
        (cs.TRACE_QUALNAME_MODULE, "runAll"),
        ("runAll", "handle"),
        ("handle", "greet"),
        ("runAll", "callback"),
    }
    if os.name != "nt" and not excluded_on_posix:
        expected.add(("runAll", "vendored"))

    assert edges == expected
    assert count == len(expected)


def test_workload_label_lands_on_every_record(tmp_path):
    _count, _header, records = _convert(tmp_path, workload="suite")

    assert records
    for record in records:
        assert record.workloads == ("suite",)


def test_malformed_profile_is_rejected(tmp_path):
    profile_path = tmp_path / "broken.cpuprofile"
    profile_path.write_text("{}")

    with pytest.raises(ValueError):
        convert_cpuprofile(
            profile_path, repo_root=tmp_path, output=tmp_path / "out.jsonl"
        )


def _convert_raw(tmp_path, profile):
    profile_path = tmp_path / "raw.cpuprofile"
    profile_path.write_text(json.dumps(profile))
    return convert_cpuprofile(
        profile_path, repo_root=tmp_path, output=tmp_path / "out.jsonl"
    )


def test_missing_child_reference_is_rejected_not_crashed(tmp_path):
    profile = {
        "nodes": [_node(1, _frame("(root)", "", 0), children=[99])],
        "samples": [],
        "timeDeltas": [],
    }

    with pytest.raises(ValueError):
        _convert_raw(tmp_path, profile)


def test_cyclic_or_shared_children_are_rejected_not_crashed(tmp_path):
    url = (tmp_path / "main.js").as_uri()
    profile = {
        "nodes": [
            _node(1, _frame("a", url, 1), children=[2]),
            _node(2, _frame("b", url, 2), children=[1]),
        ],
        "samples": [],
        "timeDeltas": [],
    }

    with pytest.raises(ValueError):
        _convert_raw(tmp_path, profile)


def test_deep_profiles_do_not_hit_the_recursion_limit(tmp_path):
    url = (tmp_path / "main.js").as_uri()
    depth = 5000
    nodes = [
        _node(i, _frame(f"f{i}", url, i), children=[i + 1], hit_count=1)
        for i in range(1, depth)
    ]
    nodes.append(_node(depth, _frame(f"f{depth}", url, depth), hit_count=1))
    profile = {"nodes": nodes, "samples": [], "timeDeltas": []}

    count = _convert_raw(tmp_path, profile)

    assert count == depth - 1


def test_windows_drive_letter_urls_keep_project_frames(tmp_path):
    from codebase_rag.trace.cpuprofile import _url_to_path

    # A Windows drive URL keeps its drive and normalises to POSIX separators so
    # it matches the POSIX root_prefix on any platform.
    assert _url_to_path("file:///C:/repo/main.js") == "C:/repo/main.js"
    assert (
        _url_to_path((tmp_path / "main.js").as_uri())
        == (tmp_path / "main.js").as_posix()
    )


def test_invalid_json_is_rejected_as_trace_format_error(tmp_path):
    profile_path = tmp_path / "bad.cpuprofile"
    profile_path.write_text("{invalid")

    with pytest.raises(ValueError):
        convert_cpuprofile(
            profile_path, repo_root=tmp_path, output=tmp_path / "out.jsonl"
        )


def test_non_object_node_is_rejected_not_crashed(tmp_path):
    with pytest.raises(ValueError):
        _convert_raw(tmp_path, {"nodes": ["not-an-object"]})


def test_non_list_children_is_rejected_not_crashed(tmp_path):
    profile = {"nodes": [{"id": 1, "callFrame": _frame("a", "", 0), "children": 3}]}
    with pytest.raises(ValueError):
        _convert_raw(tmp_path, profile)


def test_duplicate_node_id_is_rejected_not_overwritten(tmp_path):
    profile = {
        "nodes": [
            _node(1, _frame("(root)", "", 0)),
            _node(1, _frame("dup", "", 0)),
        ]
    }
    with pytest.raises(ValueError):
        _convert_raw(tmp_path, profile)


def test_percent_escaped_url_is_decoded(tmp_path):
    from codebase_rag.trace.cpuprofile import _url_to_path

    # Spaces and other characters arrive percent-encoded; the decoded path must
    # match the real repo prefix.
    assert _url_to_path("file:///repo/my%20dir/main.js") == "/repo/my dir/main.js"


def test_non_int_child_entry_is_rejected_not_dropped(tmp_path):
    profile = {"nodes": [{"id": 1, "callFrame": _frame("a", "", 0), "children": ["x"]}]}
    with pytest.raises(ValueError):
        _convert_raw(tmp_path, profile)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("file://nas/share/my%20repo/main.js", "//nas/share/my repo/main.js"),
        (
            "file://nas/share/%E4%BD%A0%E5%A5%BD%20repo/main.js",
            "//nas/share/你好 repo/main.js",
        ),
        ("file:////nas/share/my%20repo/main.js", "//nas/share/my repo/main.js"),
        ("file://localhost/C:/repo/main.js", "C:/repo/main.js"),
        ("file://LOCALHOST/repo/main.js", "/repo/main.js"),
    ],
)
def test_file_url_authority_preserves_network_and_local_paths(
    url: str, expected: str
) -> None:
    from codebase_rag.trace.cpuprofile import _url_to_path

    assert _url_to_path(url) == expected


@pytest.mark.skipif(os.name != "nt", reason="UNC repository paths require Windows")
@pytest.mark.parametrize("dependency_directory", ["node_modules", "NODE_MODULES"])
@pytest.mark.parametrize(
    "profile_root",
    [
        "//nas/cgr-profile-tests/my repo 项目",
        "//NAS/cgr-profile-tests/my repo 项目",
        "//nas/CGR-PROFILE-TESTS/my repo 项目",
        "//nas/cgr-profile-tests/MY REPO 项目",
    ],
)
def test_unc_repository_keeps_profile_edges(
    tmp_path: Path, profile_root: str, dependency_directory: str
) -> None:
    repo_root = Path("//nas/cgr-profile-tests/my repo 项目")
    profile_path = tmp_path / "unc.cpuprofile"
    profile_path.write_text(
        json.dumps(_profile(Path(profile_root), dependency_directory)), encoding="utf-8"
    )
    output = tmp_path / "trace.jsonl"

    count = convert_cpuprofile(profile_path, repo_root, output)
    header, records = read_trace_file(output)
    edges = {
        (record.caller.qualname, record.callee.qualname): record for record in records
    }

    assert count == 4
    assert set(edges) == {
        (cs.TRACE_QUALNAME_MODULE, "runAll"),
        ("runAll", "handle"),
        ("handle", "greet"),
        ("runAll", "callback"),
    }
    assert header.repo_root == str(repo_root)
    dispatch = edges[("handle", "greet")]
    assert (
        dispatch.caller.path == (Path(profile_root) / "src" / "registry.js").as_posix()
    )
    assert dispatch.callee.path == dispatch.caller.path
    assert (dispatch.caller.line, dispatch.callee.line, dispatch.count) == (7, 11, 7)


@pytest.mark.skipif(os.name != "nt", reason="UNC repository paths require Windows")
@pytest.mark.parametrize(
    "profile_root",
    [
        "//other-nas/cgr-profile-tests/my repo 项目",
        "//nas/other-share/my repo 项目",
        "//nas/cgr-profile-tests/my repo 项目-sibling",
    ],
)
def test_other_unc_repositories_stay_excluded(
    tmp_path: Path, profile_root: str
) -> None:
    profile_path = tmp_path / "unc.cpuprofile"
    profile_path.write_text(json.dumps(_profile(Path(profile_root))), encoding="utf-8")
    output = tmp_path / "trace.jsonl"

    count = convert_cpuprofile(
        profile_path, Path("//nas/cgr-profile-tests/my repo 项目"), output
    )
    _header, records = read_trace_file(output)

    assert count == 0
    assert list(records) == []


@pytest.mark.skipif(os.name != "nt", reason="UNC repository paths require Windows")
@pytest.mark.parametrize(
    "source_directory", ["src", "node_modules", "NODE_MODULES", "Node_Modules"]
)
def test_unc_remapped_paths_keep_case_and_scope(
    tmp_path: Path, source_directory: str
) -> None:
    repo_root = Path("//nas/cgr-profile-tests/my repo 项目")
    source_path = (
        Path("//NAS/cgr-profile-tests/my repo 项目") / source_directory / "MixedCase.ts"
    )
    generated = tmp_path / "bundle.js"
    generated.write_text("//# sourceMappingURL=bundle.js.map\n", encoding="utf-8")
    generated.with_suffix(".js.map").write_text(
        json.dumps(
            {
                "version": 3,
                "sources": [source_path.as_posix()],
                "names": [],
                "mappings": "AAAA;AAKA",
            }
        ),
        encoding="utf-8",
    )
    profile_path = tmp_path / "remapped.cpuprofile"
    profile_path.write_text(
        json.dumps(
            {
                "nodes": [
                    _node(1, _frame("caller", generated.as_uri(), 0), children=[2]),
                    _node(2, _frame("callee", generated.as_uri(), 1), hit_count=7),
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "trace.jsonl"

    count = convert_cpuprofile(profile_path, repo_root, output)
    _header, records = read_trace_file(output)
    edges = list(records)

    if source_directory != "src":
        assert count == 0
        assert edges == []
    else:
        assert count == len(edges) == 1
        dispatch = edges[0]
        assert (dispatch.caller.qualname, dispatch.callee.qualname) == (
            "caller",
            "callee",
        )
        assert dispatch.caller.path == dispatch.callee.path == source_path.as_posix()
        assert (dispatch.caller.line, dispatch.callee.line, dispatch.count) == (1, 6, 7)


@pytest.mark.skipif(os.name == "nt", reason="POSIX repository paths are case-sensitive")
def test_case_distinct_posix_repository_stays_excluded(tmp_path: Path) -> None:
    repo_root = tmp_path / "project"
    repo_root.mkdir()
    profile_path = tmp_path / "case-distinct.cpuprofile"
    profile_path.write_text(
        json.dumps(_profile(tmp_path / "PROJECT")), encoding="utf-8"
    )
    output = tmp_path / "trace.jsonl"

    count = convert_cpuprofile(profile_path, repo_root, output)
    _header, records = read_trace_file(output)

    assert count == 0
    assert list(records) == []


def test_http_frames_stay_outside_the_project(tmp_path: Path) -> None:
    local_url = (tmp_path / "main.js").as_uri()
    profile = {
        "nodes": [
            _node(1, _frame("caller", local_url, 1), children=[2]),
            _node(
                2,
                _frame("remote", "https://example.invalid/main.js", 1),
                children=[3],
            ),
            _node(3, _frame("callee", local_url, 5), hit_count=3),
        ]
    }

    assert _convert_raw(tmp_path, profile) == 1
    _header, records = read_trace_file(tmp_path / "out.jsonl")
    assert [(record.caller.qualname, record.callee.qualname) for record in records] == [
        ("caller", "callee")
    ]
