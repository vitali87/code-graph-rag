"""A rename rewrites the strings that spell the symbol's dotted path.

`mock.patch("pkg.core.helper")` and a `[project.scripts]` entry point
`"pkg.core:main"` name a definition by its import path. The rename rewrote
every code site and left those strings, so the test patching `helper` raised
`AttributeError` and the console script pointed at a name that no longer
existed, while the report said `applied` and `verdict.ok` (issue #2810).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import rename
from codebase_rag.tests.test_rename_op import _index, _write

_CORE = (
    "def helper(x):\n    return x + 1\n\n\n"
    "def main():\n    return helper(1)\n\n\n"
    "class Store:\n    def get(self):\n        return 1\n"
)
_TEST = (
    "from unittest import mock\n\n"
    "from pkg.core import main\n\n\n"
    "def test_main_uses_helper():\n"
    '    with mock.patch("pkg.core.helper", return_value=41):\n'
    "        assert main() == 41\n"
)
_PYPROJECT = (
    '[project]\nname = "renstr"\nversion = "0.1.0"\n\n'
    '[project.scripts]\nrenstr = "pkg.core:main"\n'
)


def _repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        _write(root, rel, text)
    return root


def _rename(root: Path, mock: MagicMock, qn_tail: str, new_name: str) -> None:
    graph = _index(root, mock)
    report = rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.{qn_tail}",
        new_name,
        allow_heuristic=True,
    )
    assert report.applied, report.message


def _run_test(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.test_core import test_main_uses_helper as t; t()",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )


@pytest.fixture
def repo(temp_repo: Path) -> Path:
    return _repo(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": _CORE,
            "tests/__init__.py": "",
            "tests/test_core.py": _TEST,
            "pyproject.toml": _PYPROJECT,
        },
    )


def test_a_patched_dotted_path_follows_the_rename(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    _rename(repo, mock_ingestor, "pkg.core.helper", "add_one")
    test = (repo / "tests" / "test_core.py").read_text()
    assert 'mock.patch("pkg.core.add_one", return_value=41)' in test, test
    result = _run_test(repo)
    assert result.returncode == 0, result.stderr


def test_an_entry_point_follows_the_rename(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    _rename(repo, mock_ingestor, "pkg.core.main", "run_main")
    assert 'renstr = "pkg.core:run_main"' in (repo / "pyproject.toml").read_text()


@pytest.mark.parametrize(
    ("files", "qn_tail", "new_name", "rel", "expected"),
    [
        (
            {
                "setup.cfg": "[options.entry_points]\nconsole_scripts =\n    renstr = pkg.core:main\n"
            },
            "pkg.core.main",
            "run_main",
            "setup.cfg",
            "renstr = pkg.core:run_main",
        ),
        (
            {"tests/test_store.py": "PATCH = 'pkg.core.Store.get'\n"},
            "pkg.core.Store.get",
            "fetch",
            "tests/test_store.py",
            "PATCH = 'pkg.core.Store.fetch'",
        ),
    ],
    ids=["setup_cfg_entry_point", "method_path"],
)
def test_other_spellings_follow_the_rename(
    repo: Path,
    mock_ingestor: MagicMock,
    files: dict[str, str],
    qn_tail: str,
    new_name: str,
    rel: str,
    expected: str,
) -> None:
    _repo(repo, files)
    _rename(repo, mock_ingestor, qn_tail, new_name)
    assert expected in (repo / rel).read_text()


def test_a_src_layout_path_is_its_import_path(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = _repo(
        temp_repo,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": _CORE,
            "tests/test_core.py": 'TARGET = "pkg.core.helper"\n',
        },
    )
    _rename(root, mock_ingestor, "src.pkg.core.helper", "add_one")
    assert (
        'TARGET = "pkg.core.add_one"' in (root / "tests" / "test_core.py").read_text()
    )


def test_other_strings_are_left_alone(repo: Path, mock_ingestor: MagicMock) -> None:
    # Negatives: a longer path that only starts with the name's, a path to
    # another module, a bare name and prose stay exactly as written.
    untouched = (
        'A = "pkg.core.helper_v2"\n'
        'B = "other.core.helper"\n'
        'C = "helper"\n'
        'D = "see pkg.core.helper for details"\n'
        'E = "xpkg.core.helper"\n'
    )
    _repo(repo, {"tests/strings.py": untouched})
    _rename(repo, mock_ingestor, "pkg.core.helper", "add_one")
    assert (repo / "tests" / "strings.py").read_text() == untouched


def test_a_path_inside_a_longer_value_is_left_alone(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negatives: the path quoted inside a longer string, in a comment, or as
    # prose inside a config value is not the value itself.
    untouched_py = (
        "MSG = \"Do not call 'pkg.core.helper' directly\"\n"
        '# mock.patch("pkg.core.helper") is how the tests patch it\n'
    )
    untouched_cfg = "[metadata]\ndescription = see pkg.core:main for details\n"
    _repo(
        repo,
        {
            "tests/messages.py": untouched_py,
            "pyproject.toml": _PYPROJECT
            + '\n[tool.renstr]\nnote = "see pkg.core:main for details"\n',
            "setup.cfg": untouched_cfg,
        },
    )
    _rename(repo, mock_ingestor, "pkg.core.main", "run_main")
    pyproject = (repo / "pyproject.toml").read_text()
    assert 'renstr = "pkg.core:run_main"' in pyproject, pyproject
    assert 'note = "see pkg.core:main for details"' in pyproject, pyproject
    assert (repo / "setup.cfg").read_text() == untouched_cfg
    _rename(repo, mock_ingestor, "pkg.core.helper", "add_one")
    assert (repo / "tests" / "messages.py").read_text() == untouched_py


def test_another_app_with_the_same_import_path_is_left_alone(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `pkg.core.helper` is a path of both apps; only app a's strings are
    # app a's helper.
    test_file = 'TARGET = "pkg.core.helper"\n'
    pyproject = '[project.scripts]\nrun = "pkg.core:helper"\n'
    root = _repo(
        temp_repo,
        {
            "apps/a/pkg/__init__.py": "",
            "apps/a/pkg/core.py": _CORE,
            "apps/a/tests/test_core.py": test_file,
            "apps/a/pyproject.toml": pyproject,
            "apps/b/pkg/__init__.py": "",
            "apps/b/pkg/core.py": _CORE,
            "apps/b/tests/test_core.py": test_file,
            "apps/b/pyproject.toml": pyproject,
        },
    )
    _rename(root, mock_ingestor, "apps.a.pkg.core.helper", "add_one")
    a, b = root / "apps" / "a", root / "apps" / "b"
    assert 'TARGET = "pkg.core.add_one"' in (a / "tests" / "test_core.py").read_text()
    assert '"pkg.core:add_one"' in (a / "pyproject.toml").read_text()
    assert (b / "tests" / "test_core.py").read_text() == test_file
    assert (b / "pyproject.toml").read_text() == pyproject


def test_an_undecodable_byte_before_the_path_keeps_its_offset(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # A Latin-1 `é` before the entry point: a decode that replaced it would
    # shift every later offset by two bytes and refuse the rename.
    (repo / "setup.cfg").write_bytes(
        b"[metadata]\nauthor = Ren\xe9\n\n[options.entry_points]\n"
        b"console_scripts =\n    renstr = pkg.core:main\n"
    )
    _rename(repo, mock_ingestor, "pkg.core.main", "run_main")
    assert b"renstr = pkg.core:run_main" in (repo / "setup.cfg").read_bytes()


def test_a_symlinked_file_is_patched_once(repo: Path, mock_ingestor: MagicMock) -> None:
    _repo(repo, {"tests/target.py": 'TARGET = "pkg.core.helper"\n'})
    try:
        (repo / "tests" / "alias.py").symlink_to("target.py")
    except OSError:
        pytest.skip("symlinks are not available here")
    _rename(repo, mock_ingestor, "pkg.core.helper", "add_one")
    assert (repo / "tests" / "target.py").read_text() == 'TARGET = "pkg.core.add_one"\n'
