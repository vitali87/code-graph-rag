"""Issue #2927: `reingest()` starts from empty Python type-inference caches.

`run()` cleared the Python engine's memoised return types, return statements
and self-assignments; `reingest()`, the path behind every MCP edit tool, the
watcher and `cgr rename`, did not. On the long-lived updater those callers
hold, a dependent re-resolved after an edit read the edited function's OLD
return type (wrong CALLS edge), and the Node-keyed caches kept every
re-parsed tree alive (about 76 MB per edit on fastapi). The class member
type cache was cleared by neither path.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "shop"

MODELS = """\
class Foo:
    def ping(self):
        return "foo"


class Bar:
    def ping(self):
        return "bar"


def make():
    return Foo()


class Holder:
    def __init__(self):
        self.box = Foo()
"""

SERVICE = """\
from app.models import make


def use():
    x = make()
    return x.ping()
"""


def _project(
    root: Path, models: str = MODELS
) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in {
        "app/__init__.py": "",
        "app/models.py": models,
        "app/service.py": SERVICE,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
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
    return store, updater


def _callees(store: _StatefulIngestor, caller: str) -> set[str]:
    prefix = f"{PROJECT}."
    return {
        str(dst).removeprefix(prefix)
        for _src_label, src, rel, _dst_label, dst, _site in store.keyed_edges
        if rel == cs.RelationshipType.CALLS and src == f"{prefix}{caller}"
    }


def _edit(root: Path, old: str, new: str) -> Path:
    path = root / "app/models.py"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    return path


def test_a_dependent_follows_the_edited_return_type(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    store, updater = _project(root)
    assert "app.models.Foo.ping" in _callees(store, "app.service.use")

    updater.reingest([_edit(root, "    return Foo()\n", "    return Bar()\n")])

    callees = _callees(store, "app.service.use")
    assert "app.models.Bar.ping" in callees
    assert "app.models.Foo.ping" not in callees


def test_a_class_member_type_follows_the_edit(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    _store, updater = _project(root)
    engine = updater.factory.type_inference._python_type_inference
    assert engine is not None
    holder = f"{PROJECT}.app.models.Holder"
    assert engine._class_member_types_by_qn(holder) == {
        "box": f"{PROJECT}.app.models.Foo"
    }

    updater.reingest(
        [_edit(root, "        self.box = Foo()\n", "        self.box = Bar()\n")]
    )

    assert engine._class_member_types_by_qn(holder) == {
        "box": f"{PROJECT}.app.models.Bar"
    }


def test_repeated_reingests_do_not_grow_the_node_keyed_caches(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    _store, updater = _project(root)
    engine = updater.factory.type_inference._python_type_inference
    assert engine is not None
    path = root / "app/models.py"
    sizes = []
    for n in range(4):
        path.write_text(f"{MODELS}\n# edit {n}\n")
        updater.reingest([path])
        sizes.append(
            (len(engine._return_stmt_cache), len(engine._self_assignment_cache))
        )
    assert sizes == [sizes[0]] * len(sizes)


# Negative: what must not change.


def test_a_reingest_with_no_type_change_keeps_the_edge(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    store, updater = _project(root)
    updater.reingest([_edit(root, '        return "bar"\n', '        return "BAR"\n')])
    assert "app.models.Foo.ping" in _callees(store, "app.service.use")


def test_a_fresh_index_of_the_edited_code_binds_the_new_type(
    temp_repo: Path,
) -> None:
    # The baseline a reingest must match.
    edited = MODELS.replace("return Foo()", "return Bar()")
    store, _updater = _project(temp_repo / PROJECT, edited)
    callees = _callees(store, "app.service.use")
    assert "app.models.Bar.ping" in callees
    assert "app.models.Foo.ping" not in callees
