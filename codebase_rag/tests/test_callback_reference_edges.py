from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    # (H) Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
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
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _has(
    rels: set[tuple[str, str, str]], caller_suffix: str, rel: str, callee_suffix: str
) -> bool:
    return any(
        a.endswith(caller_suffix) and r == rel and b.endswith(callee_suffix)
        for a, r, b in rels
    )


REFERENCES = cs.RelationshipType.REFERENCES.value


def test_callback_kwarg_to_first_party_function_is_referenced(tmp_path: Path) -> None:
    # (H) A nested function passed by keyword to a FIRST-PARTY callee that stores it
    # (H) (never invokes it) must get a REFERENCES edge from the passing scope, or
    # (H) dead-code wrongly flags it (with_brrr_from_cfg/create_context shape).
    files = {
        "brrrlib.py": (
            "def with_brrr_from_cfg(cfg, handlers, create_context=None):\n"
            "    cfg.ctx_factory = create_context\n"
            "    return cfg\n"
        ),
        "worker.py": (
            "from brrrlib import with_brrr_from_cfg\n\n"
            "def _main(cfg, handlers, extra):\n"
            "    def create_context(active_worker, meta):\n"
            "        return (active_worker, extra)\n"
            "    return with_brrr_from_cfg(cfg, handlers, create_context=create_context)\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "worker._main", REFERENCES, "worker._main.create_context")


def test_callback_kwarg_to_constructor_is_referenced(tmp_path: Path) -> None:
    # (H) A nested function passed into a first-party CONSTRUCTOR that stores it as a
    # (H) field must get a REFERENCES edge (AnteriorCodec/create_context shape).
    files = {
        "codec.py": (
            "class Codec:\n"
            "    def __init__(self, create_context=None):\n"
            "        self.create_context = create_context\n"
        ),
        "worker.py": (
            "from codec import Codec\n\n"
            "def get_local(topic, handlers, extra):\n"
            "    def create_context(active_worker, meta):\n"
            "        return (active_worker, extra)\n"
            "    return Codec(create_context=create_context)\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "worker.get_local", REFERENCES, "worker.get_local.create_context")


def test_callback_to_misbound_external_method_is_referenced(tmp_path: Path) -> None:
    # (H) df.apply is external (pandas), but a same-named first-party method makes the
    # (H) trie bind the call to it; the passed callback must STILL be referenced from
    # (H) the passing scope (build_suggestion shape).
    files = {
        "strategies.py": (
            "class BatchingStrategyBase:\n"
            "    def apply(self, batch):\n"
            "        return batch\n"
        ),
        "report.py": (
            "import pandas as pd\n\n"
            "def write_report(results):\n"
            "    df = pd.DataFrame(results)\n"
            "    def build_suggestion(row):\n"
            "        return row\n"
            "    df['suggestions'] = df.apply(build_suggestion, axis=1)\n"
            "    return df\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels, "report.write_report", REFERENCES, "report.write_report.build_suggestion"
    )


def test_plain_argument_is_not_referenced(tmp_path: Path) -> None:
    # (H) Non-callable arguments (locals, literals) must not produce REFERENCES noise.
    files = {
        "lib.py": "def consume(x, y):\n    return x\n",
        "m.py": (
            "from lib import consume\n\n"
            "def use(data):\n"
            "    limit = 3\n"
            "    return consume(data, limit)\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert not any(r == REFERENCES for _, r, _ in rels)


def test_function_assigned_to_local_variable_is_referenced(tmp_path: Path) -> None:
    # (H) A nested function assigned to a local (http_callback =
    # (H) llm_http_task_closure_with_context) is referenced even if never called by
    # (H) name in this scope; the alias may be stored or passed onward for dynamic
    # (H) dispatch, so the assignment itself must keep the function reachable.
    files = {
        "svc.py": (
            "import os\n\n"
            "def get_llm_service(config, http_callback=None):\n"
            "    def llm_http_task_closure_with_context(llm, method, url):\n"
            "        return (config, llm, method, url)\n"
            "    if http_callback:\n"
            "        pass\n"
            "    else:\n"
            "        http_callback = llm_http_task_closure_with_context\n"
            "    return http_callback\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels,
        "svc.get_llm_service",
        REFERENCES,
        "svc.get_llm_service.llm_http_task_closure_with_context",
    )


def test_function_assigned_to_object_attribute_is_referenced(tmp_path: Path) -> None:
    # (H) A nested function monkeypatched onto an object attribute (mock_client.post
    # (H) = handle_post) is invoked later through that attribute; the assignment must
    # (H) reference it (MockHTTPRouter shape). Its body's self-call then keeps the
    # (H) called method reachable transitively.
    files = {
        "mocking.py": (
            "class MockHTTPRouter:\n"
            "    def _handle_post(self, url, **kwargs):\n"
            "        return url\n"
            "    def create_mock_client(self, mock_client):\n"
            "        async def handle_post(url, **kwargs):\n"
            "            return self._handle_post(url, **kwargs)\n"
            "        mock_client.post = handle_post\n"
            "        return mock_client\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels,
        "mocking.MockHTTPRouter.create_mock_client",
        REFERENCES,
        "create_mock_client.handle_post",
    )
    assert _has(
        rels,
        "create_mock_client.handle_post",
        cs.RelationshipType.CALLS.value,
        "MockHTTPRouter._handle_post",
    )
