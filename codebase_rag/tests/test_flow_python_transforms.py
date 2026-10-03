# Python taint through non-first-party transforms (issue #2588): a tainted
# value passed through a builtin (`float(raw)`), a method on the value
# (`t.strip()`), or string building (f-string, `%`, `.format`, `+`, `join`)
# keeps its origin, the way an unknown function propagates taint in a taint
# engine. A short list of calls whose result reveals nothing of their input
# (`len`, predicates, one-way digests) still clears it, and first-party
# callees keep their summary-based model instead of becoming pass-throughs.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.flow_verdict import (
    CYPHER_FLOW_COVERAGE_GAPS,
    CYPHER_FLOW_EDGES,
    CYPHER_FLOW_REMOTE_EDGES,
    FLOW_VERDICT_FOUND,
    FLOW_VERDICT_NO_FLOW,
    flow_reachability_verdict,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind
from codebase_rag.types_defs import PropertyParams, ResultRow

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])

FlowEdge = tuple[str, str, dict[str, str]]


def _run_flow(
    tmp_path: Path,
    files: dict[str, str],
    capture: CaptureSelection = _CAPTURE_IO,
) -> list[FlowEdge]:
    parsers, queries = load_parsers()
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
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


def _has(edges: list[FlowEdge], frm: str, to: str, **props: str) -> bool:
    return any(
        a.endswith(frm)
        and b.endswith(to)
        and all(p.get(k) == v for k, v in props.items())
        for a, b, p in edges
    )


def _arg_edge(edges: list[FlowEdge], frm: str, to: str) -> bool:
    return _has(edges, frm, to, kind=FlowKind.ARG.value, via="arg:0")


def _return_edge(edges: list[FlowEdge], frm: str, to: str) -> bool:
    return _has(edges, frm, to, kind=FlowKind.RETURN.value, via="return")


def _env_to_stdout(edges: list[FlowEdge], key: str = "API_TOKEN") -> bool:
    return _has(
        edges,
        f"resource::ENV::{key}",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


# The issue's repro 1: a secret read from the environment reaches `emit` (a
# `print` wrapper) plainly, through a method on the value, and through an
# f-string.
_REPRO_PREAMBLE = (
    "import os\n\n"
    "def secret():\n"
    "    return os.getenv('API_TOKEN')\n\n"
    "def emit(v):\n"
    "    print(v)\n\n"
)
_REPRO_BODIES = {
    "handler_plain": "    emit(t)\n",
    "handler_strip": "    emit(t.strip())\n",
    "handler_fmt": "    emit(f'token={t}')\n",
}


def _repro(*handlers: str) -> dict[str, str]:
    defs = "".join(
        f"def {name}():\n    t = secret()\n{_REPRO_BODIES[name]}\n" for name in handlers
    )
    return {"app.py": _REPRO_PREAMBLE + defs}


_REPRO_HANDLERS = _repro(*_REPRO_BODIES)


def _handler(body: str) -> dict[str, str]:
    # One handler that reads the secret into `t`, then runs `body`.
    return {
        "app.py": (
            "import hashlib\nimport os\n\n"
            "def emit(v):\n"
            "    print(v)\n\n"
            "def handler():\n"
            "    t = os.getenv('API_TOKEN')\n"
            f"{body}"
        )
    }


def test_method_on_tainted_value_reaches_callee(tmp_path: Path) -> None:
    edges = _run_flow(tmp_path, _REPRO_HANDLERS)
    assert _arg_edge(edges, "app.handler_strip", "app.emit")


def test_fstring_of_tainted_value_reaches_callee(tmp_path: Path) -> None:
    edges = _run_flow(tmp_path, _REPRO_HANDLERS)
    assert _arg_edge(edges, "app.handler_fmt", "app.emit")


@pytest.mark.parametrize("handler", ["handler_strip", "handler_fmt"])
def test_transformed_secret_reaches_the_sink_inside_the_callee(
    tmp_path: Path, handler: str
) -> None:
    # The resource flow composes through emit's parameter-to-sink summary,
    # so the ENV -> STDOUT leak is visible, not only the call edge.
    assert _env_to_stdout(_run_flow(tmp_path, _repro(handler)))


def _verdict_fetch(edges: list[FlowEdge]):
    rows = [{"source": a, "target": b} for a, b, _ in edges]

    def fetch_all(query: str, params: PropertyParams | None = None) -> list[ResultRow]:
        if query == CYPHER_FLOW_EDGES:
            return list(rows)
        if query in (CYPHER_FLOW_COVERAGE_GAPS, CYPHER_FLOW_REMOTE_EDGES):
            return []
        return []

    return fetch_all


@pytest.mark.parametrize("handler", ["handler_strip", "handler_fmt"])
def test_flow_verdict_finds_the_transformed_flow(tmp_path: Path, handler: str) -> None:
    # Before the fix this answered NO_FLOW with no gaps: a "verified absence"
    # for a real source -> sink flow.
    edges = _run_flow(tmp_path, _REPRO_HANDLERS)
    emit_qn = next(b for _a, b, _p in edges if b.endswith("app.emit"))
    project = emit_qn.removesuffix(".app.emit")
    result = flow_reachability_verdict(
        _verdict_fetch(edges),
        project,
        f"{project}.app.{handler}",
        emit_qn,
    )
    assert result.verdict == FLOW_VERDICT_FOUND
    assert result.path == (f"{project}.app.{handler}", emit_qn)


# The issue's repro 2: a function returning a transformed env value hands
# that taint back to its caller.
_REPRO_RETURNS = (
    "import os\n\n"
    "def via_builtin():\n"
    "    raw = os.getenv('K')\n"
    "    return float(raw)\n\n"
    "def via_method():\n"
    "    raw = os.getenv('K')\n"
    "    return raw.strip()\n\n"
    "def via_fstring():\n"
    "    raw = os.getenv('K')\n"
    "    return f'k={raw}'\n\n"
    "def use():\n"
    "    return via_builtin(), via_method(), via_fstring()\n"
)


@pytest.mark.parametrize("fn", ["via_builtin", "via_method", "via_fstring"])
def test_returned_transform_emits_return_edge(tmp_path: Path, fn: str) -> None:
    edges = _run_flow(tmp_path, {"flow.py": _REPRO_RETURNS})
    assert _return_edge(edges, f"flow.{fn}", "flow.use")


def test_crash_shape_inline_conversion_of_env_read_emits_return_edge(
    tmp_path: Path,
) -> None:
    # The traceback shape from the issue: the env read is converted inline in
    # the returned expression, so rank_root_causes had no flow to rank with.
    files = {
        "pricing.py": (
            "import os\n\n"
            "def load_rate():\n"
            "    return float(os.environ.get('TAX_RATE', ''))\n\n"
            "def apply_tax(amount):\n"
            "    rate = load_rate()\n"
            "    return amount * rate\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _return_edge(edges, "pricing.load_rate", "pricing.apply_tax")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("    emit('token=%s' % t)\n", id="percent"),
        pytest.param("    emit('%s:%s' % ('k', t))\n", id="percent-tuple"),
        pytest.param("    emit('token={}'.format(t))\n", id="format"),
        pytest.param("    emit('token=' + t)\n", id="concat"),
        pytest.param("    emit(', '.join(['a', t]))\n", id="join-list"),
        pytest.param("    emit(''.join(c for c in t))\n", id="join-generator"),
        pytest.param("    emit(c for c in t)\n", id="generator-argument"),
        pytest.param("    emit(f\"{'':{t}>10}\")\n", id="fstring-format-spec"),
        pytest.param(
            "    emit(f\"{'':{'*':{t}}>10}\")\n", id="fstring-nested-format-spec"
        ),
        pytest.param("    emit(t.strip().lower().encode())\n", id="method-chain"),
        pytest.param("    emit(str(t))\n", id="builtin-str"),
        pytest.param("    emit(int(t))\n", id="builtin-int"),
        pytest.param("    emit(t[:4])\n", id="slice"),
        pytest.param("    emit({'token': t})\n", id="dict-value"),
        pytest.param("    t = t.strip()\n    emit(t)\n", id="rebind-to-transform"),
        pytest.param("    s = 'token='\n    s += t\n    emit(s)\n", id="aug-assign"),
    ],
)
def test_string_building_and_conversions_keep_taint(tmp_path: Path, body: str) -> None:
    edges = _run_flow(tmp_path, _handler(body))
    assert _arg_edge(edges, "app.handler", "app.emit")
    assert _env_to_stdout(edges)


def test_transform_inside_a_direct_sink_call(tmp_path: Path) -> None:
    files = {
        "app.py": (
            "import os\n\n"
            "def leak():\n"
            "    t = os.getenv('API_TOKEN')\n"
            "    print(f'token={t.strip()}')\n"
        )
    }
    assert _env_to_stdout(_run_flow(tmp_path, files))


def test_awaited_library_call_on_a_secret_keeps_taint(tmp_path: Path) -> None:
    files = {
        "app.py": (
            "import os\n\n"
            "def emit(v):\n"
            "    print(v)\n\n"
            "async def handler(client):\n"
            "    t = os.getenv('API_TOKEN')\n"
            "    r = await client.sign(t)\n"
            "    emit(r)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _arg_edge(edges, "app.handler", "app.emit")


def test_generated_long_concatenation_does_not_abort_the_file(
    tmp_path: Path,
) -> None:
    # A generated `t + t + ...` is as deep as it is long; evaluating it one
    # stack frame per operand would overflow and drop the file's call
    # processing, so the flow (and every other edge of the file) would vanish.
    chain = " + ".join(["t"] * 600)
    files = {
        "app.py": (
            "import os\n\n"
            "def leak():\n"
            "    t = os.getenv('API_TOKEN')\n"
            f"    x = {chain}\n"
            "    print(x)\n"
        )
    }
    assert _env_to_stdout(_run_flow(tmp_path, files))


def test_parameter_reaches_sink_through_a_transform(tmp_path: Path) -> None:
    # The wrapper transforms its parameter before the sink; the call site
    # passing a secret still composes into ENV -> STDOUT.
    files = {
        "app.py": (
            "import os\n\n"
            "def log_it(m):\n"
            "    print(m.upper())\n\n"
            "def caller():\n"
            "    log_it(os.getenv('API_TOKEN'))\n"
        )
    }
    assert _env_to_stdout(_run_flow(tmp_path, files))


def test_parameter_returned_through_a_transform_is_a_pass_through(
    tmp_path: Path,
) -> None:
    files = {
        "app.py": (
            "import os\n\n"
            "def clean(v):\n"
            "    return v.strip()\n\n"
            "def caller():\n"
            "    y = clean(os.getenv('API_TOKEN'))\n"
            "    print(y)\n"
        )
    }
    assert _env_to_stdout(_run_flow(tmp_path, files))


@pytest.mark.parametrize(
    "arg",
    [
        pytest.param("[c for c in p]", id="list-comprehension"),
        pytest.param("{k: v for k, v in p}", id="dict-comprehension"),
        pytest.param("c for c in p", id="bare-generator"),
    ],
)
def test_parameter_returned_through_a_comprehension_is_a_pass_through(
    tmp_path: Path, arg: str
) -> None:
    # `wrap` hands its parameter to a first-party callee inside a
    # comprehension; the returned call must still record `p` as reaching
    # `redact`, or the caller's secret stops at `wrap`.
    files = {
        "app.py": (
            "import os\n\n"
            "def redact(v):\n"
            "    return ''.join(v)\n\n"
            "def wrap(p):\n"
            f"    return redact({arg})\n\n"
            "def caller():\n"
            "    y = wrap(os.getenv('API_TOKEN'))\n"
            "    print(y)\n"
        )
    }
    assert _env_to_stdout(_run_flow(tmp_path, files))


# Negative controls.


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "    t = t.strip()\n    t = 'fixed'\n    emit(t)\n", id="constant"
        ),
        pytest.param(
            "    t = f'{t}'\n    t = 'x' + 'y'\n    emit(t)\n", id="clean-concat"
        ),
        pytest.param("    emit(f'static')\n", id="static-fstring"),
        pytest.param(
            "    emit(f\"{'':>{len(t)}}\")\n", id="format-spec-of-cleared-value"
        ),
        pytest.param("    emit(str(42))\n", id="clean-builtin"),
        pytest.param("    emit(t == 'x')\n", id="comparison"),
        pytest.param("    emit(not t)\n", id="not"),
    ],
)
def test_value_replaced_or_unrelated_does_not_flow(tmp_path: Path, body: str) -> None:
    edges = _run_flow(tmp_path, _handler(body))
    assert not _arg_edge(edges, "app.handler", "app.emit")
    assert not _env_to_stdout(edges)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("    emit(len(t))\n", id="len"),
        pytest.param("    emit(isinstance(t, str))\n", id="isinstance"),
        pytest.param("    emit(t.startswith('sk-'))\n", id="predicate-method"),
        pytest.param("    emit(t.count('-'))\n", id="count-method"),
        pytest.param(
            "    emit(hashlib.sha256(t.encode()).hexdigest())\n", id="sha256-digest"
        ),
        pytest.param(
            "    d = hashlib.sha256(t.encode())\n    emit(d.hexdigest())\n",
            id="digest-object",
        ),
    ],
)
def test_sanitizer_clears_the_taint(tmp_path: Path, body: str) -> None:
    edges = _run_flow(tmp_path, _handler(body))
    assert not _arg_edge(edges, "app.handler", "app.emit")
    assert not _env_to_stdout(edges)


# A str lookup name (`find`, `index`, `count`, ...) clears its argument only
# on a receiver known to be a string. On any other object it is that object's
# own method, and its result may carry the argument it was given.
_LOOKUPS_WITH_AN_ARGUMENT = (
    "find",
    "rfind",
    "index",
    "rindex",
    "count",
    "startswith",
    "endswith",
)


def _client_handler(call: str) -> dict[str, str]:
    return {
        "app.py": (
            "import os\n"
            "import external_client\n\n"
            "def handler():\n"
            "    secret = os.getenv('API_TOKEN')\n"
            "    client = external_client.Client()\n"
            f"    print({call})\n"
        )
    }


@pytest.mark.parametrize("method", _LOOKUPS_WITH_AN_ARGUMENT)
def test_lookup_on_an_object_keeps_its_argument_taint(
    tmp_path: Path, method: str
) -> None:
    edges = _run_flow(tmp_path, _client_handler(f"client.{method}(secret)"))
    assert _env_to_stdout(edges)


def test_lookup_on_an_untyped_parameter_reaches_the_callee(tmp_path: Path) -> None:
    files = {
        "app.py": (
            "import os\n\n"
            "def emit(v):\n"
            "    print(v)\n\n"
            "def handler(store):\n"
            "    t = os.getenv('API_TOKEN')\n"
            "    emit(store.find(t))\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _arg_edge(edges, "app.handler", "app.emit")
    assert _env_to_stdout(edges)


@pytest.mark.parametrize("method", _LOOKUPS_WITH_AN_ARGUMENT)
@pytest.mark.parametrize(
    "receiver",
    [
        pytest.param("'sk-live-abc'", id="str-literal"),
        pytest.param("f'sk-{1}'", id="fstring"),
        pytest.param("label", id="annotated-str-local"),
    ],
)
def test_lookup_on_a_known_string_clears_its_argument(
    tmp_path: Path, receiver: str, method: str
) -> None:
    files = _handler(
        f"    label: str = external_client.name()\n    emit({receiver}.{method}(t))\n"
    )
    files["app.py"] = "import external_client\n" + files["app.py"]
    edges = _run_flow(tmp_path, files)
    assert not _arg_edge(edges, "app.handler", "app.emit")
    assert not _env_to_stdout(edges)


def test_lookup_on_a_parameter_annotated_str_clears_its_argument(
    tmp_path: Path,
) -> None:
    files = {
        "app.py": (
            "import os\n\n"
            "def handler(label: str):\n"
            "    t = os.getenv('API_TOKEN')\n"
            "    print(label.find(t))\n"
        )
    }
    assert not _env_to_stdout(_run_flow(tmp_path, files))


def test_sanitizer_result_is_not_resurrected_by_a_later_transform(
    tmp_path: Path,
) -> None:
    edges = _run_flow(tmp_path, _handler("    n = len(t)\n    emit(f'len={n}')\n"))
    assert not _arg_edge(edges, "app.handler", "app.emit")


def test_first_party_callee_is_not_a_pass_through(tmp_path: Path) -> None:
    # A resolved first-party function keeps its summary: one that returns a
    # fresh value does not carry its argument's taint, even though an
    # unresolved callee in the same position would.
    files = {
        "app.py": (
            "import os\n\n"
            "def fresh(v):\n"
            "    return 'clean'\n\n"
            "def emit(v):\n"
            "    print(v)\n\n"
            "def handler():\n"
            "    t = os.getenv('API_TOKEN')\n"
            "    emit(fresh(t))\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not _arg_edge(edges, "app.handler", "app.emit")
    assert not _env_to_stdout(edges)


def test_first_party_method_named_like_a_builtin_keeps_its_summary(
    tmp_path: Path,
) -> None:
    # `strip` resolves to the project's own function, which drops its
    # argument, so the call is analysed as that function, not as str.strip.
    files = {
        "app.py": (
            "import os\n\n"
            "def strip(v):\n"
            "    return ''\n\n"
            "def emit(v):\n"
            "    print(v)\n\n"
            "def handler():\n"
            "    t = os.getenv('API_TOKEN')\n"
            "    emit(strip(t))\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not _arg_edge(edges, "app.handler", "app.emit")


def test_plain_first_party_flow_is_unchanged(tmp_path: Path) -> None:
    edges = _run_flow(tmp_path, _REPRO_HANDLERS)
    assert _arg_edge(edges, "app.handler_plain", "app.emit")
    assert _return_edge(edges, "app.secret", "app.handler_plain")
    assert _return_edge(edges, "app.secret", "app.handler_strip")
    assert _return_edge(edges, "app.secret", "app.handler_fmt")


def test_verdict_between_unconnected_functions_is_still_no_flow(
    tmp_path: Path,
) -> None:
    files = {
        "app.py": _REPRO_HANDLERS["app.py"]
        + "def handler_len():\n    t = secret()\n    emit(len(t))\n"
    }
    edges = _run_flow(tmp_path, files)
    emit_qn = next(b for _a, b, _p in edges if b.endswith("app.emit"))
    project = emit_qn.removesuffix(".app.emit")
    result = flow_reachability_verdict(
        _verdict_fetch(edges), project, f"{project}.app.handler_len", emit_qn
    )
    assert result.verdict == FLOW_VERDICT_NO_FLOW


def test_without_io_capture_no_flow_edges_are_emitted(tmp_path: Path) -> None:
    # FLOWS_TO stays opt-in: the default capture set does no flow work.
    edges = _run_flow(tmp_path, _REPRO_HANDLERS, capture=resolve_capture([]))
    assert edges == []


def test_rust_terminal_len_still_does_not_propagate(tmp_path: Path) -> None:
    # The lean walks keep their own allow-listed transparent methods; the
    # Python default does not leak into them.
    files = {
        "main.rs": (
            "fn leak() {\n"
            '    let s = std::env::var("API_TOKEN").unwrap();\n'
            "    let n = s.len();\n"
            '    println!("{}", n);\n'
            "}\n"
        )
    }
    assert not _env_to_stdout(_run_flow(tmp_path, files))
