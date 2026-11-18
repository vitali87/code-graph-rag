"""Flow-sensitive bindings for Python calls (JARVIS, arXiv 2305.05949).

cgr typed a local once per body, so every call through a rebound name saw
one binding: `a = f1; a(); a = f2; a()` gave two edges to `f2`'s side and
none to `f1`, and a receiver rebound on each arm of an `if` resolved to one
arm's class. The bindings that reach each call now decide it. A call whose
reaching bindings include one this cannot follow (a parameter, an import) is
left to the flat map, as before.

The `with` protocol is the second JARVIS feature here: entering a manager
calls its `__enter__` and leaving calls its `__exit__`, through its bases.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.py.flow_bindings import BindingKind, FlowBindings
from evals.cgr_graph import _CapturingIngestor

PROJECT = "proj"


def _parse(source: str) -> Node:
    return Parser(Language(tsp.language())).parse(source.encode()).root_node


def _reaching(source: str) -> dict[str, list[str]]:
    """{call text @ line: sorted 'kind@line' of the bindings reaching it}."""
    root = _parse(source)
    flow = FlowBindings.of(root)
    data = source.encode()
    out: dict[str, list[str]] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != cs.TS_PY_CALL:
            continue
        bindings = flow.at(node)
        if bindings is None:
            continue
        key = f"{node.text.decode()}@{node.start_point[0] + 1}"
        out[key] = sorted(
            f"{b.kind}@{data[: b.site].count(10) + 1}"
            if b.kind != BindingKind.ENTRY
            else str(b.kind)
            for b in bindings
        )
    return out


class TestReaching:
    def test_a_later_binding_replaces_an_earlier_one_on_its_path(self) -> None:
        got = _reaching("a = b = f1\nb()\na = b = f2\na()\n")
        assert got == {"b()@2": ["assign@1"], "a()@4": ["assign@3"]}

    def test_branches_merge_and_an_exhaustive_if_kills_the_entry_binding(
        self,
    ) -> None:
        both = "c = A()\nif x:\n    c = B()\nelse:\n    c = C()\nc.m()\n"
        one = "c = A()\nif x:\n    c = B()\nc.m()\n"
        assert _reaching(both)["c.m()@6"] == ["assign@3", "assign@5"]
        assert _reaching(one)["c.m()@4"] == ["assign@1", "assign@3"]

    def test_a_loop_carries_its_body_back_to_its_head(self) -> None:
        got = _reaching("a = f1\nfor i in xs:\n    a()\n    a = f2\na()\n")
        assert got["a()@3"] == ["assign@1", "assign@4"]
        assert got["a()@5"] == ["assign@1", "assign@4"]

    def test_a_path_that_returns_contributes_nothing_after_it(self) -> None:
        got = _reaching("a = f1\nif x:\n    a = f2\n    return\na()\n")
        assert got["a()@5"] == ["assign@1"]

    def test_a_handler_sees_every_state_of_its_try(self) -> None:
        source = "a = f1\ntry:\n    a = f2\n    g()\nexcept E:\n    a()\n"
        assert _reaching(source)["a()@6"] == ["assign@1", "assign@3"]

    def test_unpacking_pairs_targets_with_values_around_a_star(self) -> None:
        got = _reaching("a, *b, c = f1, f2, f3, f4\na()\nc()\nd, e = e, d\n")
        assert got["a()@2"] == ["assign@1"]
        assert got["c()@3"] == ["assign@1"]

    def test_a_walrus_binds_where_it_is_evaluated(self) -> None:
        assert _reaching("a = f1\nif (a := f2):\n    a()\n")["a()@3"] == ["assign@2"]

    def test_a_parameter_is_undecided_until_the_body_rebinds_it(self) -> None:
        # Before the body binds `cb` the call sees the parameter, which this
        # pass does not decide; after `cb = g` it sees that assignment, and
        # past a branch that rebinds it on one arm only, both.
        source = (
            "def f(cb):\n    cb()\n    cb = g\n    cb()\n"
            "def h(cb):\n    if x:\n        cb = g\n    cb()\n"
        )
        root = _parse(source)
        f, h = root.named_children
        f_calls = [n for n in _walk(f) if n.type == cs.TS_PY_CALL]
        flow = FlowBindings.of(f)
        assert flow.at(f_calls[0]) is None
        assert {b.kind for b in flow.at(f_calls[1]) or ()} == {BindingKind.ASSIGN}
        h_call = next(n for n in _walk(h) if n.type == cs.TS_PY_CALL)
        assert {b.kind for b in FlowBindings.of(h).at(h_call) or ()} == {
            BindingKind.ENTRY,
            BindingKind.ASSIGN,
        }

    def test_an_irrefutable_case_leaves_no_path_on_which_none_ran(self) -> None:
        source = "a = f1\nmatch v:\n    case _:\n        a = f2\na()\n"
        assert _reaching(source)["a()@5"] == ["assign@4"]
        guarded = "a = f1\nmatch v:\n    case _ if c:\n        a = f2\na()\n"
        assert _reaching(guarded)["a()@5"] == ["assign@1", "assign@4"]

    def test_a_case_guard_runs_before_its_body(self) -> None:
        source = "a = f1\nmatch v:\n    case _ if (a := f2):\n        a()\n"
        assert _reaching(source)["a()@4"] == ["assign@3"]

    @pytest.mark.parametrize(
        "expression",
        ["flag and (a := f2)", "flag or (a := f2)", "(a := f2) if flag else 0"],
    )
    def test_an_operand_that_may_not_run_joins_the_path_that_skips_it(
        self, expression: str
    ) -> None:
        got = _reaching(f"a = f1\n{expression}\na()\n")
        assert got["a()@3"] == ["assign@1", "assign@2"]

    def test_a_walrus_in_a_comprehension_binds_the_enclosing_name(self) -> None:
        # The comprehension may iterate zero times, so the earlier binding
        # still reaches; its own target does not outlive it.
        got = _reaching("a = f1\n[(a := f2) for a2 in xs]\na()\n")
        assert got["a()@3"] == ["assign@1", "assign@2"]

    def test_finally_runs_on_the_way_out_of_a_loop(self) -> None:
        source = (
            "a = f1\nfor i in xs:\n    try:\n        break\n"
            "    finally:\n        a = f2\na()\n"
        )
        assert _reaching(source)["a()@7"] == ["assign@1", "assign@6"]

    def test_a_global_name_is_never_decided_here(self) -> None:
        source = "def f():\n    global a\n    a = f1\n    a()\n"
        root = _parse(source)
        flow = FlowBindings.of(root.named_children[0])
        assert "a" not in flow.tracked


def _walk(node: Node) -> list[Node]:
    out = [node]
    for child in node.children:
        out += _walk(child)
    return out


def _calls(tmp_path: Path, files: dict[str, str]) -> dict[str, set[str]]:
    """{caller qn without the project: {callee qn without the project}}."""
    repo = tmp_path / PROJECT
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    ingestor = _CapturingIngestor()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    out: dict[str, set[str]] = {}
    for _fl, caller, rel, _tl, callee in ingestor.rels:
        if rel == cs.RelationshipType.CALLS.value:
            a = str(caller).removeprefix(f"{PROJECT}.")
            b = str(callee).removeprefix(f"{PROJECT}.")
            out.setdefault(a, set()).add(b)
    return out


class _SiteIngestor(_CapturingIngestor):
    def __init__(self) -> None:
        super().__init__()
        self.lines: dict[tuple[str, str], set[object]] = {}

    def ensure_relationship_batch(self, from_spec, rel_type, to_spec, properties=None):
        super().ensure_relationship_batch(from_spec, rel_type, to_spec, properties)
        key = (
            str(from_spec[2]).removeprefix(f"{PROJECT}."),
            str(to_spec[2]).removeprefix(f"{PROJECT}."),
        )
        self.lines.setdefault(key, set()).add((properties or {}).get(cs.KEY_LINE))


def _sites(tmp_path: Path, files: dict[str, str]) -> dict[tuple[str, str], set[object]]:
    """{(caller, callee) without the project: the lines its edges are sited at}."""
    repo = tmp_path / PROJECT
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    ingestor = _SiteIngestor()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return ingestor.lines


CLASSES = (
    "class A:\n    def m(self):\n        pass\n"
    "class B:\n    def m(self):\n        pass\n"
    "class C:\n    def m(self):\n        pass\n"
)


class TestEdges:
    def test_each_call_goes_to_the_function_bound_before_it(
        self, tmp_path: Path
    ) -> None:
        source = (
            "def f1():\n    pass\ndef f2():\n    pass\n"
            "a = b = f1\nb()\na = b = f2\na()\n"
        )
        assert _calls(tmp_path, {"main.py": source})["main"] == {"main.f1", "main.f2"}

    def test_a_receiver_rebound_on_every_arm_loses_its_first_class(
        self, tmp_path: Path
    ) -> None:
        source = CLASSES + (
            "def use(x):\n"
            "    c = A()\n"
            "    if x:\n        c = B()\n"
            "    else:\n        c = C()\n"
            "    c.m()\n"
        )
        calls = _calls(tmp_path, {"main.py": source})["main.use"]
        assert {"main.B.m", "main.C.m"} <= calls
        assert "main.A.m" not in calls

    def test_unpacked_and_copied_callables_resolve(self, tmp_path: Path) -> None:
        source = (
            "def f1():\n    pass\ndef f2():\n    pass\n"
            "a, b = f1, f2\na()\n"
            "v1 = f1\nv2 = v1\nv1 = f2\nv2()\n"
        )
        assert _calls(tmp_path, {"main.py": source})["main"] == {"main.f1"}

    def test_a_parameter_keeps_its_flat_resolution(self, tmp_path: Path) -> None:
        source = "def g():\n    pass\ndef use(cb):\n    cb()\n    cb = g\n    cb()\n"
        # The first call sees the parameter, which nothing here types; only
        # the second is decided by its binding.
        assert "main.g" in _calls(tmp_path, {"main.py": source}).get("main.use", set())

    def test_a_lone_binding_resolves_as_the_flat_map_does(self, tmp_path: Path) -> None:
        # One binding is what the flat map already holds, so the flow pass
        # must not touch it: these are the edges cgr gave before it existed
        # (the value's class, not the annotation's).
        source = CLASSES + (
            "def make():\n    return B()\ndef use():\n    c: A = make()\n    c.m()\n"
        )
        assert _calls(tmp_path, {"main.py": source})["main.use"] == {
            "main.B.m",
            "main.make",
        }


RESPONSES = (
    "class Response:\n"
    "    @property\n    def content(self):\n        return b''\n"
    "class Streaming:\n"
    "    @property\n    def content(self):\n        raise AttributeError\n"
)


class TestMemberReads:
    # A property read is an attribute, not a call; the bindings that reach
    # its receiver decide it all the same (django's
    # test_conditional_content_removal reads `res.content` after each of
    # several `res = HttpResponse(...)` / `res = StreamingHttpResponse(...)`).
    def test_a_property_read_goes_to_the_class_bound_before_it(
        self, tmp_path: Path
    ) -> None:
        source = RESPONSES + (
            "def use():\n"
            "    res = Response()\n    res.content\n"
            "    res = Streaming()\n    res.content\n"
        )
        calls = _calls(tmp_path, {"main.py": source})["main.use"]
        assert {"main.Response.content", "main.Streaming.content"} <= calls

    def test_a_property_passed_as_an_argument_reads_the_same_way(
        self, tmp_path: Path
    ) -> None:
        source = RESPONSES + (
            "def check(value):\n    pass\n"
            "def use():\n"
            "    res = Response()\n    check(res.content)\n"
            "    res = Streaming()\n    check(res.content)\n"
        )
        calls = _calls(tmp_path, {"main.py": source})["main.use"]
        assert {"main.Response.content", "main.Streaming.content"} <= calls

    @pytest.mark.parametrize(
        "body",
        [
            # One binding: the flat map's answer.
            "    res = Streaming()\n    res.content\n",
            # A parameter reaches the read, which nothing here types.
            "    if res is None:\n        res = Streaming()\n    res.content\n",
        ],
    )
    def test_a_read_this_cannot_decide_keeps_the_flat_answer(
        self, tmp_path: Path, body: str
    ) -> None:
        source = RESPONSES + f"def use(res: Response = None):\n{body}"
        calls = _calls(tmp_path, {"main.py": source}).get("main.use", set())
        assert calls & {"main.Response.content", "main.Streaming.content"} == {
            "main.Streaming.content"
        }


MANAGERS = (
    "class Base:\n"
    "    def __enter__(self):\n        return self\n"
    "    def __exit__(self, *exc):\n        pass\n"
    "class Child(Base):\n"
    "    def __enter__(self):\n        return self\n"
    "    def run(self):\n        pass\n"
    "class Async:\n"
    "    async def __aenter__(self):\n        return self\n"
    "    async def __aexit__(self, *exc):\n        pass\n"
)


class TestWithProtocol:
    def test_entering_calls_enter_and_exit_through_the_bases(
        self, tmp_path: Path
    ) -> None:
        source = MANAGERS + "def use():\n    with Child() as c:\n        c.run()\n"
        assert _calls(tmp_path, {"main.py": source})["main.use"] == {
            "main.Child.__enter__",
            "main.Base.__exit__",
            "main.Child.run",
        }

    def test_a_body_that_only_enters_a_manager_calls_its_dunders(
        self, tmp_path: Path
    ) -> None:
        # No call anywhere in the file: the with statement is itself one of
        # the body's call nodes, so the protocol pass still runs.
        source = MANAGERS + "def use(lock: Child):\n    with lock:\n        pass\n"
        assert _calls(tmp_path, {"main.py": source})["main.use"] == {
            "main.Child.__enter__",
            "main.Base.__exit__",
        }

    def test_a_rebound_manager_is_the_one_bound_at_the_with(
        self, tmp_path: Path
    ) -> None:
        source = MANAGERS + (
            "def use():\n    m = Child()\n    with m:\n        pass\n    m = Base()\n"
        )
        calls = _calls(tmp_path, {"main.py": source})["main.use"]
        assert "main.Child.__enter__" in calls
        assert "main.Base.__enter__" not in calls

    def test_a_protocol_edge_is_sited_at_its_with(self, tmp_path: Path) -> None:
        source = MANAGERS + (
            "def helper():\n    pass\n"
            # The call after the with is the last call node the body's
            # pass visits; the protocol edges must not take its site.
            "def use():\n    with Child():\n        pass\n    helper()\n"
        )
        with_line = source.splitlines().index("    with Child():") + 1
        sites = _sites(tmp_path, {"main.py": source})
        assert sites[("main.use", "main.Child.__enter__")] == {with_line}
        assert sites[("main.use", "main.Base.__exit__")] == {with_line}

    def test_async_with_calls_the_async_pair(self, tmp_path: Path) -> None:
        source = MANAGERS + "async def use():\n    async with Async():\n        pass\n"
        assert _calls(tmp_path, {"main.py": source})["main.use"] == {
            "main.Async.__aenter__",
            "main.Async.__aexit__",
        }

    @pytest.mark.parametrize(
        "manager", ["open('f')", "lock", "contextlib.suppress(KeyError)"]
    )
    def test_a_manager_the_project_does_not_define_adds_nothing(
        self, tmp_path: Path, manager: str
    ) -> None:
        source = (
            f"import contextlib\ndef use(lock):\n    with {manager}:\n        pass\n"
        )
        calls = _calls(tmp_path, {"main.py": source}).get("main.use", set())
        assert not any(c.endswith(("__enter__", "__exit__")) for c in calls)
