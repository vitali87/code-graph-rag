# (H) Store-then-invoke gaps beyond keyword-arg bindings: a callable stored via a
# (H) POSITIONAL constructor argument, or stored under a DIFFERENT attribute name
# (H) than its parameter (self.ctx_factory = create_context), must still resolve
# (H) when the field is invoked (cfg.handler(), codec.ctx_factory()).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == "CALLS"
    }


def _has(calls: set[tuple[str, str]], caller_suffix: str, callee_suffix: str) -> bool:
    return any(
        a.endswith(caller_suffix) and b.endswith(callee_suffix) for a, b in calls
    )


def test_positional_init_arg_binds_to_stored_field(tmp_path: Path) -> None:
    # (H) Config(fn) passes the callback positionally; __init__ stores it under
    # (H) the same name, and run() invokes it through the field.
    files = {
        "config.py": (
            "class Config:\n"
            "    def __init__(self, handler):\n"
            "        self.handler = handler\n\n"
            "    def run(self):\n"
            "        return self.handler()\n"
        ),
        "app.py": (
            "from config import Config\n\n\n"
            "def on_event():\n"
            "    return 1\n\n\n"
            "def main():\n"
            "    cfg = Config(on_event)\n"
            "    return cfg.run()\n"
        ),
    }
    calls = _run_calls(tmp_path, files)
    assert _has(calls, "Config.run", "app.on_event")


def test_keyword_arg_stored_under_renamed_attribute(tmp_path: Path) -> None:
    # (H) The keyword name (create_context) differs from the stored attribute
    # (H) (ctx_factory); the invocation goes through the ATTRIBUTE name, so the
    # (H) binding must map param -> attr (the brrr/with_brrr_from_cfg shape).
    files = {
        "codec.py": (
            "class Codec:\n"
            "    def __init__(self, create_context=None):\n"
            "        self.ctx_factory = create_context\n\n"
            "    def decode(self):\n"
            "        return self.ctx_factory()\n"
        ),
        "worker.py": (
            "from codec import Codec\n\n\n"
            "def build_context():\n"
            "    return {}\n\n\n"
            "def get_codec():\n"
            "    return Codec(create_context=build_context)\n"
        ),
    }
    calls = _run_calls(tmp_path, files)
    assert _has(calls, "Codec.decode", "worker.build_context")


def test_positional_namedtuple_field_binds(tmp_path: Path) -> None:
    # (H) A NamedTuple/dataclass without __init__ takes its field order from the
    # (H) annotated class body; Spec(py_name) binds fetch_name positionally.
    files = {
        "spec.py": (
            "from typing import Callable, NamedTuple\n\n\n"
            "def py_name():\n"
            '    return "py"\n\n\n'
            "class Spec(NamedTuple):\n"
            "    fetch_name: Callable\n\n\n"
            "PY_SPEC = Spec(py_name)\n\n\n"
            "def use():\n"
            "    return PY_SPEC.fetch_name()\n"
        ),
    }
    calls = _run_calls(tmp_path, files)
    assert _has(calls, "spec.use", "spec.py_name")


def test_nested_helper_store_does_not_clobber_ctor_rename(tmp_path: Path) -> None:
    # (H) A `self.cb = handler` inside a nested helper in __init__ must NOT be
    # (H) recorded as the constructor-store rename: the real store is
    # (H) `self.handler = handler` in the __init__ body, and the field
    # (H) invocation goes through self.handler(). The rename walk must skip
    # (H) nested function/class scopes or the nested attr overrides the real one.
    files = {
        "config.py": (
            "class Config:\n"
            "    def __init__(self, handler):\n"
            "        self.handler = handler\n\n"
            "        def later():\n"
            "            self.cb = handler\n\n"
            "        self._later = later\n\n"
            "    def run(self):\n"
            "        return self.handler()\n"
        ),
        "app.py": (
            "from config import Config\n\n\n"
            "def on_event():\n"
            "    return 1\n\n\n"
            "def main():\n"
            "    cfg = Config(on_event)\n"
            "    return cfg.run()\n"
        ),
    }
    calls = _run_calls(tmp_path, files)
    assert _has(calls, "Config.run", "app.on_event")
