# The change_signature operation itself (issue #1533): planning against a
# real index of a small fixture repo, staging into a transaction, previewing
# and applying, and the sites it leaves unmapped rather than guess at. The
# graph is the in-memory stateful ingestor.

from __future__ import annotations

from pathlib import Path

import pytest
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from codebase_rag import constants as cs
from codebase_rag.editing.patcher import Patcher
from codebase_rag.editing.signature import (
    SignatureChanger,
    _binds_its_receiver,
    _passes_receiver,
    change_signature,
)
from codebase_rag.editing.signature_spec import SignatureRefused, _Edit, _Header
from codebase_rag.graph_query import CallSiteRow
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "sigchange"
UTIL = "pkg/util.py"
APP = "pkg/app.py"
HELPER = f"{PROJECT}.pkg.util.helper"
FILES: dict[str, str] = {
    "pkg/__init__.py": "",
    UTIL: "def helper(a: int, b: str = 'x') -> str:\n    return b * a\n",
    APP: (
        "from pkg.util import helper\n\n\n"
        "def run():\n    return helper(2)\n\n\n"
        "def run_both():\n    return helper(3, 'z')\n"
    ),
}
SHAPES = "pkg/shapes.py"
SHAPES_SOURCE = (
    "class Base:\n"
    "    def f(self, a, b=0):\n"
    "        return a - b\n\n"
    "    @classmethod\n"
    "    def make(cls, a, b=0):\n"
    "        return a - b\n\n\n"
    "def through_class(obj):\n    return Base.f(obj, 5)\n\n\n"
    "def through_type(obj):\n    return type(obj).f(obj, 5)\n\n\n"
    "def through_instance():\n    return Base().f(5)\n\n\n"
    "def classmethod_through_class():\n    return Base.make(5)\n"
)
BASE_F = f"{PROJECT}.pkg.shapes.Base.f"
BASE_MAKE = f"{PROJECT}.pkg.shapes.Base.make"


def _indexed(
    temp_repo: Path, files: dict[str, str]
) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return root, store, updater


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    return _indexed(temp_repo, FILES)


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


# --- apply, preview and plan -----------------------------------------------------


def test_apply_rewrites_definition_and_sites_under_the_contract(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.transaction_id
    assert set(report.files) == {UTIL, APP}
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    assert "def helper(a: int, n: int, b: str = 'x') -> str:" in _read(root, UTIL)
    app = _read(root, APP)
    assert "return helper(2, 1)" in app
    assert "return helper(3, 1, 'z')" in app
    assert [s.kind for s in report.sites] == ["definition", "call", "call"]


def test_apply_without_reingest_skips_the_contract(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    report = change_signature(
        root, store.fetch_all, PROJECT, HELPER, ["a", "b", "c=None"]
    )
    assert report.applied, report.message
    assert report.verdict is None
    assert "def helper(a: int, b: str = 'x', c=None) -> str:" in _read(root, UTIL)
    # A new defaulted parameter changes no call site.
    assert report.files == (UTIL,)


def test_dry_run_returns_the_diff_and_writes_nothing(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    before = {rel: _read(root, rel) for rel in (UTIL, APP)}
    report = change_signature(
        root, store.fetch_all, PROJECT, HELPER, ["b: str", "a: int"], dry_run=True
    )
    assert not report.applied
    assert report.files == (APP, UTIL)
    assert "+def helper(b: str, a: int) -> str:" in report.diff
    assert "-    return helper(3, 'z')" in report.diff
    assert "+    return helper('z', 3)" in report.diff
    # `run` relied on the default `b` no longer has: left as written.
    assert [(site.owner, site.reason) for site in report.unmapped] == [
        (f"{PROJECT}.pkg.app.run", cs.SIGNATURE_SITE_NO_VALUE.format(name="b"))
    ]
    assert {rel: _read(root, rel) for rel in (UTIL, APP)} == before


def test_plan_lists_the_edits_without_staging(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    changer = SignatureChanger(root, store.fetch_all, PROJECT)
    report, edits = changer.plan(HELPER, ["a", "text: str = 'x'"], {"text": "b"}, False)
    assert report.old_params == ("a", "b")
    assert report.new_params == ("a", "text")
    assert report.message == cs.SIGNATURE_PLANNED.format(count=2, skipped=0)
    # Both sites pass `b` positionally, so only the definition changes: its
    # header and the body's read of the renamed parameter.
    assert sorted(edit.text for edit in edits) == [
        "(a: int, text: str = 'x')",
        "text",
    ]
    assert {edit.path for edit in edits} == {UTIL}
    assert _read(root, UTIL) == FILES[UTIL]


def test_a_rewrite_that_no_longer_parses_is_rolled_back(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    args = (root, store.fetch_all, PROJECT, HELPER, ["a", "n", "b"], {"n": "=)"})
    preview = change_signature(*args, dry_run=True)
    assert preview.message == cs.SIGNATURE_PARSE_FAILED.format(files=APP)
    report = change_signature(*args)
    assert not report.applied
    assert report.message == cs.SIGNATURE_PARSE_FAILED.format(files=APP)
    assert _read(root, APP) == FILES[APP]
    assert _read(root, UTIL) == FILES[UTIL]


# --- refusals ----------------------------------------------------------------------


def test_unknown_definition_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    qn = f"{PROJECT}.pkg.util.nothing"
    with pytest.raises(SignatureRefused, match="No definition named"):
        change_signature(root, store.fetch_all, PROJECT, qn, ["a"])


def test_non_python_definition_is_refused(temp_repo: Path) -> None:
    root, store, _updater = _indexed(
        temp_repo, {"web/lib.js": "function helper(a, b) {\n  return a + b;\n}\n"}
    )
    with pytest.raises(SignatureRefused, match="is not Python"):
        change_signature(
            root, store.fetch_all, PROJECT, f"{PROJECT}.web.lib.helper", ["b", "a"]
        )


def test_definition_file_gone_since_indexing_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    (root / UTIL).unlink()
    with pytest.raises(SignatureRefused, match="cannot be read"):
        change_signature(root, store.fetch_all, PROJECT, HELPER, ["a"])


def test_definition_moved_since_indexing_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    (root / UTIL).write_text("\n\n\n\n" + FILES[UTIL], encoding="utf-8")
    with pytest.raises(SignatureRefused, match="Could not locate the definition"):
        change_signature(root, store.fetch_all, PROJECT, HELPER, ["a"])


def test_dropping_a_parameter_the_body_reads_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match="Cannot drop parameter b"):
        change_signature(root, store.fetch_all, PROJECT, HELPER, ["a"])


def test_literal_of_the_wrong_type_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match="does not fit the declared type"):
        change_signature(
            root, store.fetch_all, PROJECT, HELPER, ["a", "n: int", "b"], {"n": "='x'"}
        )


def test_edit_the_patcher_cannot_apply_is_a_refusal(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    changer = SignatureChanger(root, store.fetch_all, PROJECT)
    planned = Patcher(root)
    edits = [_Edit(UTIL, (0, 10_000), "x")]
    with pytest.raises(SignatureRefused, match="Cannot stage the signature change"):
        changer._stage(edits, planned)


# --- a file changed between planning and staging (CodeRabbit, #2166) ---------


def test_file_changed_after_planning_is_refused_not_spliced(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The edits carry byte offsets measured in the planned bytes. Splicing
    # them into a re-read file that gained a line would cut the wrong text.
    root, store, _updater = repo
    changed = "# a line added after planning\n" + FILES[APP]
    original = SignatureChanger._stage

    def stage_after_an_edit(self: SignatureChanger, *args: object) -> object:
        (root / APP).write_text(changed, encoding="utf-8")
        return original(self, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(SignatureChanger, "_stage", stage_after_an_edit)
    with pytest.raises(SignatureRefused, match="changed on disk after"):
        change_signature(
            root, store.fetch_all, PROJECT, HELPER, ["a", "n", "b"], {"n": "=1"}
        )
    assert _read(root, APP) == changed
    assert _read(root, UTIL) == FILES[UTIL]


# --- the explicit receiver (CodeRabbit, #2166) ---------------------------------


def test_a_call_passing_the_receiver_explicitly_is_unmapped(
    temp_repo: Path,
) -> None:
    # `Base.f(obj, 5)` binds `obj` to `self`; reading it as the value of `a`
    # would write `Base.f(obj, 9, 5)`, shifting 5 into `b`.
    root, store, _updater = _indexed(
        temp_repo, {"pkg/__init__.py": "", SHAPES: SHAPES_SOURCE}
    )
    report = change_signature(
        root, store.fetch_all, PROJECT, BASE_F, ["a", "n", "b"], {"n": "=9"}
    )
    assert report.applied, report.message
    shapes = _read(root, SHAPES)
    assert "def f(self, a, n, b=0):" in shapes
    assert "return Base().f(5, 9)" in shapes
    assert "return Base.f(obj, 5)" in shapes
    assert "return type(obj).f(obj, 5)" in shapes
    reasons = {site.owner.rsplit(".", 1)[-1]: site.reason for site in report.unmapped}
    assert set(reasons) <= {"through_class", "through_type"}
    assert "through_class" in reasons
    assert reasons["through_class"] == cs.SIGNATURE_SITE_EXPLICIT_RECEIVER.format(
        text="Base.f(obj, 5)"
    )


def test_a_classmethod_through_its_class_is_rewritten(temp_repo: Path) -> None:
    root, store, _updater = _indexed(
        temp_repo, {"pkg/__init__.py": "", SHAPES: SHAPES_SOURCE}
    )
    report = change_signature(
        root, store.fetch_all, PROJECT, BASE_MAKE, ["a", "n", "b"], {"n": "=9"}
    )
    assert report.applied, report.message
    assert report.unmapped == ()
    shapes = _read(root, SHAPES)
    assert "def make(cls, a, n, b=0):" in shapes
    assert "return Base.make(5, 9)" in shapes


# --- sites the operation cannot read ------------------------------------------------


def _row(**overrides: object) -> CallSiteRow:
    row: dict[str, object] = {
        "label": "Function",
        "qualified_name": f"{PROJECT}.pkg.app.run",
        "path": APP,
        "line": 5,
        "col": 11,
        "end_line": 5,
        "end_col": 20,
        "arg_count": 1,
        "kwarg_names": [],
        "resolution": cs.EdgeResolution.EXACT.value,
        "depth": 1,
        "through": HELPER,
    }
    row.update(overrides)
    return row  # type: ignore[return-value]


def _function_header(changer: SignatureChanger, patcher: Patcher) -> _Header:
    return changer._header(HELPER, patcher)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"line": None, "resolution": None}, "site carries no location (dynamic)"),
        ({"resolution": "heuristic"}, "site was resolved by heuristic"),
        ({"path": "web/app.js"}, "web/app.js is not Python"),
        ({"path": "pkg/gone.py"}, "file cannot be read"),
        ({"line": 4, "col": 0}, "no call found at the recorded position"),
        ({"end_col": 99}, "no call found at the recorded position"),
    ],
)
def test_site_the_operation_cannot_read_is_unmapped(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
    overrides: dict[str, object],
    reason: str,
) -> None:
    root, store, _updater = repo
    changer = SignatureChanger(root, store.fetch_all, PROJECT)
    patcher = Patcher(root)
    header = _function_header(changer, patcher)
    candidates: list = []
    unmapped: list = []
    changer._consider(
        _row(**overrides), header, header.params, False, patcher, candidates, unmapped
    )
    assert candidates == []
    assert len(unmapped) == 1
    assert unmapped[0].reason.startswith(reason), unmapped[0].reason
    assert unmapped[0].owner == f"{PROJECT}.pkg.app.run"


def test_generator_argument_is_unmapped(temp_repo: Path) -> None:
    root, store, _updater = _indexed(
        temp_repo,
        {
            "pkg/__init__.py": "",
            UTIL: "def helper(xs):\n    return list(xs)\n",
            APP: (
                "from pkg.util import helper\n\n\n"
                "def run():\n    return helper(x for x in range(3))\n"
            ),
        },
    )
    report = change_signature(
        root, store.fetch_all, PROJECT, HELPER, ["xs", "n=0"], dry_run=True
    )
    assert [site.reason.split("`")[0] for site in report.unmapped] == [
        "arguments cannot be read positionally: "
    ]


# --- telling a class-qualified call from an instance call ------------------------

_PY = Parser(Language(tspython.language()))


@pytest.mark.parametrize(
    ("expression", "method_qn", "explicit"),
    [
        ("Base.f(obj, 5)", "p.m.Base.f", True),
        ("pkg.Sub.f(obj, 5)", "p.m.Base.f", True),
        ("cls.f(obj, 5)", "p.m.Base.f", True),
        ("type(obj).f(obj, 5)", "p.m.Base.f", True),
        ("obj.__class__.f(obj, 5)", "p.m.Base.f", True),
        ("widget.f(obj, 5)", "p.m.widget.f", True),
        ("obj.f(5)", "p.m.Base.f", False),
        ("self.helper.f(5)", "p.m.Base.f", False),
        ("LOGGER.f(5)", "p.m.Base.f", False),
        ("Base().f(5)", "p.m.Base.f", False),
        ("super().f(5)", "p.m.Base.f", False),
        ("items[0].f(5)", "p.m.Base.f", False),
        ("f(5)", "f", False),
    ],
)
def test_passes_receiver_reads_the_callee_object(
    expression: str, method_qn: str, explicit: bool
) -> None:
    source = expression.encode()
    root = _PY.parse(source).root_node
    call = root.named_children[0].named_children[0]
    assert call.type == "call"
    assert _passes_receiver(call, source, method_qn) is explicit


@pytest.mark.parametrize(
    ("receiver", "binds"),
    [(None, False), ("self", True), ("self: 'Base'", True), ("cls", False)],
)
def test_only_an_instance_method_binds_its_receiver(
    receiver: str | None, binds: bool
) -> None:
    header = _Header("q", "m.py", (0, 0), 1, 0, receiver, [], None, b"")  # type: ignore[arg-type]
    assert _binds_its_receiver(header) is binds
