# (H) A platform-conditional import with a local fallback definition (click's
# (H) `if WIN: from ._winconsole import _get_windows_console_stream ... else:
# (H) def _get_windows_console_stream(...)`) is statically undecidable: the call
# (H) resolves only to the imported target, so the mutually-exclusive local
# (H) fallback def looks dead. Like Go build variants, fan the call out to BOTH --
# (H) exactly one exists at runtime, and either may be it.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_conditional_import_local_fallback_gets_call_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "pyfall"
    root.mkdir(parents=True)
    (root / "_winconsole.py").write_text(
        "def helper(stream):\n    return stream\n",
        encoding="utf-8",
    )
    (root / "_compat.py").write_text(
        "import sys\n"
        "def get_text_stdin():\n"
        "    return helper(sys.stdin)\n"
        "if sys.platform.startswith('win'):\n"
        "    from ._winconsole import helper\n"
        "else:\n"
        "    def helper(stream):\n"
        "        return None\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing=None)
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        f.endswith(".get_text_stdin") and t.endswith("._winconsole.helper")
        for f, t in calls
    ), sorted(t for f, t in calls if "helper" in t)
    assert any(
        f.endswith(".get_text_stdin") and t.endswith("._compat.helper")
        for f, t in calls
    ), sorted(t for f, t in calls if "helper" in t)


def test_unconditional_import_shadowing_gets_no_fanout(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) An UNCONDITIONAL import that shadows an earlier local def is plain
    # (H) shadowing, not a platform variant: the local def is genuinely dead and
    # (H) must NOT receive a fan-out edge. The fan-out is gated to names bound by
    # (H) a CONDITIONAL (if/try-nested) import.
    root = temp_repo / "pyshad"
    root.mkdir(parents=True)
    (root / "other.py").write_text(
        "def helper(stream):\n    return stream\n",
        encoding="utf-8",
    )
    (root / "user.py").write_text(
        "def helper(stream):\n"
        "    return None\n"
        "from .other import helper\n"
        "def use():\n"
        "    return helper(1)\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing=None)
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert not any(
        f.endswith(".use") and t.endswith(".user.helper") for f, t in calls
    ), "shadowed local def wrongly kept alive by the fan-out"
