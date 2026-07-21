# A Python method overriding a method of an EXTERNAL STDLIB base class
# (click's `class TextWrapper(textwrap.TextWrapper)` overriding `_wrap_chunks`,
# `_handle_long_word`) is invoked by the stdlib parent's machinery
# (`wrap()` calls `self._wrap_chunks(...)`), never by first-party code, so it
# reported dead. The base's method set IS knowable (stdlib import), so mark such
# methods `overrides_external` -- the dead-code surfaces root the property.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater


def _method_props(mock_ingestor: MagicMock) -> dict[str, dict]:
    return {
        c.args[1][cs.KEY_QUALIFIED_NAME]: c.args[1]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.METHOD
    }


def test_stdlib_base_override_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "pyext"
    root.mkdir(parents=True)
    (root / "_textwrap.py").write_text(
        "import textwrap\n"
        "class TextWrapper(textwrap.TextWrapper):\n"
        "    def _wrap_chunks(self, chunks):\n"
        "        return chunks\n"
        "    def not_on_base(self):\n"
        "        return None\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing=None)
    props = _method_props(mock_ingestor)
    wrap = next(v for k, v in props.items() if k.endswith("._wrap_chunks"))
    other = next(v for k, v in props.items() if k.endswith(".not_on_base"))
    assert wrap.get(cs.KEY_OVERRIDES_EXTERNAL) is True, wrap
    assert not other.get(cs.KEY_OVERRIDES_EXTERNAL), other
