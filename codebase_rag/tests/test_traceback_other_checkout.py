# A traceback is usually pasted from somewhere other than the indexed checkout:
# a CI runner, a container, a teammate's machine, Windows. Its frames carry
# that machine's checkout root, so matching them to the graph by the local
# root alone resolved 0 of 8 frames and left rank_root_causes empty without a
# word (issue #2587). The same limit applied to `cgr trace ingest` for traces
# recorded in CI, whose header names the root they were recorded under.

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.crash_correlation import (
    CYPHER_CRASH_CALLS,
    CYPHER_CRASH_POSITIONAL_PARAMS,
    explain_traceback,
    rank_root_causes,
)
from codebase_rag.cypher_queries import (
    CYPHER_TRACE_CALLABLES,
    CYPHER_TRACE_EXISTING_CALLS,
)
from codebase_rag.flow_verdict import (
    CYPHER_FLOW_COVERAGE_GAPS,
    CYPHER_FLOW_EDGES,
    CYPHER_FLOW_REMOTE_EDGES,
)
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.trace import resolution
from codebase_rag.trace.ingest import ingest_trace
from codebase_rag.trace.records import (
    CallRecord,
    FramePoint,
    TraceHeader,
    write_trace_file,
)
from codebase_rag.trace.resolution import PathRebase
from codebase_rag.utils.path_utils import derive_project_name

_P = "tbdemo__cafe02"
_OUTSIDE = cs.TraceUnresolvedReason.OUTSIDE_REPO.value
_UNKNOWN = cs.TraceUnresolvedReason.UNKNOWN_PATH.value

_CI_ROOT = "/home/runner/work/tbdemo/tbdemo"
_DOCKER_ROOT = "/app"
_WINDOWS_ROOT = "C:\\Users\\dev\\tbdemo"
_SITE_PACKAGES = "/usr/local/lib/python3.12/site-packages"


def _row(
    label: str, qn: str, path: str, start: int | None = None, end: int | None = None
) -> dict:
    return {
        cs.KEY_LABEL: label,
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: start,
        cs.KEY_END_LINE: end,
    }


def _shop_nodes(project: str) -> list[dict]:
    """The issue's `shop/` package: cli.main -> checkout -> Cart.total ->
    apply_tax -> load_rate, which raises."""
    return [
        _row(cs.NodeLabel.MODULE, f"{project}.shop.cli", "shop/cli.py"),
        _row(
            cs.NodeLabel.FUNCTION, f"{project}.shop.cli.checkout", "shop/cli.py", 12, 17
        ),
        _row(cs.NodeLabel.FUNCTION, f"{project}.shop.cli.main", "shop/cli.py", 20, 24),
        _row(cs.NodeLabel.MODULE, f"{project}.shop.pricing", "shop/pricing.py"),
        _row(
            cs.NodeLabel.FUNCTION,
            f"{project}.shop.pricing.load_rate",
            "shop/pricing.py",
            5,
            7,
        ),
        _row(
            cs.NodeLabel.FUNCTION,
            f"{project}.shop.pricing.apply_tax",
            "shop/pricing.py",
            9,
            11,
        ),
        _row(
            cs.NodeLabel.CLASS,
            f"{project}.shop.pricing.Cart",
            "shop/pricing.py",
            13,
            19,
        ),
        _row(
            cs.NodeLabel.METHOD,
            f"{project}.shop.pricing.Cart.total",
            "shop/pricing.py",
            17,
            19,
        ),
        # A root-level module, the shape a different project's same-named
        # file would collide with.
        _row(cs.NodeLabel.MODULE, f"{project}.utils", "utils.py"),
        _row(cs.NodeLabel.FUNCTION, f"{project}.utils.slugify", "utils.py", 1, 20),
        # The same basename twice, under two packages.
        _row(cs.NodeLabel.MODULE, f"{project}.app.helpers", "app/helpers.py"),
        _row(
            cs.NodeLabel.FUNCTION, f"{project}.app.helpers.fmt", "app/helpers.py", 1, 9
        ),
        _row(cs.NodeLabel.MODULE, f"{project}.lib.helpers", "lib/helpers.py"),
        _row(
            cs.NodeLabel.FUNCTION, f"{project}.lib.helpers.fmt", "lib/helpers.py", 1, 9
        ),
        # Mirrors a stdlib file, so a coincidental suffix match is possible.
        _row(cs.NodeLabel.MODULE, f"{project}.json.decoder", "json/decoder.py"),
        _row(
            cs.NodeLabel.FUNCTION,
            f"{project}.json.decoder.decode",
            "json/decoder.py",
            1,
            40,
        ),
    ]


def _shop_calls(project: str) -> list[dict]:
    pairs = [
        ("shop.cli.main", "shop.cli.checkout"),
        ("shop.cli.checkout", "shop.pricing.Cart.total"),
        ("shop.pricing.Cart.total", "shop.pricing.apply_tax"),
        ("shop.pricing.apply_tax", "shop.pricing.load_rate"),
    ]
    return [{"from_qn": f"{project}.{a}", "to_qn": f"{project}.{b}"} for a, b in pairs]


def _fetch_all_for(project: str):
    nodes = _shop_nodes(project)
    calls = _shop_calls(project)

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        if query == CYPHER_TRACE_CALLABLES:
            return nodes
        if query == CYPHER_CRASH_CALLS:
            return calls
        if query in (
            CYPHER_FLOW_EDGES,
            CYPHER_FLOW_REMOTE_EDGES,
            CYPHER_FLOW_COVERAGE_GAPS,
            CYPHER_CRASH_POSITIONAL_PARAMS,
        ):
            return []
        raise AssertionError(f"unexpected query: {query}")

    return fetch_all


def _shop_traceback(root: str, sep: str = "/") -> str:
    """The issue's 8-frame traceback with `root` as the checkout root."""
    cli = sep.join((root, "shop", "cli.py"))
    pricing = sep.join((root, "shop", "pricing.py"))
    return (
        "Traceback (most recent call last):\n"
        '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
        '  File "<frozen runpy>", line 88, in _run_code\n'
        f'  File "{cli}", line 27, in <module>\n'
        "    main()\n"
        f'  File "{cli}", line 23, in main\n'
        "    checkout(cart)\n"
        f'  File "{cli}", line 15, in checkout\n'
        "    return cart.total()\n"
        f'  File "{pricing}", line 18, in total\n'
        "    return apply_tax(self.subtotal)\n"
        f'  File "{pricing}", line 10, in apply_tax\n'
        "    return amount * (1 + load_rate())\n"
        f'  File "{pricing}", line 6, in load_rate\n'
        '    return float(os.environ.get("TAX_RATE", ""))\n'
        "ValueError: could not convert string to float: ''\n"
    )


def _resolved(project: str) -> list[str | None]:
    return [
        None,
        None,
        f"{project}.shop.cli",
        f"{project}.shop.cli.main",
        f"{project}.shop.cli.checkout",
        f"{project}.shop.pricing.Cart.total",
        f"{project}.shop.pricing.apply_tax",
        f"{project}.shop.pricing.load_rate",
    ]


def _qns(report) -> list[str | None]:
    return [frame.qualified_name for frame in report.frames]


def _reasons(report) -> list[str | None]:
    return [frame.unresolved_reason for frame in report.frames]


# --- red: tracebacks from another checkout resolve like a local one ---------


@pytest.mark.parametrize(
    ("root", "sep"),
    [
        pytest.param(_CI_ROOT, "/", id="ci-runner"),
        pytest.param(_DOCKER_ROOT, "/", id="container"),
        pytest.param(_WINDOWS_ROOT, "\\", id="windows"),
    ],
)
def test_a_traceback_from_another_checkout_resolves_like_a_local_one(
    tmp_path, root, sep
):
    report = explain_traceback(
        _fetch_all_for(_P), _P, tmp_path, _shop_traceback(root, sep)
    )

    assert _qns(report) == _resolved(_P)
    assert report.resolution.resolved == 6
    assert report.resolution.total == 8


@pytest.mark.parametrize(
    ("root", "sep", "stripped"),
    [
        pytest.param(_CI_ROOT, "/", f"{_CI_ROOT}/", id="ci-runner"),
        pytest.param(_WINDOWS_ROOT, "\\", "C:/Users/dev/tbdemo/", id="windows"),
    ],
)
def test_the_report_names_the_checkout_root_it_inferred(tmp_path, root, sep, stripped):
    """A match made by inference is weaker evidence than one under the
    indexed root, so the caller is told which prefix was stripped."""
    report = explain_traceback(
        _fetch_all_for(_P), _P, tmp_path, _shop_traceback(root, sep)
    )

    assert report.inferred_root == stripped


def test_rank_root_causes_ranks_a_container_traceback(tmp_path):
    report = rank_root_causes(
        _fetch_all_for(_P), _P, tmp_path, _shop_traceback(_DOCKER_ROOT)
    )

    assert report.failing == f"{_P}.shop.pricing.load_rate"
    assert report.anchor_is_crash_site is True
    assert [c.qualified_name for c in report.candidates][:3] == [
        f"{_P}.shop.pricing.apply_tax",
        f"{_P}.shop.pricing.Cart.total",
        f"{_P}.shop.cli.checkout",
    ]


def test_a_path_prefix_map_anchors_what_inference_declines(tmp_path):
    """An installed copy of the package is never matched by inference (see
    the site-packages negative test); the caller can still say it is ours."""
    text = _shop_traceback(_SITE_PACKAGES)

    report = explain_traceback(
        _fetch_all_for(_P), _P, tmp_path, text, path_prefix_map={_SITE_PACKAGES: "."}
    )

    assert _qns(report) == _resolved(_P)
    assert report.inferred_root is None


def test_a_path_prefix_map_can_point_into_a_subdirectory(tmp_path):
    """A src-layout repo installed as a package: `site-packages/shop/...`
    is `src/shop/...` in the checkout."""
    nodes = [
        {**row, cs.KEY_PATH: f"src/{row[cs.KEY_PATH]}"}
        for row in _shop_nodes(_P)
        if row[cs.KEY_PATH].startswith("shop/")
    ]

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        return nodes if query == CYPHER_TRACE_CALLABLES else []

    report = explain_traceback(
        fetch_all,
        _P,
        tmp_path,
        _shop_traceback(_SITE_PACKAGES),
        path_prefix_map={_SITE_PACKAGES: "src"},
    )

    assert _qns(report) == _resolved(_P)


def test_a_windows_path_prefix_map_matches_without_case(tmp_path):
    """Windows paths name the same file whatever their case, and a drive
    letter is written either way, so a root typed as `c:\\users\\dev` still
    anchors frames recorded under `C:\\Users\\dev`."""
    report = explain_traceback(
        _fetch_all_for(_P),
        _P,
        tmp_path,
        _shop_traceback(_WINDOWS_ROOT, "\\"),
        path_prefix_map={_WINDOWS_ROOT.lower(): "."},
    )

    assert _qns(report) == _resolved(_P)
    assert report.inferred_root is None


def test_rank_root_causes_says_why_nothing_resolved(tmp_path):
    """An empty ranking with no explanation reads as "the graph does not
    know this code"; the report must say the paths did not match instead."""
    text = (
        "Traceback (most recent call last):\n"
        '  File "/srv/billing/billing/api.py", line 5, in run\n'
        '  File "/srv/billing/billing/core.py", line 9, in step\n'
        "RuntimeError: nope\n"
    )

    report = rank_root_causes(_fetch_all_for(_P), _P, tmp_path, text)

    assert report.failing is None
    assert report.resolution.total == 2
    assert report.resolution.resolved == 0
    assert report.note is not None
    assert "0 of 2" in report.note
    assert tmp_path.resolve().as_posix() in report.note
    assert cs.MCPParamName.PATH_PREFIX_MAP in report.note


# --- negative: what must not change or must stay unresolved ----------------


def test_a_local_traceback_resolves_exactly_as_before(tmp_path):
    report = explain_traceback(
        _fetch_all_for(_P), _P, tmp_path, _shop_traceback(tmp_path.resolve().as_posix())
    )

    assert _qns(report) == _resolved(_P)
    assert _reasons(report) == [_OUTSIDE, _OUTSIDE, None, None, None, None, None, None]
    assert report.inferred_root is None
    assert report.note is None


def test_a_local_traceback_does_not_adopt_a_stdlib_frame_that_mirrors_a_repo_file(
    tmp_path,
):
    """`json/decoder.py` with a `decode` is indexed, so the stdlib frame
    suffix-matches. A traceback that already has frames under the indexed
    root came from this checkout; anything else in it is genuinely outside."""
    local = (tmp_path.resolve() / "shop" / "pricing.py").as_posix()
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{local}", line 6, in load_rate\n'
        '  File "/usr/lib/python3.12/json/decoder.py", line 20, in decode\n'
        "ValueError: boom\n"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [f"{_P}.shop.pricing.load_rate", None]
    assert _reasons(report) == [None, _OUTSIDE]


def test_stdlib_and_site_packages_frames_of_a_foreign_traceback_stay_outside(
    tmp_path,
):
    """The site-packages copy of `shop/pricing.py` even names `load_rate`,
    so a bare suffix match would bind it; installed code is not the checkout."""
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{_DOCKER_ROOT}/shop/cli.py", line 23, in main\n'
        '  File "/usr/local/lib/python3.12/json/__init__.py", line 3, in loads\n'
        f'  File "{_SITE_PACKAGES}/shop/pricing.py", line 6, in load_rate\n'
        '  File "/app/.venv/lib/python3.12/site-packages/shop/pricing.py", line 6, '
        "in load_rate\n"
        "ValueError: boom\n"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [f"{_P}.shop.cli.main", None, None, None]
    assert _reasons(report) == [None, _OUTSIDE, _OUTSIDE, _UNKNOWN]


def test_a_site_packages_only_traceback_is_not_matched_by_inference(tmp_path):
    report = explain_traceback(
        _fetch_all_for(_P), _P, tmp_path, _shop_traceback(_SITE_PACKAGES)
    )

    assert report.resolution.resolved == 0
    assert report.inferred_root is None


def test_a_different_projects_same_named_file_is_not_bound_to_a_wrong_function(
    tmp_path,
):
    """`utils.py` is indexed and `slugify` spans line 12, so line containment
    alone would pick it; the frame is `parse_config` from another project."""
    text = (
        "Traceback (most recent call last):\n"
        '  File "/home/dev/other/utils.py", line 12, in parse_config\n'
        "KeyError: 'x'\n"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [None]
    assert _reasons(report) == [_OUTSIDE]
    assert report.inferred_root is None


def test_a_shared_basename_alone_never_matches(tmp_path):
    """`helpers.py` exists under `app/` and `lib/`; a frame whose path has
    neither directory must not be attributed to either."""
    text = (
        "Traceback (most recent call last):\n"
        '  File "/srv/proj/helpers.py", line 3, in fmt\n'
        "KeyError: 'x'\n"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [None]
    assert _reasons(report) == [_OUTSIDE]


def test_frames_disagreeing_on_the_checkout_root_stay_unresolved(tmp_path):
    """Two unrelated roots with equal support cannot be told apart, so
    neither is picked."""
    text = (
        "Traceback (most recent call last):\n"
        '  File "/srv/a/shop/cli.py", line 23, in main\n'
        '  File "/opt/b/shop/pricing.py", line 6, in load_rate\n'
        "ValueError: boom\n"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [None, None]
    assert _reasons(report) == [_OUTSIDE, _OUTSIDE]
    assert report.inferred_root is None


def test_a_frame_off_the_inferred_root_stays_outside(tmp_path):
    """The majority root wins, and only frames under it are rebased: a lone
    frame matching under a different root is not."""
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{_CI_ROOT}/shop/cli.py", line 23, in main\n'
        f'  File "{_CI_ROOT}/shop/cli.py", line 15, in checkout\n'
        '  File "/opt/b/shop/pricing.py", line 6, in load_rate\n'
        "ValueError: boom\n"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [f"{_P}.shop.cli.main", f"{_P}.shop.cli.checkout", None]
    assert _reasons(report)[2] == _OUTSIDE


def test_a_file_the_graph_lacks_under_the_inferred_root_is_unknown_not_guessed(
    tmp_path,
):
    """Under the inferred root a frame is judged exactly as a local one: an
    unindexed file is `unknown_path`, the same answer as for the checkout."""
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{_DOCKER_ROOT}/shop/cli.py", line 23, in main\n'
        f'  File "{_DOCKER_ROOT}/shop/new_module.py", line 6, in load_rate\n'
        "ValueError: boom\n"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [f"{_P}.shop.cli.main", None]
    assert _reasons(report) == [None, _UNKNOWN]


def test_a_path_prefix_map_cannot_escape_the_repository(tmp_path):
    text = _shop_traceback(_DOCKER_ROOT)

    report = explain_traceback(
        _fetch_all_for(_P), _P, tmp_path, text, path_prefix_map={_DOCKER_ROOT: ".."}
    )

    assert report.resolution.resolved == 0


def test_no_note_when_some_frame_resolved(tmp_path):
    report = rank_root_causes(
        _fetch_all_for(_P), _P, tmp_path, _shop_traceback(_CI_ROOT)
    )

    assert report.note is None
    assert report.resolution.resolved == 6


# --- MCP surface ------------------------------------------------------------


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _registry(tmp_path: Path) -> tuple[MCPToolsRegistry, str]:
    project = derive_project_name(tmp_path)
    ingestor = MagicMock()
    ingestor.fetch_all = _fetch_all_for(project)
    registry = MCPToolsRegistry(
        project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
    )
    return registry, project


@pytest.mark.anyio
async def test_mcp_tools_declare_an_optional_path_prefix_map(tmp_path):
    registry, _ = _registry(tmp_path)
    for name in (cs.MCPToolName.EXPLAIN_TRACEBACK, cs.MCPToolName.RANK_ROOT_CAUSES):
        schema = registry._tools[name].input_schema
        prop = schema["properties"][cs.MCPParamName.PATH_PREFIX_MAP]
        assert prop["type"] == cs.MCPSchemaType.OBJECT
        assert prop["additionalProperties"] == {"type": cs.MCPSchemaType.STRING}
        assert schema["required"] == [cs.MCPParamName.TRACEBACK_TEXT]


@pytest.mark.anyio
async def test_mcp_explain_traceback_resolves_a_ci_traceback(tmp_path):
    registry, project = _registry(tmp_path)

    result = await registry.explain_traceback(traceback_text=_shop_traceback(_CI_ROOT))

    assert [f["qualified_name"] for f in result["frames"]] == _resolved(project)
    assert result["resolution"]["resolved"] == 6
    assert result["inferred_checkout_root"] == f"{_CI_ROOT}/"
    assert result["note"] is None


@pytest.mark.anyio
async def test_mcp_explain_traceback_takes_a_path_prefix_map(tmp_path):
    registry, project = _registry(tmp_path)

    result = await registry.explain_traceback(
        traceback_text=_shop_traceback(_SITE_PACKAGES),
        path_prefix_map={_SITE_PACKAGES: "."},
    )

    assert [f["qualified_name"] for f in result["frames"]] == _resolved(project)


@pytest.mark.anyio
async def test_mcp_rank_root_causes_ranks_a_windows_traceback(tmp_path):
    registry, project = _registry(tmp_path)

    result = await registry.rank_root_causes(
        traceback_text=_shop_traceback(_WINDOWS_ROOT, "\\")
    )

    assert result["failing"] == f"{project}.shop.pricing.load_rate"
    assert result["candidates"]
    assert result["resolution"] == {"total": 8, "resolved": 6, "rate": 0.75}


@pytest.mark.anyio
async def test_mcp_rank_root_causes_explains_an_empty_ranking(tmp_path):
    registry, _ = _registry(tmp_path)

    result = await registry.rank_root_causes(
        traceback_text=_shop_traceback(_SITE_PACKAGES)
    )

    assert result["failing"] is None
    assert result["candidates"] == []
    assert result["resolution"] == {"total": 8, "resolved": 0, "rate": 0.0}
    assert "0 of 8" in result["note"]
    assert cs.MCPParamName.PATH_PREFIX_MAP in result["note"]


# --- `cgr trace ingest`: a trace recorded under another checkout root -------


class _IngestGraph:
    def __init__(self, project: str) -> None:
        self._prefix = f"{project}."
        self._rows = [
            row for row in _shop_nodes(project) if row[cs.KEY_PATH].startswith("shop/")
        ]
        self.edges: list[tuple[str, str]] = []

    def fetch_all(self, query, params=None):
        assert params == {cs.KEY_PREFIX: self._prefix}
        if query == CYPHER_TRACE_CALLABLES:
            return self._rows
        if query == CYPHER_TRACE_EXISTING_CALLS:
            return []
        raise AssertionError(f"unexpected query: {query}")

    def execute_write(self, query, params=None):
        raise AssertionError("no static edge exists to confirm")

    def ensure_relationship_batch(self, from_spec, rel_type, to_spec, properties=None):
        self.edges.append((from_spec[2], to_spec[2]))

    def flush_all(self):
        pass


def _write_trace(
    trace_path: Path, recorded_root: str, frames_root: str, package: str = "shop"
) -> None:
    def point(rel: str, name: str, line: int) -> FramePoint:
        rel = rel.replace("shop/", f"{package}/", 1)
        return FramePoint(path=f"{frames_root}/{rel}", qualname=name, line=line)

    write_trace_file(
        trace_path,
        TraceHeader(
            version=cs.TRACE_FORMAT_VERSION,
            language=cs.TRACE_LANGUAGE_PYTHON,
            repo_root=recorded_root,
            tracer=cs.TRACE_TOOL_NAME,
        ),
        [
            CallRecord(
                caller=point("shop/pricing.py", "Cart.total", 17),
                callee=point("shop/pricing.py", "apply_tax", 9),
                count=2,
                workloads=("t::ci",),
                receiver_types=(),
            ),
            CallRecord(
                caller=point("shop/pricing.py", "apply_tax", 9),
                callee=point("shop/pricing.py", "load_rate", 5),
                count=2,
                workloads=("t::ci",),
                receiver_types=(),
            ),
        ],
    )


def test_trace_ingest_resolves_a_trace_recorded_under_another_root(tmp_path):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(trace_path, _CI_ROOT, _CI_ROOT)
    graph = _IngestGraph(_P)

    summary = ingest_trace(trace_path, graph, repo, _P)

    assert summary.unresolved == 0
    assert summary.edges == 2
    assert sorted(graph.edges) == [
        (f"{_P}.shop.pricing.Cart.total", f"{_P}.shop.pricing.apply_tax"),
        (f"{_P}.shop.pricing.apply_tax", f"{_P}.shop.pricing.load_rate"),
    ]


def test_trace_ingest_leaves_frames_outside_the_recorded_root_outside(tmp_path):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(trace_path, _CI_ROOT, "/opt/elsewhere")
    graph = _IngestGraph(_P)

    summary = ingest_trace(trace_path, graph, repo, _P)

    assert summary.edges == 0
    assert summary.resolution.unresolved == {_OUTSIDE: 2}


def test_trace_ingest_of_a_local_trace_is_unchanged(tmp_path):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(trace_path, repo.as_posix(), repo.as_posix())
    graph = _IngestGraph(_P)

    summary = ingest_trace(trace_path, graph, repo, _P)

    assert summary.unresolved == 0
    assert summary.edges == 2


def test_trace_ingest_does_not_rebase_local_frames_under_a_recorded_ancestor(
    tmp_path,
):
    """A header root that is an ANCESTOR of the checkout (a tracer run with
    the home directory as its root) must not re-root frames already under
    the checkout: `<home>/repo/shop/x.py` would become `<repo>/repo/shop/x.py`."""
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(trace_path, tmp_path.resolve().as_posix(), repo.as_posix())
    graph = _IngestGraph(_P)

    summary = ingest_trace(trace_path, graph, repo, _P)

    assert summary.unresolved == 0
    assert summary.edges == 2


def test_trace_ingest_matches_a_windows_recorded_root_without_case(tmp_path):
    """The header root and `co_filename` can disagree on a Windows path's
    case (a lowercase drive letter on `sys.path`); both name one file."""
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(trace_path, _WINDOWS_ROOT, "c:\\users\\Dev\\tbdemo")
    graph = _IngestGraph(_P)

    summary = ingest_trace(trace_path, graph, repo, _P)

    assert summary.unresolved == 0
    assert summary.edges == 2


def test_trace_ingest_keeps_posix_recorded_roots_case_sensitive(tmp_path):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(trace_path, _CI_ROOT.upper(), _CI_ROOT)
    graph = _IngestGraph(_P)

    summary = ingest_trace(trace_path, graph, repo, _P)

    assert summary.edges == 0
    assert summary.resolution.unresolved == {_OUTSIDE: 2}


def test_a_frame_under_the_checkout_in_another_case_stays_under_it():
    """On Windows the checkout root itself compares without case, so a frame
    under it is re-cased onto it, never moved by an ancestor's rule."""
    rebase = PathRebase(
        local_root="C:/Users/dev/tbdemo/",
        rules=(("C:/Users/", "C:/Users/dev/tbdemo/elsewhere/"),),
    )
    frame = FramePoint(
        path="c:\\users\\DEV\\tbdemo\\shop\\pricing.py",
        qualname="apply_tax",
        line=9,
    )

    assert rebase.apply(frame).path == "C:/Users/dev/tbdemo/shop/pricing.py"
    assert not rebase.matches(frame)


# --- a Windows frame spelling an in-repo directory in another case ----------
# Windows names one file whatever the case of its path, so a traceback or
# trace recorded there can spell `shop\pricing.py` as `SHOP\pricing.py`
# (the script typed on the command line, a tool that normalises case). The
# rebase kept that recorded spelling, so the frame matched no graph node: the
# trace lost its call edge and the traceback lost the frame (follow-up to
# issue #2587).

_WINDOWS_LOCAL_ROOT = "C:/Users/dev/tbdemo/"


def _recased(text: str, package: str, *files: str) -> str:
    for name in files:
        text = text.replace(f"\\shop\\{name}", f"\\{package}\\{name}")
    return text


def _ambiguous_fetch_all(project: str):
    """A POSIX checkout may hold two files whose paths differ only in case."""
    nodes = [
        _row(cs.NodeLabel.MODULE, f"{project}.shop.cli", "shop/cli.py"),
        _row(cs.NodeLabel.FUNCTION, f"{project}.shop.cli.main", "shop/cli.py", 20, 24),
        _row(cs.NodeLabel.MODULE, f"{project}.shop.util", "shop/util.py"),
        _row(cs.NodeLabel.FUNCTION, f"{project}.shop.util.fmt", "shop/util.py", 1, 9),
        _row(cs.NodeLabel.MODULE, f"{project}.Shop.util", "Shop/util.py"),
        _row(cs.NodeLabel.FUNCTION, f"{project}.Shop.util.fmt", "Shop/util.py", 1, 9),
    ]

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        return nodes if query == CYPHER_TRACE_CALLABLES else []

    return fetch_all


def test_a_windows_frame_in_a_differently_cased_directory_resolves(tmp_path):
    """The reported case: `cli.py` frames name `shop`, `pricing.py` frames
    name `SHOP`. Both are this checkout's `shop/` package."""
    text = _recased(_shop_traceback(_WINDOWS_ROOT, "\\"), "SHOP", "pricing.py")

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == _resolved(_P)
    assert report.resolution.resolved == 6


def test_a_windows_traceback_wholly_under_a_differently_cased_directory_resolves(
    tmp_path,
):
    """With no frame spelled as indexed, the checkout root is still inferred."""
    text = _recased(
        _shop_traceback(_WINDOWS_ROOT, "\\"), "Shop", "cli.py", "pricing.py"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == _resolved(_P)
    assert report.inferred_root == _WINDOWS_LOCAL_ROOT


def test_a_frame_under_a_windows_checkout_in_another_case_resolves(
    tmp_path, monkeypatch
):
    """The checkout itself on Windows: no rebase moves the frame, yet its
    in-repo directory is spelled differently from the indexed path. The
    frames use forward slashes, the form `Path.as_posix` gives on Windows,
    so the simulated checkout behaves alike on every host."""
    monkeypatch.setattr(
        resolution, "_repo_root_posix", lambda _root: _WINDOWS_LOCAL_ROOT
    )
    text = _recased(
        _shop_traceback(_WINDOWS_ROOT, "\\"), "SHOP", "cli.py", "pricing.py"
    ).replace("\\", "/")

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == _resolved(_P)
    assert report.inferred_root is None


def test_a_windows_frame_mapped_into_a_subdirectory_is_respelled_below_it(
    tmp_path,
):
    """Only the recorded part is re-spelled; the mapped target `src` is this
    checkout's own directory and is used as given."""
    nodes = [
        {**row, cs.KEY_PATH: f"src/{row[cs.KEY_PATH]}"}
        for row in _shop_nodes(_P)
        if row[cs.KEY_PATH].startswith("shop/")
    ]

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        return nodes if query == CYPHER_TRACE_CALLABLES else []

    report = explain_traceback(
        fetch_all,
        _P,
        tmp_path,
        _recased(_shop_traceback("D:\\build", "\\"), "SHOP", "pricing.py"),
        path_prefix_map={"D:\\build": "src"},
    )

    assert _qns(report) == _resolved(_P)


def test_trace_ingest_matches_a_windows_frame_whose_directory_case_differs(
    tmp_path,
):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(trace_path, _WINDOWS_ROOT, _WINDOWS_ROOT, package="SHOP")
    graph = _IngestGraph(_P)

    summary = ingest_trace(trace_path, graph, repo, _P)

    assert summary.unresolved == 0
    assert sorted(graph.edges) == [
        (f"{_P}.shop.pricing.Cart.total", f"{_P}.shop.pricing.apply_tax"),
        (f"{_P}.shop.pricing.apply_tax", f"{_P}.shop.pricing.load_rate"),
    ]


# --- negative: case still matters where the recording OS says it does -------


def test_a_windows_frame_spelled_as_indexed_resolves_as_before(tmp_path):
    report = explain_traceback(
        _fetch_all_for(_P), _P, tmp_path, _shop_traceback(_WINDOWS_ROOT, "\\")
    )

    assert _qns(report) == _resolved(_P)
    assert report.inferred_root == _WINDOWS_LOCAL_ROOT


def test_a_posix_frame_in_a_differently_cased_directory_stays_unknown(tmp_path):
    """On a POSIX machine `SHOP/` and `shop/` are two directories, so the
    frame names a file the graph does not hold."""
    text = _shop_traceback(_DOCKER_ROOT).replace("/shop/pricing.py", "/SHOP/pricing.py")

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [*_resolved(_P)[:5], None, None, None]
    assert _reasons(report)[5:] == [_UNKNOWN, _UNKNOWN, _UNKNOWN]


def test_trace_ingest_keeps_posix_in_repo_directories_case_sensitive(tmp_path):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(trace_path, _CI_ROOT, _CI_ROOT, package="SHOP")
    graph = _IngestGraph(_P)

    summary = ingest_trace(trace_path, graph, repo, _P)

    assert summary.edges == 0
    assert summary.resolution.unresolved == {_UNKNOWN: 2}


def test_a_windows_frame_matching_two_indexed_spellings_picks_neither(tmp_path):
    """`shop/util.py` and `Shop/util.py` are both indexed; `SHOP\\util.py`
    names one file on Windows but cannot say which of the two it is."""
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\work\\shop\\cli.py", line 23, in main\n'
        '  File "C:\\work\\SHOP\\util.py", line 3, in fmt\n'
        '  File "C:\\work\\Shop\\util.py", line 3, in fmt\n'
        "ValueError: boom\n"
    )

    report = explain_traceback(_ambiguous_fetch_all(_P), _P, tmp_path, text)

    assert _qns(report) == [f"{_P}.shop.cli.main", None, f"{_P}.Shop.util.fmt"]
    assert _reasons(report)[1] == _UNKNOWN


def test_a_windows_frame_in_an_unindexed_directory_stays_unknown(tmp_path):
    text = _shop_traceback(_WINDOWS_ROOT, "\\").replace(
        "\\shop\\pricing.py", "\\shop2\\pricing.py"
    )

    report = explain_traceback(_fetch_all_for(_P), _P, tmp_path, text)

    assert _qns(report) == [*_resolved(_P)[:5], None, None, None]
    assert _reasons(report)[5:] == [_UNKNOWN, _UNKNOWN, _UNKNOWN]
