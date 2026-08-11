from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])

# One FLOWS_TO edge as (from_qn, to_qn, properties).
FlowEdge = tuple[str, str, dict[str, str]]


def _run_flow(
    tmp_path: Path,
    files: dict[str, str],
    capture: CaptureSelection = _CAPTURE_IO,
) -> list[FlowEdge]:
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
        capture=capture,
    ).run()
    edges: list[FlowEdge] = []
    for c in mock.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != FLOWS_TO:
            continue
        props = c.kwargs.get("properties")
        if props is None and len(c.args) > 3:
            props = c.args[3]
        edges.append((c.args[0][2], c.args[2][2], dict(props or {})))
    return edges


def _node_qns(mock: MagicMock) -> set[str]:
    return {
        c.args[1].get(cs.KEY_QUALIFIED_NAME)
        for c in mock.ensure_node_batch.call_args_list
        if len(c.args) >= 2
    }


def _has(edges: list[FlowEdge], frm: str, to: str, **props: str) -> bool:
    return any(
        a.endswith(frm)
        and b.endswith(to)
        and all(p.get(k) == v for k, v in props.items())
        for a, b, p in edges
    )


def test_resource_to_resource_env_to_stdout(tmp_path: Path) -> None:
    files = {"m.py": "import os\n\ndef leak():\n    x = os.getenv('K')\n    print(x)\n"}
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_tainted_positional_arg_flows_to_callee(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def helper(v):\n    pass\n\n"
            "def caller():\n    t = os.getenv('K')\n    helper(t)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.caller", "m.helper", via="arg:0", kind=FlowKind.ARG.value)


def test_tainted_keyword_arg_flows_to_callee(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def helper(v):\n    pass\n\n"
            "def caller():\n    t = os.getenv('K')\n    helper(v=t)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.caller", "m.helper", via="kw:v", kind=FlowKind.ARG.value)


def test_return_value_flows_from_callee_to_caller(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    return os.getenv('K')\n\n"
            "def caller():\n    v = build()\n    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.build", "m.caller", via="return", kind=FlowKind.RETURN.value)


def test_direct_return_of_tainted_callee_emits_return_edge(tmp_path: Path) -> None:
    # `return inner()` consumes inner's tainted return just as `v = inner()`
    # does, so the callee->caller return edge must fire at the direct-return
    # site too, not only at assignment sites.
    files = {
        "m.py": (
            "import os\n\n"
            "def inner():\n    return os.getenv('K')\n\n"
            "def outer():\n    return inner()\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.inner", "m.outer", via="return", kind=FlowKind.RETURN.value)


def test_return_parenthesized_tainted_value_emits_flow(tmp_path: Path) -> None:
    # A returned value wrapped in parentheses is still the same tainted value;
    # the return edge and downstream resource flow must survive the wrapper.
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    return (os.getenv('K'))\n\n"
            "def caller():\n    v = build()\n    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.build", "m.caller", via="return", kind=FlowKind.RETURN.value)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_return_tuple_with_tainted_element_emits_flow(tmp_path: Path) -> None:
    # `return a, b` wraps the values in an expression_list; a tainted element
    # must still be seen as a returned tainted value.
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    t = os.getenv('K')\n    return t, 1\n\n"
            "def caller():\n    v = build()\n    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.build", "m.caller", via="return", kind=FlowKind.RETURN.value)


def test_return_taint_reaches_resource_sink(tmp_path: Path) -> None:
    # A value returned from a tainted callee carries its source resource, so a
    # later sink emits the full resource->resource flow, not just the return edge.
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    return os.getenv('K')\n\n"
            "def caller():\n    v = build()\n    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_taint_propagates_through_plain_assignment(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def leak():\n    a = os.getenv('K')\n    b = a\n    print(b)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_untainted_arg_emits_no_flow(tmp_path: Path) -> None:
    # Co-occurrence of a read source and an unrelated call is not flow.
    files = {
        "m.py": (
            "import os\n\n"
            "def helper(v):\n    pass\n\n"
            "def caller():\n    u = 1\n    helper(u)\n    os.getenv('K')\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not any(b.endswith("m.helper") for _, b, _ in edges)


def test_overwrite_with_literal_kills_taint(tmp_path: Path) -> None:
    # Reassigning a tainted local to a safe literal kills its taint; the later
    # sink must not emit a resource->resource flow (no stale-taint false positive).
    files = {
        "m.py": (
            "import os\n\n"
            "def leak():\n    x = os.getenv('K')\n    x = 'safe'\n    print(x)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not any(
        a.endswith("resource::ENV::K") and b.endswith("resource::STDOUT::<dynamic>")
        for a, b, _ in edges
    )


def test_overwrite_with_untainted_name_kills_taint(tmp_path: Path) -> None:
    # Reassigning a tainted local from an untainted variable also kills taint.
    files = {
        "m.py": (
            "import os\n\n"
            "def leak():\n    x = os.getenv('K')\n    y = 1\n    x = y\n    print(x)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not any(
        a.endswith("resource::ENV::K") and b.endswith("resource::STDOUT::<dynamic>")
        for a, b, _ in edges
    )


def test_default_capture_emits_no_flow(tmp_path: Path) -> None:
    files = {"m.py": "import os\n\ndef leak():\n    x = os.getenv('K')\n    print(x)\n"}
    edges = _run_flow(tmp_path, files, capture=resolve_capture([]))
    assert edges == []


def test_flow_only_capture_still_ensures_resource_nodes(tmp_path: Path) -> None:
    # FLOWS_TO enabled, READS_FROM/WRITES_TO dropped: the resource endpoints of a
    # FLOWS_TO edge must still be ensured so no edge dangles to a missing node.
    capture = resolve_capture(
        [cs.CAPTURE_TOKEN_NONE, f"{cs.CAPTURE_ADD_PREFIX}{FLOWS_TO}"]
    )
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    (tmp_path / "m.py").write_text(
        "import os\n\ndef leak():\n    x = os.getenv('K')\n    print(x)\n",
        encoding="utf-8",
    )
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run()
    node_qns = _node_qns(mock)
    assert "resource::ENV::K" in node_qns
    assert "resource::STDOUT::<dynamic>" in node_qns


def test_multiple_returns_distinct_sources_all_flow(tmp_path: Path) -> None:
    # A callee returning DIFFERENT tainted sources on different branches must
    # carry ALL origins to the caller's sink, not just the first-seen one.
    files = {
        "m.py": (
            "import os\n\n"
            "def build(flag):\n"
            "    if flag:\n"
            "        return os.getenv('A')\n"
            "    return os.getenv('B')\n\n"
            "def caller(flag):\n"
            "    v = build(flag)\n"
            "    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::A",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )
    assert _has(
        edges,
        "resource::ENV::B",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_through_logging_wrapper(tmp_path: Path) -> None:
    # The canonical case (issue #1142): a secret handed to a logging wrapper
    # whose parameter is logged. The source and the sink live in different
    # bodies, so without forward parameter taint the ENV read never connects to
    # the STDOUT sink and the "secrets in logs" query is a false negative.
    files = {
        "m.py": (
            "import os\n\n"
            "def log_it(msg):\n    print(msg)\n\n"
            "def caller():\n    secret = os.getenv('K')\n    log_it(secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_direct_positional_source_expression(tmp_path: Path) -> None:
    # The source is written inline as the argument, not bound to a local first:
    # log_it(os.getenv('K')). The argument expression must be evaluated, or the
    # parameter-sink composition never sees the origin (CodeRabbit review, #1167).
    files = {
        "m.py": (
            "import os\n\n"
            "def log_it(msg):\n    print(msg)\n\n"
            "def caller():\n    log_it(os.getenv('K'))\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_direct_keyword_source_expression(tmp_path: Path) -> None:
    # Same, passed by keyword: log_it(msg=os.getenv('K')).
    files = {
        "m.py": (
            "import os\n\n"
            "def log_it(msg):\n    print(msg)\n\n"
            "def caller():\n    log_it(msg=os.getenv('K'))\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_through_keyword_argument(tmp_path: Path) -> None:
    # The parameter is matched by keyword name, not position.
    files = {
        "m.py": (
            "import os\n\n"
            "def log_it(msg):\n    print(msg)\n\n"
            "def caller():\n    secret = os.getenv('K')\n    log_it(msg=secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_through_two_wrapper_hops(tmp_path: Path) -> None:
    # A wrapper of a wrapper: the parameter is handed on to a second callee that
    # sinks it. The parameter-sink closure must compose transitively.
    files = {
        "m.py": (
            "import os\n\n"
            "def inner(x):\n    print(x)\n\n"
            "def log_it(msg):\n    inner(msg)\n\n"
            "def caller():\n    secret = os.getenv('K')\n    log_it(secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_wrapper_defined_after_caller(tmp_path: Path) -> None:
    # The wrapper is defined AFTER the caller, so its parameter-sink summary is
    # only known at finalize; the forward/cross-body composition must still fire.
    files = {
        "m.py": (
            "import os\n\n"
            "def caller():\n    secret = os.getenv('K')\n    log_it(secret)\n\n"
            "def log_it(msg):\n    print(msg)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_wrapper_across_files(tmp_path: Path) -> None:
    # Source in one module, logging wrapper in another: the summary crosses the
    # file boundary exactly like the return-direction fixpoint.
    files = {
        "wrap.py": "def log_it(msg):\n    print(msg)\n",
        "m.py": (
            "import os\n\n"
            "from wrap import log_it\n\n"
            "def caller():\n    secret = os.getenv('K')\n    log_it(secret)\n"
        ),
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_from_pending_argument(tmp_path: Path) -> None:
    # The argument is itself the return of a tainted callee (pending at the call
    # site); its origins resolve at finalize and must then reach the wrapper's
    # sink.
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    return os.getenv('K')\n\n"
            "def log_it(msg):\n    print(msg)\n\n"
            "def caller():\n    secret = build()\n    log_it(secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_positional_arg_not_bound_to_keyword_only_param(
    tmp_path: Path,
) -> None:
    # def target(prefix, *args, sink): print(sink). A second POSITIONAL argument
    # is absorbed by *args, not bound to the keyword-only `sink`; positional
    # mapping must stop at the variadic so no phantom ENV->STDOUT flow appears
    # (Greptile review on PR #1167).
    files = {
        "m.py": (
            "import os\n\n"
            "def target(prefix, *args, sink):\n    print(sink)\n\n"
            "def caller():\n    secret = os.getenv('K')\n    target('p', secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_keyword_only_param_composes_by_keyword(tmp_path: Path) -> None:
    # The keyword-only parameter is still reached when passed by keyword, so the
    # variadic guard does not suppress the genuine flow.
    files = {
        "m.py": (
            "import os\n\n"
            "def target(prefix, *args, sink):\n    print(sink)\n\n"
            "def caller():\n"
            "    secret = os.getenv('K')\n    target('p', sink=secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_positional_only_separator_param(tmp_path: Path) -> None:
    # A `/` positional-only marker does not consume a position: `sink` after it
    # is still positionally bound, so arg:1 must map to it and the flow appears
    # (CodeRabbit review on PR #1167).
    files = {
        "m.py": (
            "import os\n\n"
            "def target(prefix, /, sink):\n    print(sink)\n\n"
            "def caller():\n    secret = os.getenv('K')\n    target('p', secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_comment_between_parameters(tmp_path: Path) -> None:
    # A comment node between parameters must not shift positional mapping.
    files = {
        "m.py": (
            "import os\n\n"
            "def target(prefix,  # first\n           sink):\n    print(sink)\n\n"
            "def caller():\n    secret = os.getenv('K')\n    target('p', secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_comment_between_arguments(tmp_path: Path) -> None:
    # A comment node between call arguments must not shift the argument index.
    files = {
        "m.py": (
            "import os\n\n"
            "def target(prefix, sink):\n    print(sink)\n\n"
            "def caller():\n"
            "    secret = os.getenv('K')\n"
            "    target('p',  # note\n           secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_negative_control_param_never_sinks(tmp_path: Path) -> None:
    # A wrapper whose parameter never reaches a sink must NOT invent a resource
    # flow: NO_FLOW stays trustworthy. The arg edge still records the hand-off.
    files = {
        "m.py": (
            "import os\n\n"
            "def keep(msg):\n    store = msg\n\n"
            "def caller():\n    secret = os.getenv('K')\n    keep(secret)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )
    assert _has(edges, "m.caller", "m.keep", via="arg:0", kind=FlowKind.ARG.value)


def test_param_taint_untainted_argument_emits_nothing(tmp_path: Path) -> None:
    # A clean argument into a sinking wrapper produces no resource flow: the
    # parameter-sink summary only fires for arguments that actually carry taint.
    files = {
        "m.py": (
            "def log_it(msg):\n    print(msg)\n\ndef caller():\n    log_it('literal')\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not any(
        a.startswith("resource::") and b.endswith("STDOUT::<dynamic>")
        for a, b, _ in edges
    )


def _has_env_k_to_stdout_flow(edges: list[FlowEdge]) -> bool:
    return _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_passthrough_return_positional(tmp_path: Path) -> None:
    # A pass-through helper (issue #1168): redact returns its parameter, so a
    # secret routed through it must still reach the later sink. Without
    # parameter-to-return, redact's return summary is empty and this is a false
    # NO_FLOW.
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return v\n\n"
            "def caller():\n"
            "    secret = os.getenv('K')\n    y = redact(secret)\n    print(y)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_return_transitive(tmp_path: Path) -> None:
    # wrap returns redact(x): the parameter reaches wrap's return through a
    # second pass-through, which the return-param closure must compose.
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return v\n\n"
            "def wrap(x):\n    return redact(x)\n\n"
            "def caller():\n    y = wrap(os.getenv('K'))\n    print(y)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_across_files(tmp_path: Path) -> None:
    # The pass-through helper lives in another module; the summary crosses the
    # file boundary like the return-taint fixpoint.
    files = {
        "helpers.py": "def redact(v):\n    return v\n",
        "m.py": (
            "import os\n\n"
            "from helpers import redact\n\n"
            "def caller():\n"
            "    secret = os.getenv('K')\n    y = redact(secret)\n    print(y)\n"
        ),
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_defined_after_caller(tmp_path: Path) -> None:
    # The helper is defined after the caller, so its return-param status is only
    # known at finalize; the forward composition must still fire.
    files = {
        "m.py": (
            "import os\n\n"
            "def caller():\n"
            "    secret = os.getenv('K')\n    y = redact(secret)\n    print(y)\n\n"
            "def redact(v):\n    return v\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_keyword_argument(tmp_path: Path) -> None:
    # The pass-through argument is passed by keyword.
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return v\n\n"
            "def caller():\n    y = redact(v=os.getenv('K'))\n    print(y)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_pending_argument(tmp_path: Path) -> None:
    # The argument into the pass-through is itself a tainted callee return
    # (pending); its origins resolve at finalize and then flow through redact.
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    return os.getenv('K')\n\n"
            "def redact(v):\n    return v\n\n"
            "def caller():\n    y = redact(build())\n    print(y)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_returned_not_sunk(tmp_path: Path) -> None:
    # The pass-through result is never sunk, so no resource->sink flow may be
    # invented (a redact->caller return edge may legitimately exist).
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return v\n\n"
            "def caller():\n    secret = os.getenv('K')\n    y = redact(secret)\n"
        )
    }
    assert not _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_return_chain_resolves_through_fixpoint(tmp_path: Path) -> None:
    # A three-deep return chain (a -> b -> c, c reads the source) resolves only
    # by re-queueing callers as each callee's origins land — exercises the return
    # summary worklist fixpoint end to end.
    files = {
        "m.py": (
            "import os\n\n"
            "def c():\n    return os.getenv('K')\n\n"
            "def b():\n    return c()\n\n"
            "def a():\n    return b()\n\n"
            "def caller():\n    x = a()\n    print(x)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_return_keyword_handoff(tmp_path: Path) -> None:
    # The transitive hand-off passes the parameter by keyword: `return redact(v=x)`.
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return v\n\n"
            "def wrap(x):\n    return redact(v=x)\n\n"
            "def caller():\n    y = wrap(os.getenv('K'))\n    print(y)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_return_parenthesized_param(tmp_path: Path) -> None:
    # The returned call's argument wraps the parameter in parentheses.
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return v\n\n"
            "def wrap(x):\n    return redact((x))\n\n"
            "def caller():\n    y = wrap(os.getenv('K'))\n    print(y)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_return_conditional_param(tmp_path: Path) -> None:
    # A conditional argument unions both branch params; the tainted one reaches
    # the callee's parameter and composes.
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return v\n\n"
            "def wrap(x, y, flag):\n    return redact(x if flag else y)\n\n"
            "def caller():\n"
            "    y = wrap(os.getenv('K'), 'clean', True)\n    print(y)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_return_boolean_param(tmp_path: Path) -> None:
    # A short-circuit `x or default` argument carries the parameter's taint.
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return v\n\n"
            "def wrap(x):\n    return redact(x or 'd')\n\n"
            "def caller():\n    y = wrap(os.getenv('K'))\n    print(y)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_passthrough_calls_do_not_cross_contaminate(
    tmp_path: Path,
) -> None:
    # A pass-through helper invoked once with a secret must NOT taint a separate
    # call of the same helper with a clean argument: the composition is per call
    # site, not aggregated by callee (Greptile P1 on PR #1170). Here only the
    # clean call is sunk, so no ENV->STDOUT flow may appear.
    files = {
        "m.py": (
            "import os\n\n"
            "def passthrough(v):\n    return v\n\n"
            "def caller():\n"
            "    passthrough(os.getenv('K'))\n"
            "    print(passthrough('clean'))\n"
        )
    }
    assert not _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def _has_go_secret_to_stdout_flow(edges: list[FlowEdge]) -> bool:
    return _has(
        edges,
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_go_through_logging_wrapper(tmp_path: Path) -> None:
    # The canonical case (issue #1142) in a lean-walk language (issue #1169):
    # Go forward parameter taint. The source and the STDOUT sink live in
    # different function bodies, so without seeding the parameter as a
    # pseudo-origin the ENV read never connects to fmt.Println.
    files = {
        "main.go": (
            "package main\n\n"
            'import (\n\t"fmt"\n\t"os"\n)\n\n'
            "func logIt(msg string) {\n\tfmt.Println(msg)\n}\n\n"
            "func caller() {\n"
            '\tsecret := os.Getenv("SECRET")\n'
            "\tlogIt(secret)\n}\n"
        )
    }
    assert _has_go_secret_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_go_two_wrapper_hops(tmp_path: Path) -> None:
    # A wrapper of a wrapper: the parameter-to-sink summary must compose
    # transitively through _param_flow_edges so the secret still reaches STDOUT.
    files = {
        "main.go": (
            "package main\n\n"
            'import (\n\t"fmt"\n\t"os"\n)\n\n'
            "func inner(x string) {\n\tfmt.Println(x)\n}\n\n"
            "func logIt(msg string) {\n\tinner(msg)\n}\n\n"
            "func caller() {\n"
            '\tsecret := os.Getenv("SECRET")\n'
            "\tlogIt(secret)\n}\n"
        )
    }
    assert _has_go_secret_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_go_wrapper_defined_after_caller(tmp_path: Path) -> None:
    # The wrapper is defined AFTER its caller: composition happens at finalize,
    # once every body's parameter-sink summary is known, so source order is
    # irrelevant.
    files = {
        "main.go": (
            "package main\n\n"
            'import (\n\t"fmt"\n\t"os"\n)\n\n'
            "func caller() {\n"
            '\tsecret := os.Getenv("SECRET")\n'
            "\tlogIt(secret)\n}\n\n"
            "func logIt(msg string) {\n\tfmt.Println(msg)\n}\n"
        )
    }
    assert _has_go_secret_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_go_wrapper_across_files(tmp_path: Path) -> None:
    # The wrapper and the caller live in different files of the same package;
    # the parameter-sink summary is keyed by qualified name, so it crosses the
    # file boundary at finalize.
    files = {
        "log.go": (
            "package main\n\n"
            'import "fmt"\n\n'
            "func logIt(msg string) {\n\tfmt.Println(msg)\n}\n"
        ),
        "caller.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func caller() {\n"
            '\tsecret := os.Getenv("SECRET")\n'
            "\tlogIt(secret)\n}\n"
        ),
    }
    assert _has_go_secret_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_go_negative_control_param_never_sinks(tmp_path: Path) -> None:
    # The wrapper ignores its parameter and logs a constant, so no
    # parameter-to-sink summary exists and the secret must not reach STDOUT.
    files = {
        "main.go": (
            "package main\n\n"
            'import (\n\t"fmt"\n\t"os"\n)\n\n'
            'func logIt(msg string) {\n\tfmt.Println("static")\n}\n\n'
            "func caller() {\n"
            '\tsecret := os.Getenv("SECRET")\n'
            "\tlogIt(secret)\n}\n"
        )
    }
    assert not _has_go_secret_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_go_untainted_argument_emits_nothing(tmp_path: Path) -> None:
    # A clean literal handed to a sinking wrapper must not invent an ENV origin:
    # the parameter is seeded, but no call site folds a tainted argument in.
    files = {
        "main.go": (
            "package main\n\n"
            'import "fmt"\n\n'
            "func logIt(msg string) {\n\tfmt.Println(msg)\n}\n\n"
            'func caller() {\n\tlogIt("clean")\n}\n'
        )
    }
    assert not _has_go_secret_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_js_through_logging_wrapper(tmp_path: Path) -> None:
    # The lean forward-taint path in JavaScript (issue #1169), exercising the
    # js_ts parameter-name extractor: a file read handed to a console.log
    # wrapper must connect FILE to STDOUT across the two bodies.
    files = {
        "m.js": (
            'const fs = require("fs");\n\n'
            "function logIt(msg) {\n  console.log(msg);\n}\n\n"
            "function caller() {\n"
            '  const secret = fs.readFileSync("cfg.txt");\n'
            "  logIt(secret);\n}\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::FILE::cfg.txt",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_cpp_through_cout_stream_wrapper(tmp_path: Path) -> None:
    # C++ forward taint through a std::cout stream sink (issue #1169). This
    # exercises the cpp parameter-name extractor AND the _emit_taint_to_sink
    # stream path, which records the parameter-sink for the stream branch.
    files = {
        "main.cpp": (
            "#include <cstdlib>\n"
            "#include <iostream>\n"
            "void logIt(const char* msg) {\n    std::cout << msg;\n}\n\n"
            "void caller() {\n"
            '    const char* secret = getenv("SECRET");\n'
            "    logIt(secret);\n}\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_go_positional_reordering(tmp_path: Path) -> None:
    # The positional-mapping contract (CodeRabbit review on PR #1193): an outer
    # wrapper forwards its parameters to an inner callee in SWAPPED order, and
    # only the inner's second parameter sinks. The secret must follow its actual
    # argument position (logIt.x -> inner.b -> sink), which only holds if arg
    # indices map to the right slot at every hop.
    files = {
        "main.go": (
            "package main\n\n"
            'import (\n\t"fmt"\n\t"os"\n)\n\n'
            "func inner(a string, b string) {\n\tfmt.Println(b)\n}\n\n"
            "func logIt(x string, y string) {\n\tinner(y, x)\n}\n\n"
            "func caller() {\n"
            '\tsecret := os.Getenv("SECRET")\n'
            '\tlogIt(secret, "safe")\n}\n'
        )
    }
    assert _has_go_secret_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_go_variadic_maps_trailing_args(tmp_path: Path) -> None:
    # An argument PAST the start of a variadic slot must map to the variadic
    # parameter (issue #1169): secret is the third argument but the callee has a
    # `...string` variadic at index 1, so it binds to `rest` and reaches the
    # sink. A dense list without variadic metadata would drop it (index out of
    # range), a false negative.
    files = {
        "main.go": (
            "package main\n\n"
            'import (\n\t"fmt"\n\t"os"\n)\n\n'
            "func logIt(prefix string, rest ...string) {\n\tfmt.Println(rest)\n}\n\n"
            "func caller() {\n"
            '\tsecret := os.Getenv("SECRET")\n'
            '\tlogIt("p", "a", secret)\n}\n'
        )
    }
    assert _has_go_secret_to_stdout_flow(_run_flow(tmp_path, files))


def test_param_taint_cpp_unnamed_slot_does_not_shift(tmp_path: Path) -> None:
    # A leading UNNAMED parameter must occupy its own slot so a later named,
    # sink-bearing parameter keeps its true index (Greptile P1 on PR #1193).
    # Here the secret is passed into the unnamed slot (arg:0), which binds no
    # name and must NOT be mapped to `msg` — a compacting extractor would shift
    # `msg` to index 0 and emit a false ENV->STDOUT edge.
    files = {
        "main.cpp": (
            "#include <cstdlib>\n"
            "#include <iostream>\n"
            "void logIt(const char*, const char* msg) {\n    std::cout << msg;\n}\n\n"
            "void caller() {\n"
            '    const char* secret = getenv("SECRET");\n'
            '    logIt(secret, "safe");\n}\n'
        )
    }
    assert not _has(
        _run_flow(tmp_path, files),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_cpp_unnamed_leading_param_recovers_flow(tmp_path: Path) -> None:
    # The mirror of the shift test: the secret IS passed into the named,
    # sink-bearing parameter (arg:1), past a leading unnamed slot. Keeping the
    # unnamed slot as None preserves index 1 for `msg`, so the real flow is
    # recovered (a compacting extractor would drop it as out of range).
    files = {
        "main.cpp": (
            "#include <cstdlib>\n"
            "#include <iostream>\n"
            "void logIt(const char*, const char* msg) {\n    std::cout << msg;\n}\n\n"
            "void caller() {\n"
            '    const char* secret = getenv("SECRET");\n'
            '    logIt("safe", secret);\n}\n'
        )
    }
    assert _has(
        _run_flow(tmp_path, files),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_js_destructured_slot_does_not_shift(tmp_path: Path) -> None:
    # A leading DESTRUCTURING pattern binds no positional name and must occupy
    # its own slot (CodeRabbit / Greptile on PR #1193). The file read is passed
    # into that slot (arg:0), so it must NOT be mapped to the later `msg`
    # parameter, which would emit a false FILE->STDOUT edge.
    files = {
        "m.js": (
            'const fs = require("fs");\n\n'
            "function logIt({a}, msg) {\n  console.log(msg);\n}\n\n"
            "function caller() {\n"
            '  const secret = fs.readFileSync("cfg.txt");\n'
            '  logIt(secret, "safe");\n}\n'
        )
    }
    assert not _has(
        _run_flow(tmp_path, files),
        "resource::FILE::cfg.txt",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_ts_this_parameter_does_not_shift(tmp_path: Path) -> None:
    # A TypeScript `this` pseudo-parameter is type-only, not a runtime argument
    # (CodeRabbit review on PR #1193). The first real argument must map to `msg`
    # (index 0 after `this` is skipped), so the file read reaches the sink; a
    # helper that counted `this` as a slot would shift `msg` and drop the flow.
    files = {
        "m.ts": (
            'const fs = require("fs");\n\n'
            "function logIt(this: Ctx, msg: string) {\n  console.log(msg);\n}\n\n"
            "function caller() {\n"
            '  const secret = fs.readFileSync("cfg.txt");\n'
            "  logIt(secret);\n}\n"
        )
    }
    assert _has(
        _run_flow(tmp_path, files),
        "resource::FILE::cfg.txt",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_ts_typed_rest_binds_trailing_args(tmp_path: Path) -> None:
    # End-to-end: a TypeScript typed rest parameter (`...vals: string[]`) must
    # keep its name and variadic position after the typed-pattern unwrap, so a
    # trailing tainted argument binds to `vals` and the wrapper's sink emits the
    # flow (Greptile review on PR #1193). The file read is the third argument,
    # past the variadic slot at index 1.
    files = {
        "m.ts": (
            'const fs = require("fs");\n\n'
            "function logIt(prefix: string, ...vals: string[]) {\n"
            "  console.log(vals);\n}\n\n"
            "function caller() {\n"
            '  const secret = fs.readFileSync("cfg.txt");\n'
            '  logIt("p", secret);\n}\n'
        )
    }
    assert _has(
        _run_flow(tmp_path, files),
        "resource::FILE::cfg.txt",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_param_taint_passthrough_returns_fresh_value_no_flow(tmp_path: Path) -> None:
    # Negative control: the helper returns a fresh value, not its parameter, so
    # no parameter-to-return relationship exists and the secret does not reach
    # the sink.
    files = {
        "m.py": (
            "import os\n\n"
            "def redact(v):\n    return 'clean'\n\n"
            "def caller():\n"
            "    secret = os.getenv('K')\n    y = redact(secret)\n    print(y)\n"
        )
    }
    assert not _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_capture_taint_reaches_sink_in_nested_function(tmp_path: Path) -> None:
    # Closure capture (issue #1197): the nested `send` closes over the tainted
    # `token`, which is neither its parameter nor its local. Its body is walked as
    # its own caller, so without capture seeding the ENV read never connects to the
    # STDOUT sink -- the "secrets captured into a callback" false negative.
    files = {
        "m.py": (
            "import os\n\n"
            "def handler():\n"
            "    token = os.getenv('K')\n"
            "    def send():\n        print(token)\n"
            "    send()\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_capture_taint_survives_reassignment_after_the_def(tmp_path: Path) -> None:
    # MAY semantics (issue #1197): the capture is recorded from the def-site state,
    # where `token` is tainted. A closure captures a cell, and the walk does not model
    # call order, so the tool cannot assume the later reassignment precedes every
    # invocation -- the closure may run with the tainted value. Reporting the flow is
    # the safe over-approximation (no false negative); precise call-relative cell
    # tracking is a separate follow-up.
    files = {
        "m.py": (
            "import os\n\n"
            "def handler():\n"
            "    token = os.getenv('K')\n"
            "    def send():\n        print(token)\n"
            "    token = 'clean'\n"
            "    send()\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_capture_taint_composes_through_two_nested_levels(tmp_path: Path) -> None:
    # `token` is captured by `middle` and again by `inner`; the qn-keyed capture
    # summaries compose transitively, exactly like a two-hop parameter wrapper.
    files = {
        "m.py": (
            "import os\n\n"
            "def handler():\n"
            "    token = os.getenv('K')\n"
            "    def middle():\n"
            "        def inner():\n            print(token)\n"
            "        inner()\n"
            "    middle()\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_untainted_captured_variable_emits_no_flow(tmp_path: Path) -> None:
    # Negative control: the captured `token` holds a literal, so the free-variable
    # seed is never a real capture and no flow is emitted.
    files = {
        "m.py": (
            "def handler():\n"
            "    token = 'literal'\n"
            "    def send():\n        print(token)\n"
            "    send()\n"
        )
    }
    assert not _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_for_loop_local_binding_is_not_a_capture(tmp_path: Path) -> None:
    # `token` is bound by the nested `for`, so it is the closure's own local, not a
    # capture of the enclosing tainted `token`; no flow (CodeRabbit review, #1197).
    files = {
        "m.py": (
            "import os\n\n"
            "def handler():\n"
            "    token = os.getenv('K')\n"
            "    def send():\n"
            "        for token in ('clean',):\n            print(token)\n"
            "    send()\n"
        )
    }
    assert not _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_import_local_binding_is_not_a_capture(tmp_path: Path) -> None:
    # A nested `import token` binds `token` locally; it must not be classified as a
    # capture of the enclosing tainted `token`.
    files = {
        "m.py": (
            "import os\n\n"
            "def handler():\n"
            "    token = os.getenv('K')\n"
            "    def send():\n        import token\n        print(token)\n"
            "    send()\n"
        )
    }
    assert not _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_with_and_except_as_bindings_are_not_captures(tmp_path: Path) -> None:
    # `with ... as token` and `except ... as token` both bind `token` locally.
    for body in (
        "        with open('x') as token:\n            print(token)\n",
        "        try:\n            pass\n"
        "        except Exception as token:\n            print(token)\n",
    ):
        files = {
            "m.py": (
                "import os\n\n"
                "def handler():\n"
                "    token = os.getenv('K')\n"
                "    def send():\n" + body + "    send()\n"
            )
        }
        assert not _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_match_value_pattern_does_not_hide_a_capture(tmp_path: Path) -> None:
    # `case sentinel.token:` is a value pattern (multi-part dotted name) that binds
    # nothing; the `token` read in the case body is the enclosing capture, so the
    # flow must survive -- collecting value-pattern identifiers would hide it
    # (CodeRabbit review, #1197).
    files = {
        "m.py": (
            "import os\n\n"
            "sentinel = object()\n\n"
            "def handler():\n"
            "    token = os.getenv('K')\n"
            "    def send(x):\n"
            "        match x:\n            case sentinel.token:\n"
            "                print(token)\n"
            "    send(1)\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_match_capture_pattern_binds_locally_no_flow(tmp_path: Path) -> None:
    # `case token:` is a capture pattern binding `token` locally, so it is the
    # closure's own binding, not a capture of the enclosing tainted `token`.
    files = {
        "m.py": (
            "import os\n\n"
            "def handler():\n"
            "    token = os.getenv('K')\n"
            "    def send(x):\n"
            "        match x:\n            case token:\n"
            "                print(token)\n"
            "    send(1)\n"
        )
    }
    assert not _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))


def test_capture_composes_for_duplicate_named_nested_defs(tmp_path: Path) -> None:
    # Two nested defs share the name `send`; the definition pass suffixes their qns.
    # The capture must be recorded under the SAME registered qn the redefined `send`
    # is walked with, or its flow is lost (Greptile review, #1197).
    files = {
        "m.py": (
            "import os\n\n"
            "def factory():\n"
            "    token = os.getenv('K')\n"
            "    def send():\n        print('safe')\n"
            "    def send():\n        print(token)\n"
            "    send()\n"
        )
    }
    assert _has_env_k_to_stdout_flow(_run_flow(tmp_path, files))
