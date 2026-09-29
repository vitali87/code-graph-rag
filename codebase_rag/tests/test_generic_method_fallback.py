# A generic member call such as `subprocess.run(...)`, `asyncio.run(...)` or
# `agent.run(...)` names nothing first-party, yet the simple-name trie
# fallback bound it to whichever first-party `run` sat closest by import
# distance: indexing this repository gave `GraphUpdater.run` 917 incoming
# CALLS edges (issue #2360). Two receivers carry no evidence for such a
# binding: a module imported from outside the project, and a value whose
# type nothing inferred while several first-party methods share the name.
# Calls whose receiver type IS known must keep resolving exactly.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor

UPDATER_RUN = "repo.pkg.updater.GraphUpdater.run"
SESSION_RUN = "repo.pkg.driver.Session.run"

UPDATER = (
    "FRONTENDS = {}\n\n\n"
    "class GraphUpdater:\n"
    "    def run(self, force=False):\n"
    "        return force\n\n"
    "    def reingest(self):\n"
    "        return self.run(force=True)\n\n"
    "    def _run_frontend(self):\n"
    "        frontend = FRONTENDS.get('go')\n"
    "        return frontend.run('.', ())\n"
)
DRIVER = "class Session:\n    def run(self, query):\n        return query\n"

_Edge = tuple[str, str, str, str | None]


def _index(tmp_path: Path, files: dict[str, str]) -> set[_Edge]:
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    root = tmp_path / "repo"
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=root, parsers=parsers, queries=queries).run()
    edges: set[_Edge] = set()
    for call in mock.ensure_relationship_batch.call_args_list:
        props = call.args[3] if len(call.args) > 3 else call.kwargs.get("properties")
        resolution = props.get(cs.KEY_RESOLUTION) if props else None
        edges.add(
            (
                call.args[0][2],
                str(call.args[1]),
                call.args[2][2],
                str(resolution) if resolution else None,
            )
        )
    return edges


def _callees(edges: set[_Edge], caller: str) -> set[str]:
    return {
        callee
        for src, rel, callee, _ in edges
        if rel == cs.RelationshipType.CALLS and src == caller
    }


def _files(main: str, *, with_session: bool = True) -> dict[str, str]:
    files = {
        "pkg/__init__.py": "",
        "pkg/updater.py": UPDATER,
        "pkg/main.py": main,
    }
    if with_session:
        files["pkg/driver.py"] = DRIVER
    return files


# --- the defect: calls that must NOT bind to GraphUpdater.run --------------


@pytest.mark.parametrize(
    ("import_line", "call"),
    [
        ("import subprocess", "subprocess.run(['git', 'status'])"),
        ("import asyncio", "asyncio.run(start())"),
        ("import subprocess as sp", "sp.run(['git', 'status'])"),
        ("from concurrent import futures", "futures.run()"),
        # External on both paths of a try/except fallback import.
        (
            "try:\n    import subprocess32 as subprocess\n"
            "except ImportError:\n    import subprocess",
            "subprocess.run(['git', 'status'])",
        ),
        # An optional dependency: the other path binds no module at all.
        (
            "try:\n    import uvicorn\nexcept ImportError:\n    uvicorn = None",
            "uvicorn.run('app:app')",
        ),
    ],
    ids=["plain", "asyncio", "aliased", "from-package", "try-except", "optional"],
)
def test_external_module_call_does_not_bind_a_first_party_method(
    tmp_path: Path, import_line: str, call: str
) -> None:
    # GraphUpdater.run is the ONLY first-party `run` here, so nothing but the
    # receiver can rule it out: the receiver is a module from outside the
    # project, and a module attribute is never a first-party method.
    main = (
        f"{import_line}\n\n\n"
        "async def start():\n    return None\n\n\n"
        f"def caller():\n    return {call}\n"
    )
    edges = _index(tmp_path, _files(main, with_session=False))
    assert UPDATER_RUN not in _callees(edges, "repo.pkg.main.caller")


def test_function_level_import_of_an_external_module_does_not_bind(
    tmp_path: Path,
) -> None:
    # The `_git_state` shape: `subprocess` imported inside the function body.
    main = (
        "def caller():\n"
        "    import subprocess\n\n"
        "    return subprocess.run(['git', 'status'])\n"
    )
    edges = _index(tmp_path, _files(main, with_session=False))
    assert UPDATER_RUN not in _callees(edges, "repo.pkg.main.caller")


def test_untyped_local_does_not_guess_between_same_named_methods(
    tmp_path: Path,
) -> None:
    # `frontend` comes out of a dict lookup, so nothing typed it; with two
    # first-party `run` methods the trie picked GraphUpdater.run only for
    # sitting in the same module (the `_run_go_frontend` false edge).
    edges = _index(tmp_path, _files("def noop():\n    return None\n"))
    callees = _callees(edges, "repo.pkg.updater.GraphUpdater._run_frontend")
    assert UPDATER_RUN not in callees
    assert SESSION_RUN not in callees


def test_untyped_parameter_does_not_guess_between_same_named_methods(
    tmp_path: Path,
) -> None:
    main = "def ask(rag_agent):\n    return rag_agent.run('question')\n"
    callees = _callees(_index(tmp_path, _files(main)), "repo.pkg.main.ask")
    assert UPDATER_RUN not in callees
    assert SESSION_RUN not in callees


def test_untyped_attribute_does_not_guess_between_same_named_methods(
    tmp_path: Path,
) -> None:
    main = (
        "class Tools:\n"
        "    def __init__(self, agent):\n"
        "        self.rag_agent = agent\n\n"
        "    def ask(self):\n"
        "        return self.rag_agent.run('question')\n"
    )
    callees = _callees(_index(tmp_path, _files(main)), "repo.pkg.main.Tools.ask")
    assert UPDATER_RUN not in callees
    assert SESSION_RUN not in callees


def test_local_named_like_a_method_is_not_bound_to_it(tmp_path: Path) -> None:
    # A bare name is looked up in local, enclosing, global and builtin scope,
    # never on some class: the `run` handed to `to_thread` is the parameter.
    main = (
        "import asyncio\n\n\n"
        "async def graph_query(run, name):\n"
        "    return await asyncio.to_thread(run, name)\n"
    )
    edges = _index(tmp_path, _files(main))
    callees = _callees(edges, "repo.pkg.main.graph_query")
    assert UPDATER_RUN not in callees
    assert SESSION_RUN not in callees


# --- neighbouring behaviour that must NOT change ----------------------------


def _resolutions(edges: set[_Edge], caller: str, callee: str) -> set[str | None]:
    return {
        resolution
        for src, rel, dst, resolution in edges
        if rel == cs.RelationshipType.CALLS and src == caller and dst == callee
    }


@pytest.mark.parametrize(
    "body",
    [
        "    updater = GraphUpdater()\n    return updater.run()\n",
        "    return GraphUpdater().run()\n",
    ],
    ids=["constructed-local", "inline-construction"],
)
def test_constructed_receiver_still_resolves_exactly(tmp_path: Path, body: str) -> None:
    main = f"from pkg.updater import GraphUpdater\n\n\ndef index():\n{body}"
    edges = _index(tmp_path, _files(main))
    callees = _callees(edges, "repo.pkg.main.index")
    assert UPDATER_RUN in callees
    assert SESSION_RUN not in callees
    assert _resolutions(edges, "repo.pkg.main.index", UPDATER_RUN) == {
        cs.EdgeResolution.EXACT.value
    }


def test_annotated_parameter_receiver_still_resolves_exactly(tmp_path: Path) -> None:
    main = (
        "from pkg.updater import GraphUpdater\n\n\n"
        "def index(updater: GraphUpdater):\n"
        "    return updater.run()\n"
    )
    edges = _index(tmp_path, _files(main))
    callees = _callees(edges, "repo.pkg.main.index")
    assert UPDATER_RUN in callees
    assert SESSION_RUN not in callees


def test_self_call_inside_the_class_still_resolves(tmp_path: Path) -> None:
    edges = _index(tmp_path, _files("def noop():\n    return None\n"))
    callees = _callees(edges, "repo.pkg.updater.GraphUpdater.reingest")
    assert UPDATER_RUN in callees
    assert SESSION_RUN not in callees


def test_untyped_receiver_with_a_unique_method_name_still_binds(
    tmp_path: Path,
) -> None:
    # Only GraphUpdater defines `reingest`, so the name alone singles it out;
    # the edge stays, labelled as the guess it is.
    reingest = "repo.pkg.updater.GraphUpdater.reingest"
    main = "def refresh(updater):\n    return updater.reingest()\n"
    edges = _index(tmp_path, _files(main))
    assert reingest in _callees(edges, "repo.pkg.main.refresh")
    assert _resolutions(edges, "repo.pkg.main.refresh", reingest) == {
        cs.EdgeResolution.HEURISTIC.value
    }


def test_first_party_module_receiver_still_resolves(tmp_path: Path) -> None:
    # A module receiver is a namespace, not a value: `tasks.run()` names
    # the module's own `run` however many methods share the name.
    main = "from pkg import tasks\n\n\ndef go():\n    return tasks.run()\n"
    files = _files(main)
    files["pkg/tasks.py"] = "def run():\n    return 1\n"
    callees = _callees(_index(tmp_path, files), "repo.pkg.main.go")
    assert callees == {"repo.pkg.tasks.run"}


def test_imported_class_receiver_still_resolves(tmp_path: Path) -> None:
    main = (
        "from pkg.updater import GraphUpdater\n\n\n"
        "def index(updater):\n"
        "    return GraphUpdater.run(updater)\n"
    )
    callees = _callees(_index(tmp_path, _files(main)), "repo.pkg.main.index")
    assert UPDATER_RUN in callees
    assert SESSION_RUN not in callees


def test_sibling_script_module_imported_by_bare_name_still_binds(
    tmp_path: Path,
) -> None:
    # `import helper` in a script beside `helper.py` works when the script
    # runs, but the import processor cannot place it and records the bare
    # name, exactly as for an external module. The member it calls is
    # spelled out by a first-party definition, so the fallback keeps it.
    files = _files("def noop():\n    return None\n")
    files["scripts/helper.py"] = "def run():\n    return 1\n"
    files["scripts/tool.py"] = (
        "import helper\n\n\ndef main():\n    return helper.run()\n"
    )
    callees = _callees(_index(tmp_path, files), "repo.scripts.tool.main")
    assert callees == {"repo.scripts.helper.run"}


def test_annotated_attribute_receiver_still_resolves_exactly(tmp_path: Path) -> None:
    main = (
        "from pkg.updater import GraphUpdater\n\n\n"
        "class Tools:\n"
        "    def __init__(self, updater: GraphUpdater):\n"
        "        self.updater = updater\n\n"
        "    def sync(self):\n"
        "        return self.updater.run()\n"
    )
    edges = _index(tmp_path, _files(main))
    callees = _callees(edges, "repo.pkg.main.Tools.sync")
    assert UPDATER_RUN in callees
    assert SESSION_RUN not in callees


def test_class_body_reference_to_its_own_method_still_binds(tmp_path: Path) -> None:
    # Inside its class body a method IS reachable by its bare name, and a
    # `property(_get_x)` there is what keeps the getter alive.
    files = _files("def noop():\n    return None\n")
    files["pkg/model.py"] = (
        "class Box:\n"
        "    def _get_x(self):\n"
        "        return 1\n\n"
        "    x = property(_get_x)\n"
    )
    assert "repo.pkg.model.Box._get_x" in _callees(
        _index(tmp_path, files), "repo.pkg.model"
    )


def test_fallback_import_with_a_first_party_branch_still_binds(
    tmp_path: Path,
) -> None:
    # Plain imports are handled before from-imports, so the mapping keeps
    # `extlib.runner`; the other path binds the first-party module, which
    # must stay reachable through the fallbacks.
    main = (
        "try:\n"
        "    import pkg.fastrun as runner\n"
        "except ImportError:\n"
        "    from extlib import runner\n\n\n"
        "def go():\n"
        "    return runner.fast_run()\n"
    )
    files = _files(main)
    files["pkg/fastrun.py"] = "def fast_run():\n    return 1\n"
    callees = _callees(_index(tmp_path, files), "repo.pkg.main.go")
    assert callees == {"repo.pkg.fastrun.fast_run"}


def test_import_rebinds_are_recorded_and_reset_on_reparse(tmp_path: Path) -> None:
    # The resolver trusts this record to decide a module is external on every
    # path, so a re-parse that drops the fallback import must drop it too.
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    processor = ImportProcessor(repo_path=tmp_path, project_name="repo")
    module_qn = "repo.tool"

    def parse(source: str) -> None:
        tree = parsers[cs.SupportedLanguage.PYTHON].parse(source.encode())
        processor.parse_imports(
            tree.root_node, module_qn, cs.SupportedLanguage.PYTHON, queries
        )

    parse(
        "try:\n    import tomllib\nexcept ImportError:\n    import tomli as tomllib\n"
        "import json\n"
    )
    assert processor.python_import_rebinds[module_qn] == {
        "tomllib": {"tomllib", "tomli"}
    }

    parse("import tomllib\n")
    assert module_qn not in processor.python_import_rebinds


def test_module_level_class_alias_receiver_still_binds(tmp_path: Path) -> None:
    # `Updater = GraphUpdater` names the class, not a value, so a call
    # through the alias is not an untyped-value guess.
    files = _files("def noop():\n    return None\n")
    files["pkg/updater.py"] = (
        f"{UPDATER}\n\nUpdater = GraphUpdater\n\n\n"
        "def refresh(updater):\n"
        "    return Updater.run(updater)\n"
    )
    callees = _callees(_index(tmp_path, files), "repo.pkg.updater.refresh")
    assert callees == {UPDATER_RUN}


def test_root_package_module_imported_by_bare_name_still_binds(
    tmp_path: Path,
) -> None:
    # With an `__init__.py` at the repository root, `import helpers` is
    # recorded bare, as an external module would be; the package re-exports
    # `run_all` from a submodule, so no definition spells `helpers.run_all`
    # and only the registry's `repo.helpers` subtree marks it first-party.
    files = _files("def noop():\n    return None\n")
    files["__init__.py"] = ""
    files["helpers/__init__.py"] = "from .impl import run_all\n"
    files["helpers/impl.py"] = "def run_all():\n    return 1\n"
    files["tool.py"] = "import helpers\n\n\ndef main():\n    return helpers.run_all()\n"
    callees = _callees(_index(tmp_path, files), "repo.tool.main")
    assert callees == {"repo.helpers.impl.run_all"}
