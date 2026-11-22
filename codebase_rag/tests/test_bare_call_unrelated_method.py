"""Issue #2575: a bare call never binds to a same-named method of an unrelated class.

In PHP, JS/TS and Go a method is reached only through a receiver
(`$this->`, `this.`, a value), and in C/C++ only from a member of its own
class or a subclass. Yet the simple-name fallback offered every same-named
definition, so the built-ins `count($xs)`, `rewind($stream)`, `parseInt(s)`
and `max(a, b)` were bound, heuristically, to whichever class in the project
defines a method of that name: symfony/console's `TreeHelper::rewind` had 133
callers that were really PHP's `rewind`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

FILES = {
    # PHP: an Iterator/Countable class and the array/stream built-ins.
    "Bag.php": (
        "<?php\nnamespace App;\n"
        "class Bag implements \\Iterator, \\Countable {\n"
        "    private $i = 0;\n"
        "    public function count(): int { return 0; }\n"
        "    public function current(): mixed { return 1; }\n"
        "    public function key(): mixed { return 0; }\n"
        "    public function next(): void { $this->i++; }\n"
        "    public function rewind(): void { $this->i = 0; }\n"
        "    public function valid(): bool { return false; }\n"
        "    public function reset(): void { $this->rewind(); }\n"
        "}\n"
        "function tally(array $xs): int { return 1; }\n"
    ),
    "util.php": (
        "<?php\nnamespace App;\n"
        "function summarize(array $xs, $stream): string {\n"
        "    $n = count($xs); $first = current($xs); $k = key($xs);\n"
        "    next($xs); rewind($stream);\n"
        '    return "$n $first $k";\n'
        "}\n"
        "function viaBag(Bag $bag): int { return $bag->count(); }\n"
        "function viaTally(array $xs): int { return tally($xs); }\n"
    ),
    # JS / TS: a facade class named after the globals.
    "stats_js.js": (
        "class Num { parseInt(s) { return 1; } setTimeout(f) { return 0; } }\n"
        "function clamp(x) { return x; }\n"
        "module.exports = { Num, clamp };\n"
    ),
    "util_js.js": (
        "const { Num, clamp } = require('./stats_js');\n"
        "function run(s, f) { return [parseInt(s, 10), setTimeout(f, 0)]; }\n"
        "function viaNum(s) { const n = new Num(); return n.parseInt(s); }\n"
        "function viaClamp(x) { return clamp(x); }\n"
    ),
    "stats_ts.ts": "export class NumT { parseFloat(s: string) { return 1; } }\n",
    "util_ts.ts": "export function runT(s: string) { return parseFloat(s); }\n",
    # Go: a method and a package function in sibling files.
    "g/a.go": (
        "package g\n\ntype T struct{}\n\n"
        "func (t T) Len() int { return 0 }\n\n"
        "func Size() int { return 0 }\n"
    ),
    "g/b.go": "package g\n\nfunc Use() int { return Len() + Size() }\n",
    # C++: free functions, own-class and inherited member calls.
    "c/base.h": "class Base { public: int helper() { return 1; } };\n",
    "c/stat.h": (
        '#include "base.h"\n'
        "class Stat : public Base {\n public:\n"
        "  int max(int a, int b) { return a; }\n"
        "  int own() { return max(1, 2); }\n"
        "  int inherited() { return helper(); }\n"
        "  int out_of_line();\n"
        "};\n"
        "class Deeper : public Stat { int twice() { return helper(); } };\n"
    ),
    "c/stat.cpp": '#include "stat.h"\nint Stat::out_of_line() { return max(3, 4); }\n',
    "c/other.h": "class Other { public: int helper() { return 2; } };\n",
    "c/free.cpp": (
        "#include <algorithm>\n"
        '#include "other.h"\n'
        '#include "stat.h"\n'
        "using namespace std;\n"
        "int pick(int a, int b) { return max(a, b); }\n"
        "int lone() { return helper(); }\n"
        "int viaStat(Stat s) { return s.max(1, 2); }\n"
    ),
    # A virtual diamond: DB's `probe` hides DA's along every path to DD, so
    # C++ lookup finds DB::probe though a breadth-first walk meets DA first
    # (Greptile, PR #2948).
    "c/diamond.h": (
        "class DA { public: int probe() { return 1; } };\n"
        "class DB : public virtual DA { public: int probe() { return 2; } };\n"
        "class DC : public virtual DA {};\n"
        "class DE : public DB {};\n"
        "class DD : public DC, public DE { public: int go() { return probe(); } };\n"
    ),
    # A member constructing its base by name: `Parent(1)` names the class,
    # which unqualified lookup never answers with the constructor
    # (CodeRabbit, PR #2948).
    "c/ctor.h": (
        "class Parent {\n public:\n  Parent(int x) {}\n  ~Parent() {}\n};\n"
        "class Kid : public Parent {\n public:\n"
        "  Kid() : Parent(0) {}\n"
        "  Parent make() { return Parent(1); }\n"
        "};\n"
    ),
    # A parameter or local named like an inherited method hides it where in
    # scope, so the call invokes that value (CodeRabbit, PR #2948). The
    # declaration of `run` puts a `dispatch` parameter in the class body,
    # which must not hide `HB::dispatch` from `closed`.
    "c/hide.h": (
        "class HB { public: void dispatch() {} };\n"
        "class HD : public HB {\n public:\n"
        "  void run(void (*dispatch)());\n"
        "  void local() { auto dispatch = [] {}; dispatch(); }\n"
        "  void closed() { { auto dispatch = [] {}; } dispatch(); }\n"
        "};\n"
    ),
    # A data member hides a base's method of its name: `dispatch()` calls
    # the pointer (Greptile, PR #2948).
    "c/field.h": (
        "class FB { public: void dispatch() {} };\n"
        "class FD : public FB {\n public:\n"
        "  void (*dispatch)();\n  void run() { dispatch(); }\n};\n"
    ),
    "c/hide.cpp": (
        '#include "hide.h"\nvoid HD::run(void (*dispatch)()) { dispatch(); }\n'
    ),
    # A constructor's member initializer is read with the module's calls, so
    # its bare call of the class's own static keeps the name fallback.
    "c/init.h": (
        "class Seq {\n public:\n  Seq() : next_(Start()) {}\n"
        "  static int Start() { return 0; }\n  int next_;\n};\n"
    ),
    # ... and so is a default member initializer (Greptile, PR #2948).
    "c/dflt.h": (
        "class Dflt {\n public:\n  static int Begin() { return 0; }\n"
        "  int next_ = Begin();\n};\n"
    ),
    # A file-scope call is no member initializer: it keeps the method filter.
    "c/glob.cpp": '#include "stat.h"\nint g = max(1, 2);\n',
    # Third review round (CodeRabbit, PR #2948): a `for` header's local ends
    # with the loop, and a nested class reaches its enclosing class's bases.
    "c/scopes.h": (
        "class SB {\n public:\n  void dispatch() {}\n  static void ping() {}\n};\n"
        "class SD : public SB {\n public:\n"
        "  void loop() {\n    for (auto dispatch = [] {}; false;) { dispatch(); }\n"
        "    dispatch();\n  }\n"
        "  struct Inner { void run() { ping(); } };\n"
        "  void ranged(void (**fs)()) {\n"
        "    for (auto dispatch : {fs[0]}) { dispatch(); }\n  }\n"
        "  void ranged_source() { for (auto dispatch : dispatch()) {} }\n"
        "  void unpacked(Pair callbacks) {\n"
        "    auto [tag, dispatch] = callbacks;\n    dispatch();\n  }\n"
        "  void unpacked_ref(Pair& callbacks) {\n"
        "    auto& [tag, dispatch] = callbacks;\n    dispatch();\n  }\n"
        "  void ranged_pairs(Pair* pairs) {\n"
        "    for (auto [tag, dispatch] : pairs) { dispatch(); }\n  }\n"
        "  void unpacked_later() {\n    dispatch();\n"
        "    auto [tag, dispatch] = Pair();\n  }\n};\n"
    ),
    # Fourth review round (Greptile, PR #2948): a `catch` parameter ends with
    # its handler, and a block's using-declaration hides a base's member.
    "c/blocks.h": (
        "namespace UN { void ping() {} }\n"
        "class UB {\n public:\n  void dispatch() {}\n  void ping() {}\n};\n"
        "class UD : public UB {\n public:\n"
        "  void caught() {\n"
        "    try {} catch (void (*dispatch)()) { dispatch(); }\n"
        "    dispatch();\n  }\n"
        "  void imported() { using UN::ping; ping(); }\n"
        "  void imported_closed() { { using UN::ping; } ping(); }\n"
        "  void directed() { using namespace UN; ping(); }\n"
        "};\n"
    ),
    # Lua: a colon method beside a bare call of its name (CodeRabbit, PR
    # #2948); `flush()` is a global, never `M:flush`.
    "lua_m.lua": "local M = {}\nfunction M:flush() return 1 end\nreturn M\n",
    "lua_use.lua": "local function go() return flush() end\nreturn go\n",
    # Classes local to a function, scoped under it (issue #2555): a member's
    # bare calls reach its own class and a local base, never `Stat::max`.
    "c/local.cpp": (
        "void work() {\n"
        "  struct LB { int helper() { return 0; } };\n"
        "  class Res : public LB {\n   public:\n"
        "    Res() { acquire(); }\n"
        "    void acquire() { helper(); max(1, 2); }\n"
        "  };\n"
        "  Res r;\n"
        "}\n"
    ),
    # Python and Java were already right; they stay so.
    "stats.py": "class Stats:\n    def max(self):\n        return 1\n",
    "util.py": "def summarize(xs):\n    return max(xs)\n",
    "j/A.java": "package j;\nclass A { int count() { return 0; } }\n",
    "j/B.java": "package j;\nclass B { int run() { return count(); } }\n",
    "j/C.java": "package j;\nclass C extends A { int run() { return count(); } }\n",
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("bare") / "bare"
    for rel, text in FILES.items():
        _write(root, rel, text)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src.removeprefix(prefix) == caller
    }


@pytest.mark.parametrize(
    ("caller", "method"),
    [
        ("util.php.summarize", "Bag.Bag.count"),
        ("util.php.summarize", "Bag.Bag.current"),
        ("util.php.summarize", "Bag.Bag.key"),
        ("util.php.summarize", "Bag.Bag.next"),
        ("util.php.summarize", "Bag.Bag.rewind"),
        ("util_js.run", "stats_js.Num.parseInt"),
        ("util_js.run", "stats_js.Num.setTimeout"),
        ("util_ts.runT", "stats_ts.NumT.parseFloat"),
        ("g.b.Use", "g.a.T.Len"),
        ("c.free.pick", "c.stat.h.Stat.max"),
        ("c.free.lone", "c.other.h.Other.helper"),
        ("c.free.lone", "c.base.Base.helper"),
        ("c.glob", "c.stat.h.Stat.max"),
        ("lua_use.go", "lua_m.M:flush"),
    ],
)
def test_a_bare_call_binds_no_method_of_an_unrelated_class(
    graph: RecordedGraph, caller: str, method: str
) -> None:
    assert method not in _callees(graph, caller)


@pytest.mark.parametrize(
    ("caller", "method"),
    [
        ("c.stat.h.Stat.inherited", "c.base.Base.helper"),
        ("c.stat.h.Deeper.twice", "c.base.Base.helper"),
        ("c.diamond.DD.go", "c.diamond.DB.probe"),
        ("c.hide.h.HD.closed", "c.hide.h.HB.dispatch"),
    ],
)
def test_a_cpp_member_reaches_an_inherited_method_by_name_lookup(
    graph: RecordedGraph, caller: str, method: str
) -> None:
    # The base's method is what C++ name lookup finds, not a guess among
    # every `helper` in the project (`Other::helper` sits beside it).
    assert _callees(graph, caller) == {method: "exact"}


def test_a_range_for_variable_hides_an_inherited_method(graph: RecordedGraph) -> None:
    # `for (auto dispatch : ...) dispatch();` calls the loop variable
    # (Greptile, CodeRabbit, PR #2948).
    assert _callees(graph, "c.scopes.SD.ranged").get("c.scopes.SB.dispatch") != (
        "exact"
    )


@pytest.mark.parametrize("caller", ["unpacked", "unpacked_ref", "ranged_pairs"])
def test_every_structured_binding_name_hides_an_inherited_method(
    graph: RecordedGraph, caller: str
) -> None:
    # `auto [tag, dispatch] = callbacks; dispatch();` calls the bound
    # callback: every name in the brackets is a local, not only the first
    # (Greptile, PR #2948).
    assert _callees(graph, f"c.scopes.SD.{caller}").get("c.scopes.SB.dispatch") != (
        "exact"
    )


def test_a_structured_binding_hides_only_after_its_declaration(
    graph: RecordedGraph,
) -> None:
    # Before the binding, `dispatch()` still finds the base's method.
    assert _callees(graph, "c.scopes.SD.unpacked_later").get(
        "c.scopes.SB.dispatch"
    ) == ("exact")


@pytest.mark.parametrize("caller", ["c.hide.h.HD.run", "c.hide.h.HD.local"])
def test_a_cpp_parameter_or_local_hides_an_inherited_method(
    graph: RecordedGraph, caller: str
) -> None:
    assert "c.hide.h.HB.dispatch" not in _callees(graph, caller)


def test_a_cpp_data_member_hides_an_inherited_method(graph: RecordedGraph) -> None:
    # Whatever the name fallback makes of the call, it is no exact edge to
    # the base's method the field hides.
    assert _callees(graph, "c.field.FD.run").get("c.field.FB.dispatch") != "exact"


# Negative: what must not change.


@pytest.mark.parametrize(
    ("caller", "callee", "resolution"),
    [
        ("util.php.viaBag", "Bag.Bag.count", "heuristic"),
        ("Bag.Bag.reset", "Bag.Bag.rewind", "exact"),
        ("util.php.viaTally", "Bag.tally", "heuristic"),
        ("util_js.viaNum", "stats_js.Num.parseInt", "exact"),
        ("util_js.viaClamp", "stats_js.clamp", "exact"),
        ("c.stat.h.Stat.own", "c.stat.h.Stat.max", "exact"),
        ("c.stat.h.Stat.out_of_line", "c.stat.h.Stat.max", "exact"),
        ("c.free.viaStat", "c.stat.h.Stat.max", "exact"),
        ("j.C.C.run()", "j.A.A.count()", "exact"),
    ],
)
def test_a_receiver_call_a_function_or_an_own_member_still_binds(
    graph: RecordedGraph, caller: str, callee: str, resolution: str
) -> None:
    assert _callees(graph, caller).get(callee) == resolution


def test_a_cpp_member_constructing_its_base_instantiates_the_class(
    graph: RecordedGraph,
) -> None:
    # As anywhere else, the construction binds the class and its destructor,
    # not an exact call to `Parent::Parent` found as an inherited member.
    prefix = f"{graph.project}."
    instantiated = {
        dst.removeprefix(prefix)
        for src, rel, dst, _ in graph.edges
        if rel == "INSTANTIATES" and src.removeprefix(prefix) == "c.ctor.Kid.make"
    }
    assert instantiated == {"c.ctor.Parent"}
    assert "c.ctor.Parent.~Parent" in _callees(graph, "c.ctor.Kid.make")


def test_a_function_local_class_keeps_its_members_bare_calls(
    graph: RecordedGraph,
) -> None:
    # Each member is its own caller and sees its class (issue #2555), so its
    # bare calls get the same name lookup as any other class's member.
    assert _callees(graph, "c.local.work.Res.Res") == {
        "c.local.work.Res.acquire": "exact"
    }
    assert _callees(graph, "c.local.work.Res.acquire") == {
        "c.local.work.LB.helper": "exact"
    }


@pytest.mark.parametrize(
    ("caller", "method"),
    [
        ("c.scopes.SD.loop", "c.scopes.SB.dispatch"),
        ("c.scopes.SD.Inner.run", "c.scopes.SB.ping"),
        ("c.scopes.SD.ranged_source", "c.scopes.SB.dispatch"),
        ("c.blocks.UD.caught", "c.blocks.UB.dispatch"),
        ("c.blocks.UD.imported_closed", "c.blocks.UB.ping"),
        ("c.blocks.UD.directed", "c.blocks.UB.ping"),
    ],
    ids=[
        "after-a-for-header-local",
        "through-the-enclosing-class",
        "in-a-range-for-expression",
        "after-a-catch-handler",
        "after-a-closed-using-declaration",
        "beside-a-using-directive",
    ],
)
def test_a_cpp_inherited_member_found_past_a_closed_scope(
    graph: RecordedGraph, caller: str, method: str
) -> None:
    assert _callees(graph, caller).get(method) == "exact"


def test_a_block_using_declaration_hides_an_inherited_method(
    graph: RecordedGraph,
) -> None:
    # `using UN::ping; ping();` calls `UN::ping`: the using-declaration
    # declares `ping` in the block, which lookup searches before the class.
    assert _callees(graph, "c.blocks.UD.imported").get("c.blocks.UB.ping") != "exact"


def test_a_cpp_member_initializer_keeps_its_call_of_a_member(
    graph: RecordedGraph,
) -> None:
    # `next_(Start())` runs in the constructor, but its call is read with
    # the module's, where no class answers the bare name; dropping the
    # name fallback there lost the edge (leveldb reference graph).
    assert "c.init.Seq.Start" in _callees(graph, "c.init")
    assert "c.dflt.Dflt.Begin" in _callees(graph, "c.dflt")


def test_a_bare_go_call_still_reaches_a_package_function(graph: RecordedGraph) -> None:
    assert "g.a.Size" in _callees(graph, "g.b.Use")


@pytest.mark.parametrize(
    "caller", ["util.summarize", "j.B.B.run()"], ids=["python", "java"]
)
def test_a_language_that_was_already_right_binds_no_unrelated_method(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller) == {}
