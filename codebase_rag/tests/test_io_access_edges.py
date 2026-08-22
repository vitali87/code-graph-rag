from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
WRITES_TO = cs.RelationshipType.WRITES_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run_io(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    # Build the graph for `files` and return (caller_qn, rel_type, resource_qn)
    # for READS_FROM / WRITES_TO edges only.
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in (READS_FROM, WRITES_TO)
    }


def _has(rels: set[tuple[str, str, str]], caller: str, rel: str, resource: str) -> bool:
    return any(a.endswith(caller) and r == rel and b == resource for a, r, b in rels)


def test_open_read_literal(tmp_path: Path) -> None:
    files = {"m.py": "def load():\n    open('config.yaml')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.load", READS_FROM, "resource::FILE::config.yaml")


def test_open_write_mode(tmp_path: Path) -> None:
    files = {"m.py": "def save():\n    open('out.txt', 'w')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::out.txt")


def test_open_write_dynamic_target(tmp_path: Path) -> None:
    files = {"m.py": "def save(path):\n    open(path, 'w')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::<dynamic>")


def test_open_keyword_mode_write(tmp_path: Path) -> None:
    # mode passed by keyword must still refine direction to WRITE.
    files = {"m.py": "def save():\n    open('out.txt', mode='w')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::out.txt")


def test_open_all_keyword_args(tmp_path: Path) -> None:
    # both target and mode by keyword: target and direction still resolve.
    files = {"m.py": "def save():\n    open(file='out.txt', mode='w')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::out.txt")


def test_requests_get_read(tmp_path: Path) -> None:
    files = {
        "m.py": "import requests\n\ndef fetch(url):\n    requests.get(url)\n",
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.fetch", READS_FROM, "resource::NETWORK::<dynamic>")


def test_os_getenv_read(tmp_path: Path) -> None:
    files = {"m.py": "import os\n\ndef cfg():\n    os.getenv('HOME')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.cfg", READS_FROM, "resource::ENV::HOME")


def test_print_write_stdout(tmp_path: Path) -> None:
    files = {"m.py": "def show(x):\n    print(x)\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.show", WRITES_TO, "resource::STDOUT::<dynamic>")


def test_file_handle_write(tmp_path: Path) -> None:
    files = {
        "m.py": ("def save(data):\n    f = open('a.txt', 'w')\n    f.write(data)\n"),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::a.txt")


def test_with_handle_binding_tracked(tmp_path: Path) -> None:
    # `with sqlite3.connect(...) as conn:` binds a handle whose constructor is
    # not itself a sink; the later conn.execute must still attribute the edge.
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def ins():\n"
            "    with sqlite3.connect('db.sqlite') as conn:\n"
            "        conn.execute('INSERT INTO t VALUES (1)')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.ins", WRITES_TO, "resource::DATABASE::db.sqlite")


def test_handle_use_before_assignment_emits_nothing(tmp_path: Path) -> None:
    # A handle method that appears before its binding must not resolve to a
    # later assignment (no false forward-reference edge).
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def f():\n"
            "    conn.execute('INSERT INTO t VALUES (1)')\n"
            "    conn = sqlite3.connect('db.sqlite')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "m.f", WRITES_TO, "resource::DATABASE::db.sqlite")


def test_handle_reassignment_uses_last_binding(tmp_path: Path) -> None:
    # A rebind must resolve to the last assignment in source order. Uses
    # sqlite3.connect (a handle constructor that is NOT itself a sink) so the
    # only edge comes from handle attribution, isolating traversal order.
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def ins():\n"
            "    conn = sqlite3.connect('first.db')\n"
            "    conn = sqlite3.connect('second.db')\n"
            "    conn.execute('INSERT INTO t VALUES (1)')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.ins", WRITES_TO, "resource::DATABASE::second.db")
    assert not _has(rels, "m.ins", WRITES_TO, "resource::DATABASE::first.db")


def test_db_handle_select_is_read(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def q():\n"
            "    conn = sqlite3.connect('db.sqlite')\n"
            "    conn.execute('SELECT * FROM t')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.q", READS_FROM, "resource::DATABASE::db.sqlite")


def test_db_handle_insert_is_write(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def ins():\n"
            "    conn = sqlite3.connect('db.sqlite')\n"
            "    conn.execute('INSERT INTO t VALUES (1)')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.ins", WRITES_TO, "resource::DATABASE::db.sqlite")


def test_write_only_capture_drops_dangling_read_resource(tmp_path: Path) -> None:
    # With only WRITES_TO enabled, a read-only sink must not persist a
    # Resource node whose READS_FROM edge the filter drops (no orphan node).
    capture = resolve_capture(
        [cs.CAPTURE_TOKEN_NONE, f"{cs.CAPTURE_ADD_PREFIX}{WRITES_TO}"]
    )
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    (tmp_path / "m.py").write_text(
        "import os\n\ndef cfg():\n    os.getenv('HOME')\n", encoding="utf-8"
    )
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run()
    node_qns = {
        c.args[1].get(cs.KEY_QUALIFIED_NAME)
        for c in mock.ensure_node_batch.call_args_list
        if len(c.args) >= 2
    }
    assert "resource::ENV::HOME" not in node_qns


def test_plain_call_emits_no_io(tmp_path: Path) -> None:
    files = {
        "m.py": "def helper():\n    return 1\n\ndef run():\n    helper()\n",
    }
    rels = _run_io(tmp_path, files)
    assert rels == set()


def test_nested_read_attributes_to_inner_scope_only(tmp_path: Path) -> None:
    # A read inside a nested def belongs to that nested caller alone; the
    # enclosing function (and the module) must NOT also be credited with it,
    # matching how FLOWS_TO and CALLS attribute to the immediate scope.
    files = {
        "m.py": (
            "import os\n\ndef outer():\n    def inner():\n        os.getenv('K')\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.outer.inner", READS_FROM, "resource::ENV::K")
    assert not _has(rels, "m.outer", READS_FROM, "resource::ENV::K")


def test_module_scope_read_not_credited_with_function_read(tmp_path: Path) -> None:
    # A read inside a function must not also be attributed to the module
    # caller_spec (module-level IO is only genuine top-level statements).
    files = {"m.py": "import os\n\n\ndef load():\n    os.getenv('K')\n"}
    rels = _run_io(tmp_path, files)
    module_reads = [
        a
        for a, r, b in rels
        if r == READS_FROM and b.endswith("ENV::K") and a.endswith(".m")
    ]
    assert module_reads == []


def test_default_argument_read_belongs_to_enclosing_scope(tmp_path: Path) -> None:
    # A default argument value is evaluated at definition time in the
    # ENCLOSING scope, not inside the function body. So a read there is the
    # module's, never the function's.
    files = {"m.py": "import os\n\n\ndef f(x=os.getenv('K')):\n    pass\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, ".m", READS_FROM, "resource::ENV::K")
    assert not _has(rels, "m.f", READS_FROM, "resource::ENV::K")


def test_decorator_read_belongs_to_enclosing_scope(tmp_path: Path) -> None:
    # A decorator expression is evaluated in the enclosing scope at
    # definition time, so its read is the module's, not the function's.
    files = {
        "m.py": (
            "import os\n\n\n"
            "def deco(v):\n    return lambda fn: fn\n\n\n"
            "@deco(os.getenv('D'))\n"
            "def f():\n    pass\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, ".m", READS_FROM, "resource::ENV::D")
    assert not _has(rels, "m.f", READS_FROM, "resource::ENV::D")


def test_body_read_still_belongs_to_the_function(tmp_path: Path) -> None:
    # Guard: a genuine body read stays attributed to the function after the
    # header/body split (not accidentally pushed to the module).
    files = {"m.py": "import os\n\n\ndef load():\n    os.getenv('K')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.load", READS_FROM, "resource::ENV::K")


# Cross-scope handle recall: a handle bound in one scope (an instance
# attribute set in __init__, or a module/enclosing-scope local) and used in
# another must attribute the I/O to the USING scope.
def test_self_attr_db_handle_used_in_other_method(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "class DB:\n"
            "    def __init__(self):\n"
            "        self.conn = sqlite3.connect('app.db')\n\n"
            "    def run(self, sql):\n"
            "        self.conn.execute(sql)\n"
        )
    }
    rels = _run_io(tmp_path, files)
    # execute() with dynamic SQL stays READ_WRITE -> both edges, credited to
    # run (not __init__).
    assert _has(rels, "m.DB.run", READS_FROM, "resource::DATABASE::app.db")
    assert _has(rels, "m.DB.run", WRITES_TO, "resource::DATABASE::app.db")
    assert not _has(rels, "m.DB.__init__", READS_FROM, "resource::DATABASE::app.db")


def test_self_attr_file_handle_write(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "class Log:\n"
            "    def __init__(self, p):\n"
            "        self.f = open(p, 'w')\n\n"
            "    def emit(self, msg):\n"
            "        self.f.write(msg)\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.Log.emit", WRITES_TO, "resource::FILE::<dynamic>")


def test_module_level_handle_used_in_function(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "conn = sqlite3.connect('app.db')\n\n"
            "def run():\n"
            "    conn.executemany('INSERT INTO t VALUES (?)', [])\n"
        )
    }
    rels = _run_io(tmp_path, files)
    # executemany() is WRITE, credited to the function that uses the handle.
    assert _has(rels, "m.run", WRITES_TO, "resource::DATABASE::app.db")


def test_enclosing_function_handle_used_in_nested(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "def outer(path):\n"
            "    f = open(path, 'w')\n\n"
            "    def inner():\n"
            "        f.write('x')\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.outer.inner", WRITES_TO, "resource::FILE::<dynamic>")


def test_reexported_stdlib_module_handle_recognized(tmp_path: Path) -> None:
    # A stdlib module re-exported under its own name (`from .utils import
    # sqlite3`, as sqlite-utils does) remaps the local head away from the
    # registry key; the raw dotted callee must still match `sqlite3.connect`.
    files = {
        "pkg/__init__.py": "",
        "pkg/utils.py": "import sqlite3  # noqa: F401\n",
        "pkg/db.py": (
            "from .utils import sqlite3\n\n"
            "def run(sql):\n"
            "    conn = sqlite3.connect('app.db')\n"
            "    conn.execute(sql)\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "pkg.db.run", READS_FROM, "resource::DATABASE::app.db")
    assert _has(rels, "pkg.db.run", WRITES_TO, "resource::DATABASE::app.db")


def test_local_rebind_shadows_inherited_handle_before_assignment(
    tmp_path: Path,
) -> None:
    # Any assignment to `conn` in the body makes it local for the WHOLE
    # function (Python scoping), so a use BEFORE that assignment is an
    # UnboundLocalError at runtime and must NOT resolve to the inherited
    # module-level handle. The later local rebind governs uses after it.
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "conn = sqlite3.connect('mod.db')\n\n"
            "def run(sql):\n"
            "    conn.execute(sql)\n"
            "    conn = sqlite3.connect('local.db')\n"
            "    conn.execute(sql)\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "m.run", READS_FROM, "resource::DATABASE::mod.db")
    assert not _has(rels, "m.run", WRITES_TO, "resource::DATABASE::mod.db")
    assert _has(rels, "m.run", READS_FROM, "resource::DATABASE::local.db")


def test_sql_commit_is_write_only(tmp_path: Path) -> None:
    # COMMIT/ROLLBACK/SAVEPOINT/REPLACE etc. are writes; without them the
    # first-keyword heuristic falls back to READ_WRITE and over-reports a read.
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def tx():\n"
            "    conn = sqlite3.connect('db.sqlite')\n"
            "    conn.execute('COMMIT')\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.tx", WRITES_TO, "resource::DATABASE::db.sqlite")
    assert not _has(rels, "m.tx", READS_FROM, "resource::DATABASE::db.sqlite")


def test_fstring_url_keeps_interpolation_placeholder(tmp_path: Path) -> None:
    # An interpolated segment must stay visible as a placeholder; truncating
    # the resource to its first literal fragment fabricates a URL that then
    # resolves to the wrong endpoint (issue #876).
    files = {
        "m.py": (
            "import requests\n\n"
            "def fetch(user_id):\n"
            "    requests.get(f'http://user-service:8000/users/{user_id}')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(
        rels,
        "m.fetch",
        READS_FROM,
        "resource::NETWORK::http://user-service:8000/users/{user_id}",
    )
    assert not _has(
        rels,
        "m.fetch",
        READS_FROM,
        "resource::NETWORK::http://user-service:8000/users/",
    )


def test_fstring_without_literal_content_is_dynamic(tmp_path: Path) -> None:
    # Placeholders alone carry no identity: same-named variables in unrelated
    # projects would collide into one shared resource node.
    files = {
        "m.py": (
            "import requests\n\ndef fetch(url, uid):\n    requests.get(f'{url}{uid}')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.fetch", READS_FROM, "resource::NETWORK::<dynamic>")


def test_escape_sequence_does_not_truncate_path(tmp_path: Path) -> None:
    # tree-sitter splits string content around escape sequences; the resource
    # identity must keep the whole literal, not just the first fragment.
    files = {"m.py": 'def load():\n    open("logs\\nightly.txt")\n'}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.load", READS_FROM, "resource::FILE::logs\\nightly.txt")


def test_interpolation_with_slash_collapses_to_opaque_placeholder(
    tmp_path: Path,
) -> None:
    # An expression containing a path or parse delimiter would fabricate
    # extra URL segments ({offset/limit} reads as two segments) or change
    # how urlparse splits the URL, so it collapses to an opaque placeholder.
    files = {
        "m.py": (
            "import requests\n\n"
            "def page(offset, limit):\n"
            "    requests.get(f'http://svc:8000/users/{offset/limit}')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(
        rels, "m.page", READS_FROM, "resource::NETWORK::http://svc:8000/users/{*}"
    )


def test_python_os_environ_subscript_read(tmp_path: Path) -> None:
    # `os.environ['K']` is the subscript spelling of the `os.getenv('K')`
    # env read; the io walk must emit READS_FROM ENV K (issue #1324).
    files = {"m.py": "import os\n\ndef leak():\n    return os.environ['TOKEN']\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels
    assert not _has(rels, "m.leak", WRITES_TO, "resource::ENV::TOKEN"), rels


def test_python_os_environ_subscript_write(tmp_path: Path) -> None:
    # `os.environ['K'] = v` mutates the environment (dotenv's core behavior);
    # the subscript on the assignment's LHS is a WRITE, not a READ.
    files = {"m.py": "import os\n\ndef set(v):\n    os.environ['TOKEN'] = v\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.set", WRITES_TO, "resource::ENV::TOKEN"), rels
    assert not _has(rels, "m.set", READS_FROM, "resource::ENV::TOKEN"), rels


def test_python_os_environ_subscript_augmented_reads_and_writes(
    tmp_path: Path,
) -> None:
    # `os.environ['K'] += v` reads the old value and writes the new one,
    # mirroring the lean member-walk augmented-assignment semantics.
    files = {"m.py": "import os\n\ndef grow(v):\n    os.environ['PATH_LIKE'] += v\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.grow", WRITES_TO, "resource::ENV::PATH_LIKE"), rels
    assert _has(rels, "m.grow", READS_FROM, "resource::ENV::PATH_LIKE"), rels


def test_python_os_environ_subscript_dynamic_key_is_dynamic(tmp_path: Path) -> None:
    # A non-literal index keeps the <dynamic> identity, matching the flow walk
    # and the lean member walks (issue #1324).
    files = {
        "m.py": "import os\n\nk = 'TOKEN'\ndef leak():\n    return os.environ[k]\n"
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.leak", READS_FROM, "resource::ENV::<dynamic>"), rels


def test_python_os_environ_subscript_aliased_import(tmp_path: Path) -> None:
    # The object is import-normalised before the member-read match, so an
    # aliased `import os as o` still reads `os.environ` (issue #1324).
    files = {"m.py": "import os as o\n\ndef leak():\n    return o.environ['TOKEN']\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels


def test_python_unrelated_subscript_is_not_env(tmp_path: Path) -> None:
    # A subscript of any other object (`config['TOKEN']`) is not an env read,
    # even with a TOKEN index (maintainer review on PR #1325).
    files = {"m.py": "import config\n\ndef leak():\n    return config['TOKEN']\n"}
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels
    assert not _has(rels, "m.leak", WRITES_TO, "resource::ENV::TOKEN"), rels


def test_python_unimported_os_subscript_is_not_env(tmp_path: Path) -> None:
    # An unimported `os` is a local or a NameError, never the stdlib module;
    # the import gate must reject it (issue #1324).
    files = {"m.py": "def leak():\n    return os.environ['TOKEN']\n"}
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels


def test_python_os_environ_subscript_global_receiver_is_env(
    tmp_path: Path,
) -> None:
    # `global os` makes the later `os = Fake()` a module-global rebind, NOT a
    # local binding, so the earlier read still hits the imported module and
    # emits READS_FROM ENV (CodeRabbit review on PR #1325).
    files = {
        "m.py": (
            "import os\n\n"
            "class Fake:\n"
            "    environ = {'TOKEN': 'x'}\n\n"
            "def leak():\n"
            "    global os\n"
            "    print(os.environ['TOKEN'])\n"
            "    os = Fake()\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels


def test_python_os_environ_subscript_global_rebind_before_read_not_env(
    tmp_path: Path,
) -> None:
    # A `global`-declared name keeps its module binding only until a REBIND:
    # once `os = Fake()` executes, a later subscript hits the rebound value,
    # not the imported module (CodeRabbit review on PR #1325).
    files = {
        "m.py": (
            "import os\n\n"
            "class Fake:\n"
            "    environ = {'TOKEN': 'x'}\n\n"
            "def leak():\n"
            "    global os\n"
            "    os = Fake()\n"
            "    return os.environ['TOKEN']\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels
    assert not _has(rels, "m.leak", WRITES_TO, "resource::ENV::TOKEN"), rels


def test_python_os_environ_subscript_augmented_assignment_shadow_not_env(
    tmp_path: Path,
) -> None:
    # `os += v` binds `os` locally for the whole function exactly like a plain
    # assignment, so the subscript is not an env read (Greptile review on
    # PR #1325).
    files = {
        "m.py": (
            "import os\n\ndef leak(v):\n    os += v\n    return os.environ['TOKEN']\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels
    assert not _has(rels, "m.leak", WRITES_TO, "resource::ENV::TOKEN"), rels


def test_python_os_environ_subscript_walrus_shadow_not_env(tmp_path: Path) -> None:
    # The walrus `(os := v)` binds `os` locally for the whole function, so the
    # subscript is not an env read (Greptile review on PR #1325).
    files = {
        "m.py": (
            "import os\n\n"
            "def leak():\n"
            "    (os := source())\n"
            "    return os.environ['TOKEN']\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels
    assert not _has(rels, "m.leak", WRITES_TO, "resource::ENV::TOKEN"), rels


def test_python_os_environ_subscript_local_shadow_is_not_env(tmp_path: Path) -> None:
    # A name bound anywhere in the scope shadows the module-level import for
    # the whole scope, so a local `os = Fake()` never reads the env mapping.
    files = {
        "m.py": (
            "import os\n\n"
            "class Fake:\n"
            "    environ = {'TOKEN': 'x'}\n\n"
            "def leak():\n"
            "    os = Fake()\n"
            "    return os.environ['TOKEN']\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "m.leak", READS_FROM, "resource::ENV::TOKEN"), rels
    assert not _has(rels, "m.leak", WRITES_TO, "resource::ENV::TOKEN"), rels
