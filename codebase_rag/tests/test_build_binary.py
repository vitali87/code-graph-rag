from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from build_binary import (
    _build_package_args,
    _get_treesitter_packages,
    build_binary,
    forbidden_bundle_entries,
)
from codebase_rag import constants as cs
from codebase_rag.constants import PyInstallerPackage


class TestGetTreesitterPackages:
    def test_extracts_treesitter_packages_from_pyproject(self) -> None:
        mock_pyproject = {
            "project": {
                "optional-dependencies": {
                    "treesitter-full": [
                        "tree-sitter-python>=0.23.6",
                        "tree-sitter-javascript>=0.23.1",
                        "tree-sitter-rust>=0.24.0",
                    ]
                }
            }
        }

        with patch("build_binary.tomllib.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == [
            "tree_sitter_python",
            "tree_sitter_javascript",
            "tree_sitter_rust",
        ]

    def test_handles_different_version_specifiers(self) -> None:
        mock_pyproject = {
            "project": {
                "optional-dependencies": {
                    "treesitter-full": [
                        "tree-sitter-python>=0.23.6",
                        "tree-sitter-go==0.23.4",
                        "tree-sitter-java<1.0.0",
                    ]
                }
            }
        }

        with patch("build_binary.tomllib.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == [
            "tree_sitter_python",
            "tree_sitter_go",
            "tree_sitter_java",
        ]

    def test_filters_non_treesitter_packages(self) -> None:
        mock_pyproject = {
            "project": {
                "optional-dependencies": {
                    "treesitter-full": [
                        "tree-sitter-python>=0.23.6",
                        "some-other-package>=1.0.0",
                        "tree-sitter-rust>=0.24.0",
                    ]
                }
            }
        }

        with patch("build_binary.tomllib.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == ["tree_sitter_python", "tree_sitter_rust"]

    def test_real_pyproject_yields_bare_module_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Negative test against the real file, not a mock: an exact pin such as
        # `tree-sitter-dart==0.1.0` must not leak its specifier into the name
        # PyInstaller is told to collect, or the grammar silently drops out of
        # the binary.
        monkeypatch.chdir(Path(__file__).parents[2])

        packages = _get_treesitter_packages()

        assert packages
        assert all(name.isidentifier() for name in packages), packages

    def test_returns_empty_list_when_no_treesitter_extra(self) -> None:
        mock_pyproject = {"project": {"optional-dependencies": {}}}

        with patch("build_binary.tomllib.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == []

    def test_returns_empty_list_when_no_optional_dependencies(self) -> None:
        mock_pyproject = {"project": {}}

        with patch("build_binary.tomllib.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == []


class TestBuildPackageArgs:
    def test_collect_all_only(self) -> None:
        pkg = PyInstallerPackage(name="rich", collect_all=True)
        args = _build_package_args(pkg)
        assert args == ["--collect-all", "rich"]

    def test_collect_data_only(self) -> None:
        pkg = PyInstallerPackage(name="mypackage", collect_data=True)
        args = _build_package_args(pkg)
        assert args == ["--collect-data", "mypackage"]

    def test_hidden_import_only(self) -> None:
        pkg = PyInstallerPackage(name="mypackage", hidden_import="secret_module")
        args = _build_package_args(pkg)
        assert args == ["--hidden-import", "secret_module"]

    def test_all_options_combined(self) -> None:
        pkg = PyInstallerPackage(
            name="pydantic_ai",
            collect_all=True,
            collect_data=True,
            hidden_import="pydantic_ai_slim",
        )
        args = _build_package_args(pkg)
        assert args == [
            "--collect-all",
            "pydantic_ai",
            "--collect-data",
            "pydantic_ai",
            "--hidden-import",
            "pydantic_ai_slim",
        ]

    def test_no_options_returns_empty_list(self) -> None:
        pkg = PyInstallerPackage(name="mypackage")
        args = _build_package_args(pkg)
        assert args == []


class TestBuildBinaryCommand:
    def test_copies_own_package_metadata_for_version_lookup(self) -> None:
        with patch("build_binary.subprocess.run") as mock_run:
            build_binary()

        cmd = mock_run.call_args.args[0]
        metadata_pair = [cs.PYINSTALLER_ARG_COPY_METADATA, cs.PACKAGE_NAME]
        assert any(cmd[i : i + 2] == metadata_pair for i in range(len(cmd) - 1)), cmd

    def test_excludes_gpl_readline_from_the_bundle(self) -> None:
        """The Linux interpreter's `readline` links GNU Readline (GPL-3.0).

        Released binaries up to v0.0.945 carried `libreadline.so.8` because
        guarded imports in `site`, `pdb` and `websockets.cli` made PyInstaller
        collect the module; excluding it keeps copyleft out of the executable.
        """
        with patch("build_binary.subprocess.run") as mock_run:
            build_binary()

        cmd = mock_run.call_args.args[0]
        exclude_pair = [cs.PYINSTALLER_ARG_EXCLUDE_MODULE, "readline"]
        assert any(cmd[i : i + 2] == exclude_pair for i in range(len(cmd) - 1)), cmd

    def test_darwin_spec_excludes_readline_too(self) -> None:
        spec = Path(__file__).resolve().parents[2] / "code-graph-rag-darwin-arm64.spec"

        assert "'readline'" in spec.read_text(encoding="utf-8")

    def test_excludes_sqlite3_from_the_bundle(self) -> None:
        """filelock 3.32 imports `sqlite3` for its read-write locks.

        Collecting it put `libsqlite3.so.0` / `sqlite3.dll` in the binary,
        which the third-party notice step refuses as an unlicensed library.
        """
        with patch("build_binary.subprocess.run") as mock_run:
            build_binary()

        cmd = mock_run.call_args.args[0]
        exclude_pair = [cs.PYINSTALLER_ARG_EXCLUDE_MODULE, "sqlite3"]
        assert any(cmd[i : i + 2] == exclude_pair for i in range(len(cmd) - 1)), cmd

    def test_darwin_spec_excludes_sqlite3_too(self) -> None:
        spec = Path(__file__).resolve().parents[2] / "code-graph-rag-darwin-arm64.spec"

        assert "'sqlite3'" in spec.read_text(encoding="utf-8")

    def test_filelock_still_locks_when_sqlite3_is_unavailable(
        self, tmp_path: Path
    ) -> None:
        """Excluding `sqlite3` must not break filelock, which the binary uses.

        `ReadWriteLock is None` is the known positive that the import was
        really blocked; with sqlite3 present it is a class.
        """
        script = (
            "import sys\n"
            "sys.modules['sqlite3'] = None\n"
            "import filelock\n"
            "assert filelock.ReadWriteLock is None, filelock.ReadWriteLock\n"
            "with filelock.FileLock(sys.argv[1]):\n"
            "    print('locked')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / "x.lock")],
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "locked"


class TestForbiddenBundleEntries:
    """The built archive is checked, not just the command that built it.

    Entry names are the ones measured on the CI Linux binary (#2189), so the
    patterns are tested against the shape PyInstaller actually writes.
    """

    LINUX_READLINE = [
        "libreadline.so.8",
        "python3.12/lib-dynload/readline.cpython-312-x86_64-linux-gnu.so",
    ]
    # macOS links the system libedit rather than GNU Readline, but the module
    # is excluded on every platform, so its presence is still a failed build.
    DARWIN_READLINE = ["python3.12/lib-dynload/readline.cpython-312-darwin.so"]
    LINUX_CLEAN = [
        "libssl.so.3",
        "libtinfo.so.6",
        "python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so",
        # A package whose name merely contains "readline" is not GNU Readline.
        "pyreadline3/__init__.py",
    ]

    def test_reports_readline_library_and_extension(self) -> None:
        found = forbidden_bundle_entries(self.LINUX_READLINE + self.LINUX_CLEAN)
        assert found == sorted(self.LINUX_READLINE)

    def test_reports_macos_readline_extension(self) -> None:
        found = forbidden_bundle_entries(self.DARWIN_READLINE + self.LINUX_CLEAN)
        assert found == self.DARWIN_READLINE

    def test_reports_untagged_readline_extensions(self) -> None:
        """Python also loads `readline.so` and `readline.abi3.so`, untagged."""
        untagged = ["readline.so", "python3.12/lib-dynload/readline.abi3.so"]

        found = forbidden_bundle_entries(untagged + self.LINUX_CLEAN)

        assert found == sorted(untagged)

    def test_clean_archive_reports_nothing(self) -> None:
        assert forbidden_bundle_entries(self.LINUX_CLEAN) == []

    def _build_with_entries(self, entries: list[str]) -> bool:
        with (
            patch("build_binary.subprocess.run"),
            patch("build_binary._archive_entries", return_value=entries),
            patch("build_binary.Path.exists", return_value=True),
            patch("build_binary.Path.stat") as mock_stat,
            patch("build_binary.os.chmod"),
        ):
            mock_stat.return_value.st_size = 1
            return build_binary()

    def test_build_fails_when_archive_carries_readline(self) -> None:
        assert self._build_with_entries(self.LINUX_READLINE) is False

    def test_build_succeeds_when_archive_is_clean(self) -> None:
        assert self._build_with_entries(self.LINUX_CLEAN) is True

    def test_windows_guard_reads_the_exe_pyinstaller_writes(self) -> None:
        """On Windows the built file is `dist/<name>.exe`, not `dist/<name>`.

        Checking the bare name found nothing there, so the guard never ran on
        the Windows build.
        """
        with (
            patch("build_binary.platform.system", return_value="Windows"),
            patch("build_binary.subprocess.run"),
            patch(
                "build_binary._archive_entries", return_value=self.LINUX_READLINE
            ) as mock_entries,
            patch("build_binary.Path.exists", return_value=True),
            patch("build_binary.Path.stat") as mock_stat,
            patch("build_binary.os.chmod"),
        ):
            mock_stat.return_value.st_size = 1
            assert build_binary() is False

        checked = mock_entries.call_args.args[0]
        assert checked.name.endswith(cs.WINDOWS_EXECUTABLE_SUFFIX)

    def test_a_missing_binary_fails_the_build(self) -> None:
        """Reporting success with no artifact would skip the guard entirely."""
        with (
            patch("build_binary.subprocess.run"),
            patch("build_binary.Path.exists", return_value=False),
            patch("build_binary._archive_entries") as mock_entries,
        ):
            assert build_binary() is False

        mock_entries.assert_not_called()
