# Import statement rewriting for rename/move (issue #1530): the IMPORTS edge
# sites of issue #1522 name every statement to touch, and the span patcher of
# issue #1529 keeps everything around them intact.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import ImportRewriter, ImportSite, SymbolMove
from codebase_rag.tests.conftest import create_and_run_updater


def _assert_parses(parses: bool | None, grammar: str) -> None:
    # The patcher reports None when no grammar is installed for the file
    # (the base install ships none for Rust and Go): nothing to verify then.
    if parses is None:
        pytest.skip(f"{grammar} grammar not installed")
    assert parses is True


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _site(
    text: str, needle: str, path: str, alias: str | None = None, name: str | None = None
) -> ImportSite:
    """An ImportSite for the statement `needle` inside `text`, like the graph records it."""
    idx = text.index(needle)
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1)
    end = idx + len(needle)
    end_line = text.count("\n", 0, end) + 1
    end_col = end - (text.rfind("\n", 0, end) + 1)
    return ImportSite(path, line, col, end_line, end_col, alias, name)


def _sites_from_graph(mock, importer_suffix: str) -> list[ImportSite]:
    """The IMPORTS edges the indexer produced for one importer, as ImportSites."""
    out = []
    for c in mock.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != cs.RelationshipType.IMPORTS:
            continue
        props = c.kwargs.get("properties") or {}
        src = str(c.args[0][2])
        if not src.endswith(importer_suffix) or cs.KEY_LINE not in props:
            continue
        out.append((props, src))
    return out


# --- Python ---------------------------------------------------------------------


PY_APP = "from pkg.util import helper as h, other\nimport pkg.util\n\n\ndef run():\n    return h() + other() + pkg.util.other()\n"
PY_INIT = 'from pkg.util import helper\n\n__all__ = ["helper", "other"]\n'


def test_python_move_updates_aliased_import_and_splits_the_statement(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "pkg/app.py", PY_APP)
    rewriter = ImportRewriter(tmp_path)
    site = _site(
        PY_APP, "from pkg.util import helper as h, other", "pkg/app.py", "h", "helper"
    )
    move = SymbolMove(symbol="helper", old_module="pkg.util", new_module="pkg.helpers")
    (rewrite,) = rewriter.retarget([site], move)
    assert (
        rewrite.after
        == "from pkg.util import other\nfrom pkg.helpers import helper as h"
    )
    (result,) = rewriter.patcher.apply().values()
    assert result.parses is True
    assert result.content.decode() == (
        "from pkg.util import other\nfrom pkg.helpers import helper as h\nimport pkg.util\n\n\n"
        "def run():\n    return h() + other() + pkg.util.other()\n"
    )


def test_python_rename_keeps_the_local_name_through_an_alias(tmp_path: Path) -> None:
    src = "from pkg.util import helper\n"
    _write(tmp_path, "pkg/app.py", src)
    rewriter = ImportRewriter(tmp_path)
    site = _site(src, "from pkg.util import helper", "pkg/app.py", "helper", "helper")
    (rewrite,) = rewriter.retarget(
        [site], SymbolMove("helper", "pkg.util", "pkg.util", new_name="assist")
    )
    assert rewrite.after == "from pkg.util import assist as helper"


def test_python_module_import_is_retargeted_when_the_module_moves(
    tmp_path: Path,
) -> None:
    src = "import pkg.util as u\n"
    _write(tmp_path, "pkg/app.py", src)
    rewriter = ImportRewriter(tmp_path)
    site = _site(src, "import pkg.util as u", "pkg/app.py", "u", None)
    (rewrite,) = rewriter.retarget(
        [site], SymbolMove("pkg.util", "pkg.util", "pkg.core.util")
    )
    assert rewrite.after == "import pkg.core.util as u"


def test_python_barrel_and_dunder_all(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", PY_INIT)
    rewriter = ImportRewriter(tmp_path)
    site = _site(
        PY_INIT, "from pkg.util import helper", "pkg/__init__.py", "helper", "helper"
    )
    (rewrite,) = rewriter.retarget(
        [site], SymbolMove("helper", "pkg.util", "pkg.helpers", new_name="assist")
    )
    # The barrel keeps exporting `helper`; only the leaf moved.
    assert rewrite.after == "from pkg.helpers import assist as helper"
    assert rewriter.rename_in_all("pkg/__init__.py", "other", "another") == 1
    (result,) = rewriter.patcher.apply().values()
    assert (
        result.content.decode()
        == 'from pkg.helpers import assist as helper\n\n__all__ = ["helper", "another"]\n'
    )


def test_unrelated_statements_are_left_untouched(tmp_path: Path) -> None:
    src = "from pkg.util import other\nfrom elsewhere import helper\n"
    _write(tmp_path, "pkg/app.py", src)
    rewriter = ImportRewriter(tmp_path)
    sites = [
        _site(src, "from pkg.util import other", "pkg/app.py", "other", "other"),
        _site(src, "from elsewhere import helper", "pkg/app.py", "helper", "helper"),
    ]
    assert (
        rewriter.retarget(sites, SymbolMove("helper", "pkg.util", "pkg.helpers")) == []
    )
    assert len(rewriter.untouched) == 2
    assert rewriter.patcher.pending == {}


def test_python_move_from_graph_sites_still_imports(
    tmp_path: Path, mock_ingestor
) -> None:
    """End to end: index a fixture, take the IMPORTS sites from the graph,
    move the symbol, apply, and the package still imports (the acceptance's
    smoke import)."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/util.py",
        "def helper():\n    return 1\n\n\ndef other():\n    return 2\n",
    )
    _write(tmp_path, "pkg/helpers.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "pkg/app.py",
        "from pkg.util import helper as h, other\n\n\ndef run():\n    return h() + other()\n",
    )
    _write(
        tmp_path,
        "pkg/cli.py",
        "from pkg.util import (\n    helper,\n)\n\n\ndef main():\n    return helper()\n",
    )
    create_and_run_updater(tmp_path, mock_ingestor)

    sites: list[ImportSite] = []
    for props, _src in _sites_from_graph(mock_ingestor, ".pkg.app") + _sites_from_graph(
        mock_ingestor, ".pkg.cli"
    ):
        # Recover the importer's path from the edge's module qn.
        importer = "pkg/app.py" if "app" in _src else "pkg/cli.py"
        sites.append(
            ImportSite(
                importer,
                int(props[cs.KEY_LINE]),
                int(props[cs.KEY_COL]),
                int(props[cs.KEY_END_LINE]),
                int(props[cs.KEY_END_COL]),
                props.get(cs.KEY_ALIAS),
                props.get(cs.KEY_IMPORTED_NAME),
            )
        )
    assert len(sites) == 3, sites
    rewriter = ImportRewriter(tmp_path)
    rewrites = rewriter.retarget(sites, SymbolMove("helper", "pkg.util", "pkg.helpers"))
    assert {r.path for r in rewrites} == {"pkg/app.py", "pkg/cli.py"}
    for key, result in rewriter.patcher.apply().items():
        assert result.parses is True, key
        (tmp_path / key).write_bytes(result.content)
    assert (
        (tmp_path / "pkg" / "cli.py").read_text()
        == "from pkg.helpers import helper\n\n\ndef main():\n    return helper()\n"
    )
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pkg.app, pkg.cli; assert pkg.app.run() == 3 and pkg.cli.main() == 1",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert smoke.returncode == 0, smoke.stderr


# --- TypeScript / JavaScript -------------------------------------------------------


def test_typescript_named_import_split_and_relative_specifier(tmp_path: Path) -> None:
    src = "import { helper as h, other } from './util';\nexport const x = h();\n"
    _write(tmp_path, "src/app.ts", src)
    rewriter = ImportRewriter(tmp_path)
    site = _site(
        src, "import { helper as h, other } from './util';", "src/app.ts", "h", "helper"
    )
    move = SymbolMove("helper", "./util", "", new_module_path="src/lib/helpers.ts")
    (rewrite,) = rewriter.retarget([site], move)
    assert (
        rewrite.after
        == "import { other } from './util';\nimport { helper as h } from './lib/helpers';"
    )


def test_typescript_barrel_reexport_is_retargeted_at_the_leaf(tmp_path: Path) -> None:
    src = "export { helper } from './util';\nexport { other } from './util';\n"
    _write(tmp_path, "src/index.ts", src)
    rewriter = ImportRewriter(tmp_path)
    site = _site(
        src, "export { helper } from './util';", "src/index.ts", "helper", "helper"
    )
    move = SymbolMove(
        "helper", "./util", "", new_name="assist", new_module_path="src/helpers.ts"
    )
    (rewrite,) = rewriter.retarget([site], move)
    # Consumers of the barrel keep importing `helper`.
    assert rewrite.after == "export { assist as helper } from './helpers';"


def test_typescript_specifier_climbs_directories(tmp_path: Path) -> None:
    src = 'import * as util from "../util";\n'
    _write(tmp_path, "src/deep/app.ts", src)
    rewriter = ImportRewriter(tmp_path)
    site = _site(
        src, 'import * as util from "../util";', "src/deep/app.ts", "util", None
    )
    (rewrite,) = rewriter.retarget(
        [site], SymbolMove("../util", "../util", "", new_module_path="lib/util.ts")
    )
    assert rewrite.after == 'import * as util from "../../lib/util";'


# --- Java / Rust / Go ---------------------------------------------------------------


def test_java_import_retarget_and_rename(tmp_path: Path) -> None:
    src = "package app;\n\nimport util.Helper;\n"
    _write(tmp_path, "app/App.java", src)
    rewriter = ImportRewriter(tmp_path)
    site = _site(src, "import util.Helper;", "app/App.java", "Helper", "Helper")
    (rewrite,) = rewriter.retarget(
        [site], SymbolMove("Helper", "util", "core.util", new_name="Assist")
    )
    assert rewrite.after == "import core.util.Assist;"


def test_rust_use_single_grouped_and_aliased(tmp_path: Path) -> None:
    src = "use crate::util::helper;\nuse crate::util::{helper as h, other};\nuse crate::util::helper as hh;\n"
    _write(tmp_path, "src/main.rs", src)
    rewriter = ImportRewriter(tmp_path)
    sites = [
        _site(src, "use crate::util::helper;", "src/main.rs", "helper", "helper"),
        _site(
            src, "use crate::util::{helper as h, other};", "src/main.rs", "h", "helper"
        ),
        _site(src, "use crate::util::helper as hh;", "src/main.rs", "hh", "helper"),
    ]
    rewrites = rewriter.retarget(
        sites, SymbolMove("helper", "crate::util", "crate::helpers", new_name="assist")
    )
    assert [r.after for r in rewrites] == [
        "use crate::helpers::assist as helper;",
        "use crate::util::other;\nuse crate::helpers::assist as h;",
        "use crate::helpers::assist as hh;",
    ]
    (result,) = rewriter.patcher.apply().values()
    _assert_parses(result.parses, "rust")


def test_go_import_path_retarget(tmp_path: Path) -> None:
    src = 'package main\n\nimport (\n\tu "example.com/mod/util"\n\t"fmt"\n)\n'
    _write(tmp_path, "main.go", src)
    rewriter = ImportRewriter(tmp_path)
    site = _site(src, 'u "example.com/mod/util"', "main.go", "u", None)
    (rewrite,) = rewriter.retarget(
        [site],
        SymbolMove(
            "example.com/mod/util", "example.com/mod/util", "example.com/mod/core/util"
        ),
    )
    assert rewrite.after == 'u "example.com/mod/core/util"'
    (result,) = rewriter.patcher.apply().values()
    _assert_parses(result.parses, "go")


@pytest.mark.parametrize("bad", [ImportSite("nope.py", 1, 0, 1, 5, None, None)])
def test_missing_importer_file_is_an_error(tmp_path: Path, bad: ImportSite) -> None:
    from codebase_rag.editing import PatcherError

    rewriter = ImportRewriter(tmp_path)
    move = SymbolMove("x", "a", "b")
    with pytest.raises(PatcherError):
        rewriter.retarget([bad], move)


def test_every_alias_of_the_moved_symbol_survives_the_move() -> None:
    # `helper` and `helper as h` both bind the moved symbol; a rewrite that
    # keeps only the first drops the `h` binding and breaks its call sites.
    from codebase_rag.editing.imports import _py_rewrite

    out = _py_rewrite(
        "from pkg.util import helper, helper as h, other",
        SymbolMove("helper", "pkg.util", "pkg.new"),
    )
    assert out == "from pkg.util import other\nfrom pkg.new import helper, helper as h"


def test_every_js_alias_of_the_moved_symbol_survives_the_move() -> None:
    # `helper as h` in a named-import list binds the moved symbol too; losing
    # it leaves `h()` in the body pointing at nothing.
    from codebase_rag.editing.imports import _js_rewrite

    out = _js_rewrite(
        "import { helper, helper as h, other } from './util';",
        SymbolMove("helper", "./util", "./lib/helpers"),
        "src/app.ts",
    )
    assert out == (
        "import { other } from './util';\n"
        "import { helper, helper as h } from './lib/helpers';"
    )


def test_every_rust_alias_of_the_moved_symbol_survives_the_move() -> None:
    # Same defect in the Rust `use` list: `helper as h` must move with it.
    from codebase_rag.editing.imports import _rs_rewrite

    out = _rs_rewrite(
        "use pkg::util::{helper, helper as h, other};",
        SymbolMove("helper", "pkg::util", "pkg::new"),
    )
    assert out == "use pkg::util::other;\nuse pkg::new::{helper, helper as h};"


def test_a_default_binding_stays_with_its_original_module() -> None:
    # `import def, { helper } from './util'` binds `def` to ./util. Moving
    # `helper` must not redeclare `def` in the new statement, nor carry it to
    # the new module when the named list empties.
    from codebase_rag.editing.imports import _js_rewrite

    move = SymbolMove("helper", "./util", "./new")
    with_kept = _js_rewrite(
        "import def, { helper, other } from './util';", move, "src/app.ts"
    )
    assert with_kept == (
        "import def, { other } from './util';\nimport { helper } from './new';"
    )

    emptied = _js_rewrite("import def, { helper } from './util';", move, "src/app.ts")
    assert emptied == ("import def from './util';\nimport { helper } from './new';")


def test_a_type_only_import_keeps_its_modifier() -> None:
    # `type` is a modifier, not a default binding: it must stay attached to
    # the braces on both statements. `import type from './util'` parses as a
    # default import named `type`, so the patcher's parse gate cannot catch it.
    from codebase_rag.editing.imports import _js_rewrite

    move = SymbolMove("helper", "./util", "./new")
    assert (
        _js_rewrite("import type { helper } from './util';", move, "src/app.ts")
        == "import type { helper } from './new';"
    )
    assert (
        _js_rewrite("export type { helper } from './util';", move, "src/app.ts")
        == "export type { helper } from './new';"
    )
    assert _js_rewrite(
        "import type { helper, other } from './util';", move, "src/app.ts"
    ) == ("import type { other } from './util';\nimport type { helper } from './new';")


# The four behaviours the #1545 merge has to preserve. Two are rename-op's own
# (`ANY_MODULE`, `rebind`) and have no counterpart on `main`, so a port onto
# main's token parser drops them silently -- nothing else asserts on them.
# The fourth is main's, and is the one case where the two sides genuinely
# DISAGREE: rename-op rewrites `moved[0]` only and loses the second binding,
# main rewrites every entry. The port must take main's answer there, so the
# test states main's behaviour rather than this branch's.
def test_wildcard_old_module_matches_any_module() -> None:
    from codebase_rag.editing.imports import ANY_MODULE, _py_rewrite

    move = SymbolMove(symbol="helper", old_module=ANY_MODULE, new_module="pkg.helpers")
    # A wildcard rename leaves the module alone and only touches the name; the
    # statement is returned unchanged when there is no new name to write.
    assert (
        _py_rewrite("from anything.at.all import helper", move)
        == "from anything.at.all import helper"
    )
    # ...and a non-matching symbol is still refused, so the wildcard widens the
    # MODULE test only.
    assert _py_rewrite("from anything.at.all import other", move) is None


def test_rebind_drops_the_local_alias_and_no_rebind_keeps_it() -> None:
    from codebase_rag.editing.imports import _py_rewrite

    rebinding = SymbolMove(
        symbol="helper",
        old_module="pkg.util",
        new_module="pkg.util",
        new_name="helper2",
        rebind=True,
    )
    # rebind=True renames the use sites too, so the local name follows.
    assert (
        _py_rewrite("from pkg.util import helper", rebinding)
        == "from pkg.util import helper2"
    )

    keeping = rebinding._replace(rebind=False)
    # rebind=False leaves the use sites alone, so the old name must stay bound.
    assert (
        _py_rewrite("from pkg.util import helper", keeping)
        == "from pkg.util import helper2 as helper"
    )


def test_every_entry_binding_the_symbol_moves_not_just_the_first() -> None:
    from codebase_rag.editing.imports import _py_rewrite

    move = SymbolMove(symbol="helper", old_module="pkg.util", new_module="pkg.helpers")
    # `helper, helper as h` binds the symbol twice; keeping only the first
    # would drop the `h` binding and break its use sites. This is main's
    # behaviour and the port must adopt it.
    assert _py_rewrite("from pkg.util import helper, helper as h, other", move) == (
        "from pkg.util import other\nfrom pkg.helpers import helper, helper as h"
    )


def test_same_module_rename_keeps_each_binding_its_own_local_name() -> None:
    from pathlib import Path as _Path

    from codebase_rag.editing.imports import _js_rewrite, _py_rewrite, _rs_rewrite

    # The SAME-module branches are the ones the cross-module tests never reach:
    # a rename keeps one statement and rewrites entries in place, so each
    # binding must keep its own local name. Mapping the moved entries
    # positionally (`moved_entries[0]` for every match, as the pre-merge branch
    # did) collapses `helper as h` onto `helper`, binding one name twice and
    # losing the other -- and every other test in this file stays green.
    move = SymbolMove("helper", "pkg.util", "pkg.util", new_name="renamed")
    assert (
        _py_rewrite("from pkg.util import helper, helper as h", move)
        == "from pkg.util import renamed as helper, renamed as h"
    )

    js = SymbolMove("helper", "./m", "./m", new_name="renamed", new_module_path="m.ts")
    assert (
        _js_rewrite("import { helper, helper as h } from './m';", js, _Path("a.ts"))
        == "import { renamed as helper, renamed as h } from './m';"
    )

    rs = SymbolMove("helper", "m", "m", new_name="renamed")
    assert (
        _rs_rewrite("use m::{helper, helper as h};", rs)
        == "use m::{renamed as helper, renamed as h};"
    )
