"""Indexing work grows linearly in the definitions of one file (issue #2453).

Several per-definition and per-call steps rescanned every sibling definition
of the file or class, so one generated file of N definitions cost O(N^2): a
40,000-function JavaScript file took 16.5 minutes. Each step now reads a
lookup table built once per parent, tree, scope or class.

The growth tests assert COMPLEXITY rather than wall-clock time, the way
`test_csharp_chain_resolution_is_linear` does, because a timing threshold is
flaky on shared CI while the work counted here is deterministic. The work is
the number of Python lines executed inside the module that holds the scan,
counted with `sys.monitoring` on that module's code objects only, so it
measures the scan whether it is written as a per-call walk (before) or as a
table build plus a lookup (after). Quadrupling N quadruples linear work and
multiplies quadratic work by sixteen; the bound of 6x sits between the two.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from unittest.mock import MagicMock

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag import function_registry
from codebase_rag.parsers import call_processor, definition_docstring, export_detection
from codebase_rag.parsers.csharp import type_inference as cs_type_inference
from codebase_rag.parsers.java import variable_analyzer
from codebase_rag.parsers.js_ts import type_inference as js_type_inference
from codebase_rag.tests.conftest import get_relationships, run_updater

_EXACT = cs.EdgeResolution.EXACT.value

SMALL = 50
LARGE = 200
# LARGE / SMALL is 4: linear work grows ~4x, quadratic work ~16x.
MAX_GROWTH = 6.0

_MONITOR_NAME = "cgr-linear-scan-counter"


class _LineCounter:
    def __init__(self) -> None:
        self.lines = 0

    def on_line(self, _code: CodeType, _line: int) -> None:
        self.lines += 1


def _module_code_objects(
    modules: tuple[ModuleType, ...], functions: frozenset[str] | None = None
) -> list[CodeType]:
    """Every code object defined in `modules`, nested and decorated ones too.

    Read off the modules' own namespaces (functions, and the methods of the
    classes they define) rather than `gc.get_objects()`, which would touch
    every live object, lazy library proxies included. With `functions`, only
    those named functions and the code nested in them.
    """
    files = {module.__file__ for module in modules}
    pending: list[CodeType] = []
    for module in modules:
        for value in vars(module).values():
            members = vars(value).values() if isinstance(value, type) else [value]
            for member in members:
                if isinstance(member, (staticmethod, classmethod)):
                    member = member.__func__
                if isinstance(member, property):
                    accessors = [member.fget, member.fset]
                else:
                    accessors = [inspect.unwrap(member)] if callable(member) else []
                pending.extend(
                    accessor.__code__
                    for accessor in accessors
                    if isinstance(accessor, FunctionType)
                )
    if functions is not None:
        pending = [code for code in pending if code.co_name in functions]
    seen: set[CodeType] = set()
    while pending:
        code = pending.pop()
        if code in seen or code.co_filename not in files:
            continue
        seen.add(code)
        pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
    return list(seen)


def _free_tool_id() -> int:
    for tool in range(2, 6):
        if sys.monitoring.get_tool(tool) is None:
            return tool
    pytest.skip("no free sys.monitoring tool id")


@contextmanager
def _lines_executed_in(
    *modules: ModuleType, functions: frozenset[str] | None = None
) -> Iterator[_LineCounter]:
    """Count the lines run inside `modules` while the block executes.

    Local events on those code objects only, so the rest of the indexer runs
    unmonitored; a dedicated tool id, so coverage's own tracing is untouched.
    """
    monitoring = sys.monitoring
    tool = _free_tool_id()
    codes = _module_code_objects(modules, functions)
    counter = _LineCounter()
    monitoring.use_tool_id(tool, _MONITOR_NAME)
    monitoring.register_callback(tool, monitoring.events.LINE, counter.on_line)
    for code in codes:
        monitoring.set_local_events(tool, code, monitoring.events.LINE)
    try:
        yield counter
    finally:
        for code in codes:
            monitoring.set_local_events(tool, code, monitoring.events.NO_EVENTS)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)


@dataclass(frozen=True)
class _Scan:
    """What one growth test indexes and where it counts the work."""

    source: Callable[[int], dict[str, str]]
    grammar: str
    modules: tuple[ModuleType, ...]
    functions: frozenset[str] | None = None


def _index(root: Path, files: dict[str, str], scan: _Scan, ingestor: MagicMock) -> int:
    """Index `files` under `root` into `ingestor`; the lines `scan` counts."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    ingestor.reset_mock()
    with _lines_executed_in(*scan.modules, functions=scan.functions) as counter:
        run_updater(root, ingestor, skip_if_missing=scan.grammar)
    return counter.lines


def _growth(tmp_path: Path, scan: _Scan, ingestor: MagicMock) -> tuple[int, int]:
    """The work at SMALL and at LARGE; `ingestor` keeps the LARGE graph."""
    small = _index(tmp_path / "small", scan.source(SMALL), scan, ingestor)
    large = _index(tmp_path / "large", scan.source(LARGE), scan, ingestor)
    assert small > 0, "fixture guard: the counted module never ran"
    return small, large


def _assert_linear(small: int, large: int, what: str) -> None:
    assert large < small * MAX_GROWTH, (
        f"{what} is superlinear: {small} lines at {SMALL} definitions, {large} "
        f"at {LARGE} (linear would be about {small * LARGE // SMALL})"
    )


def _calls_targets(ingestor: MagicMock) -> set[str]:
    return {str(c.args[2][2]) for c in get_relationships(ingestor, "CALLS")}


def _resolutions_into(ingestor: MagicMock, target_qn: str) -> set[str]:
    """The resolution of every CALLS edge into `target_qn`."""
    out: set[str] = set()
    for c in get_relationships(ingestor, "CALLS"):
        if c.args[2][2] != target_qn:
            continue
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        out.add(str(props.get(cs.KEY_RESOLUTION)))
    return out


# --- synthetic sources: one file of n chained, documented definitions -------


def _c_source(n: int) -> dict[str, str]:
    body = "".join(
        f"/** Doc {i}. */\nint f{i}(void) {{ return f{i + 1}(); }}\n" for i in range(n)
    )
    return {"gen.c": f"int f{n}(void);\n{body}int f{n}(void) {{ return 0; }}\n"}


def _js_source(n: int) -> dict[str, str]:
    body = "".join(
        f"/** Doc {i}. */\nfunction f{i}() {{ return f{i + 1}(); }}\n" for i in range(n)
    )
    return {"gen.js": f"{body}function f{n}() {{ return 0; }}\n"}


def _ts_source(n: int) -> dict[str, str]:
    body = "".join(
        f"/** Doc {i}. */\nexport function f{i}(): number {{ return f{i + 1}(); }}\n"
        for i in range(n)
    )
    return {"gen.ts": f"{body}export function f{n}(): number {{ return 0; }}\n"}


def _java_source(n: int) -> dict[str, str]:
    body = "".join(
        f"  /** Doc {i}. */\n  int f{i}() {{ return f{i + 1}(); }}\n" for i in range(n)
    )
    return {
        "src/p/Big.java": (
            "package p;\npublic class Big {\n  private Big next;\n"
            f"{body}  int f{n}() {{ return 0; }}\n}}\n"
        )
    }


def _csharp_source(n: int) -> dict[str, str]:
    methods = "".join(
        f"    /// <summary>Doc {i}.</summary>\n    public void Handle{i}() {{ }}\n"
        for i in range(n)
    )
    calls = "".join(f"      Handle{i}();\n" for i in range(n))
    return {
        "Big.cs": (
            "namespace N {\n  public class Big {\n"
            f"{methods}    public void Run() {{\n{calls}    }}\n  }}\n}}\n"
        )
    }


def _js_for_of_source(n: int) -> dict[str, str]:
    loops = "".join(
        f"export function f{i}(): void {{\n"
        f"  for (const w of items()) {{ w.spin(); }}\n}}\n"
        for i in range(n)
    )
    return {
        "gen.ts": (
            "export class Widget { spin(): void {} }\n"
            "export function items(): Widget[] { return [new Widget()]; }\n"
            f"{loops}"
        )
    }


def _js_factory_source(n: int) -> dict[str, str]:
    users = "".join(
        f"function f{i}() {{ const r = make(); r.open(); }}\n" for i in range(n)
    )
    return {
        "gen.js": (
            "class Box { open() { return 1; } }\n"
            "function make() { return new Box(); }\n"
            f"{users}"
        )
    }


def _js_module_receiver_source(n: int) -> dict[str, str]:
    # Module-level bindings between the uses: a per-call rescan walks every
    # one of them, which is the module-sized walk the issue measured.
    users = "".join(
        f"const k{i} = {i};\nfunction f{i}() {{ shared.open(); }}\n" for i in range(n)
    )
    return {
        "gen.js": (
            "class Box { open() { return 1; } }\n"
            "const shared = new Box();\n"
            f"{users}"
        )
    }


DOCSTRING_CASES = [
    pytest.param(_c_source, "c", id="c"),
    pytest.param(_js_source, "javascript", id="javascript"),
    pytest.param(_ts_source, "typescript", id="typescript"),
    pytest.param(_java_source, "java", id="java"),
    pytest.param(_csharp_source, "c_sharp", id="csharp"),
]


@pytest.mark.parametrize(("source", "grammar"), DOCSTRING_CASES)
def test_doc_comment_lookup_is_linear_in_sibling_definitions(
    tmp_path: Path,
    mock_ingestor: MagicMock,
    source: Callable[[int], dict[str, str]],
    grammar: str,
) -> None:
    """Hot spot 1: `extract_definition_docstring` rebuilt the parent's child
    list and scanned it for the definition, once per definition."""
    scan = _Scan(source, grammar, (definition_docstring,))
    small, large = _growth(tmp_path, scan, mock_ingestor)
    _assert_linear(small, large, "doc-comment extraction")


def test_csharp_bare_call_lookup_is_linear_in_class_members(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """Comment 1: each bare `HandleK();` enumerated every member of the class
    through the registry trie before filtering on the name."""
    scan = _Scan(_csharp_source, "c_sharp", (cs_type_inference, function_registry))
    small, large = _growth(tmp_path, scan, mock_ingestor)
    _assert_linear(small, large, "C# bare-call resolution")
    expected = {f"large.Big.N.Big.Handle{i}" for i in range(LARGE)}
    missing = expected - _calls_targets(mock_ingestor)
    assert not missing, f"bare calls lost their method: {sorted(missing)[:5]}"


def test_method_call_ownership_is_linear_in_class_methods(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """Found profiling the fixed C# class: splitting each method's calls from
    its nested functions' scanned every function of the class, per method.
    Counted in that one function: the rest of the call pass is linear and
    would dilute the ratio."""
    scan = _Scan(
        _csharp_source,
        "c_sharp",
        (call_processor,),
        functions=frozenset({"_calls_owned_by"}),
    )
    small, large = _growth(tmp_path, scan, mock_ingestor)
    _assert_linear(small, large, "per-method call ownership")
    assert len(_calls_targets(mock_ingestor)) == LARGE


def test_java_field_scan_is_linear_in_class_members(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """Hot spot 4: the class body was re-walked for its fields once per method
    whose local variable types were built."""
    scan = _Scan(_java_source, "java", (variable_analyzer,))
    small, large = _growth(tmp_path, scan, mock_ingestor)
    _assert_linear(small, large, "Java class-field typing")


def test_ts_for_of_call_typing_is_linear_in_functions(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """Comment 2, item 1: `for (w of items())` located `items` by walking the
    whole module tree, once per loop and enclosing function."""
    scan = _Scan(_js_for_of_source, "typescript", (js_type_inference,))
    small, large = _growth(tmp_path, scan, mock_ingestor)
    _assert_linear(small, large, "for-of element typing")
    assert "large.gen.Widget.spin" in _calls_targets(mock_ingestor), (
        "the loop variable must still be typed by items()'s return type"
    )


def test_js_call_result_receiver_lookup_is_linear(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """Comment 2, items 2 and 4: `const r = make(); r.open()` found `make` by
    walking its module tree and re-read its returns, once per call site."""
    scan = _Scan(_js_factory_source, "javascript", (call_processor,))
    small, large = _growth(tmp_path, scan, mock_ingestor)
    _assert_linear(small, large, "call-result receiver typing")
    # Exact, not heuristic: the receiver was typed through make()'s return.
    assert _resolutions_into(mock_ingestor, "large.gen.Box.open") == {_EXACT}


def test_js_factory_returns_are_read_once_per_factory(
    tmp_path: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comment 2, item 4: the factory's body was re-walked for its returns at
    every call-result receiver; the answer is per factory, not per site."""
    reads: list[str] = []
    original = call_processor.CallProcessor._js_function_return_class

    def counting(
        self: call_processor.CallProcessor, fn_node: Node, fn_qn: str, module: str
    ) -> str | None:
        reads.append(fn_qn)
        return original(self, fn_node, fn_qn, module)

    monkeypatch.setattr(
        call_processor.CallProcessor, "_js_function_return_class", counting
    )
    scan = _Scan(_js_factory_source, "javascript", ())
    _index(tmp_path / "small", _js_factory_source(SMALL), scan, mock_ingestor)
    small = len(reads)
    reads.clear()
    _index(tmp_path / "large", _js_factory_source(LARGE), scan, mock_ingestor)

    assert small > 0, "fixture guard: no factory return was read"
    assert len(reads) == small, (
        f"make()'s returns were read {small} times for {SMALL} call sites and "
        f"{len(reads)} times for {LARGE}: once per site, not once per factory"
    )


def test_js_module_receiver_scope_scan_is_linear(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """Comment 2, item 3: a module-level receiver's introduction was found by
    rescanning the module's statements, once per call site."""
    scan = _Scan(_js_module_receiver_source, "javascript", (call_processor,))
    small, large = _growth(tmp_path, scan, mock_ingestor)
    _assert_linear(small, large, "receiver scope lookup")
    # Exact, not heuristic: the receiver was typed by its module declaration.
    assert _resolutions_into(mock_ingestor, "large.gen.Box.open") == {_EXACT}


def test_js_export_list_scan_stays_linear(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """Hot spot 2 was memoised per tree before this fix (#2591); this keeps it
    from regressing alongside the others."""
    scan = _Scan(_ts_source, "typescript", (export_detection,))
    small, large = _growth(tmp_path, scan, mock_ingestor)
    _assert_linear(small, large, "export-list detection")
