"""Issue #3822: a function passed by name is resolved with the caller's
language, in every language whose passed functions reach the resolver.

The callback, callable-flow, factory-alias and returned-closure passes
resolved what they were handed with no language, so each language's own
rules for a name were skipped there and the edge said something the same
name in a call would not. The cache did not key on the language either, so a
call made after such a pass could get its guess back. Go's three bare-name
rules are in `test_go_passed_function_language.py`; these are the other
languages whose graph changes:

- Python: a local that shadows an imported module hides it (#1907), and a
  typed receiver whose class lacks the member names no other function.
- JavaScript and TypeScript: a member read off an untyped value names no
  same-named free function (#2609).
- TypeScript, through the cache: `x = this.opts.retry.delay(1)` read by the
  factory-alias pass must not hand the call itself that pass's guess.
- Dart: the same rule, for `props.fold` on an `Iterable`.
- C++: `s.cb` can only name a member, never a free function `cb` (#3176).
- C#: a bare method group under `using static` prefers the imported type's
  method over a same-named one elsewhere (#2001).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

CALLS = str(cs.RelationshipType.CALLS)
REFERENCES = str(cs.RelationshipType.REFERENCES)

Edges = dict[tuple[str, str, str], set[str]]


def _index(root: Path, files: dict[str, str], language: str) -> Edges:
    """{(relationship, caller qn, callee qn): resolutions} for every CALLS
    and REFERENCES edge an index of `files` writes."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=language)
    edges: Edges = {}
    for rel in (cs.RelationshipType.CALLS, cs.RelationshipType.REFERENCES):
        for c in get_relationships(ingestor, rel):
            props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
            key = (str(rel), str(c.args[0][2]), str(c.args[2][2]))
            edges.setdefault(key, set()).add(str(props.get(cs.KEY_RESOLUTION)))
    return edges


def _callees(edges: Edges, caller: str) -> set[str]:
    return {callee for _, source, callee in edges if source == caller}


PY_HELPERS = "def make_pair(x):\n    return (x, x)\n\n\ndef size(x):\n    return 1\n"
PY_USE = (
    "from pkg import helpers\n"
    "\n"
    "\n"
    "def load():\n"
    "    return None\n"
    "\n"
    "\n"
    "class Box:\n"
    "    def open(self):\n"
    "        return 1\n"
    "\n"
    "\n"
    "def shadowed(xs):\n"
    "    helpers = load()\n"
    "    pairs = list(map(helpers.make_pair, xs))\n"
    "    keep = helpers.make_pair\n"
    "    return sorted(pairs, key=helpers.make_pair), keep\n"
    "\n"
    "\n"
    "def typed(b: Box):\n"
    "    return list(map(b.size, [1])), sorted([1], key=b.size)\n"
    "\n"
    "\n"
    "def plain(xs):\n"
    "    return list(map(helpers.make_pair, xs))\n"
)


def test_python_passed_names_follow_pythons_rules(temp_repo: Path) -> None:
    edges = _index(
        temp_repo / "proj",
        {
            "pkg/__init__.py": "",
            "pkg/helpers.py": PY_HELPERS,
            "pkg/use.py": PY_USE,
        },
        cs.SupportedLanguage.PYTHON,
    )
    make_pair, size = "proj.pkg.helpers.make_pair", "proj.pkg.helpers.size"
    # `helpers = load()` hides the module: `helpers.make_pair` handed to
    # map(), sorted() and a local is a member of an unknown value.
    assert make_pair not in _callees(edges, "proj.pkg.use.shadowed"), edges
    # `b: Box` has no `size`, so `b.size` is not the module function `size`.
    assert size not in _callees(edges, "proj.pkg.use.typed"), edges
    # Where nothing hides it, the module's function is passed.
    assert edges[(CALLS, "proj.pkg.use.plain", make_pair)] == {
        cs.EdgeResolution.EXACT
    }, edges


JS_READ = (
    "function helper (x) { return x }\n"
    "\n"
    "function read (res, opts) {\n"
    "  var check = opts.verify\n"
    "  var body = JSON.parse(res.text)\n"
    "  var settings = { verify: opts.verify }\n"
    "  var mapped = [1].map(helper)\n"
    "  return [check, body, settings, mapped]\n"
    "}\n"
    "\n"
    "module.exports = read\n"
)


def test_javascript_passed_members_name_no_free_function(temp_repo: Path) -> None:
    edges = _index(
        temp_repo / "proj",
        {
            "lib/text.js": "function text () { return '' }\nmodule.exports = text\n",
            "lib/verify.js": "function verify () {}\nmodule.exports = verify\n",
            "lib/read.js": JS_READ,
        },
        cs.SupportedLanguage.JS,
    )
    read = "proj.lib.read.read"
    # `opts.verify` assigned and stored, `res.text` passed: reads of an
    # untyped value's members, as body-parser's options and responses are.
    callees = _callees(edges, read)
    assert "proj.lib.verify.verify" not in callees, edges
    assert "proj.lib.text.text" not in callees, edges
    # A bare name passed to map() is still the function.
    assert edges[(CALLS, read, "proj.lib.read.helper")] == {cs.EdgeResolution.EXACT}, (
        edges
    )


TS_CLIENT = (
    "import {delay} from './delay';\n"
    "\n"
    "export class Client {\n"
    "\toptions: any;\n"
    "\n"
    "\twait(): number {\n"
    "\t\tnotify();\n"
    "\t\tconst pause = this.options.retry.delay(1);\n"
    "\t\treturn Math.min(pause, delay(2));\n"
    "\t}\n"
    "}\n"
)


def test_typescript_call_keeps_its_own_answer_after_the_factory_pass(
    temp_repo: Path,
) -> None:
    # `notify()` resolves to nothing, so the factory-alias pass reads
    # `pause = this.options.retry.delay(1)`. The call itself is a member of
    # an untyped value, ky's `retry.delay` option: only `delay(2)` reaches
    # the imported function.
    edges = _index(
        temp_repo / "proj",
        {
            "src/delay.ts": (
                "export function delay(ms: number): number {\n\treturn ms;\n}\n"
            ),
            "src/client.ts": TS_CLIENT,
        },
        cs.SupportedLanguage.TS,
    )
    key = (CALLS, "proj.src.client.Client.wait", "proj.src.delay.delay")
    assert edges[key] == {cs.EdgeResolution.EXACT}, edges


DART_UTILS = (
    "int hashAll(Iterable<Object?>? props) {\n"
    "  return _finish(props == null ? 0 : props.fold(0, _combine));\n"
    "}\n"
    "\n"
    "int _combine(int hash, Object? o) => hash ^ o.hashCode;\n"
    "\n"
    "int _finish(int hash) => hash;\n"
)
DART_LIST = (
    "class CustomList<E> {\n"
    "  CustomList(this._list);\n"
    "\n"
    "  final List<E> _list;\n"
    "\n"
    "  T fold<T>(T initialValue, T Function(T previousValue, E element) combine) =>\n"
    "      _list.fold(initialValue, combine);\n"
    "}\n"
)


def test_dart_passed_member_names_no_unrelated_method(temp_repo: Path) -> None:
    # equatable's mapPropsToHashCode: `props.fold` is the SDK's Iterable.fold,
    # not the test helper class's `fold`.
    edges = _index(
        temp_repo / "proj",
        {
            "pubspec.yaml": "name: demo\n",
            "lib/utils.dart": DART_UTILS,
            "lib/custom_list.dart": DART_LIST,
        },
        cs.SupportedLanguage.DART,
    )
    hash_all = "proj.lib.utils.hashAll"
    assert "proj.lib.custom_list.CustomList.fold" not in _callees(edges, hash_all), (
        edges
    )
    assert edges[(REFERENCES, hash_all, "proj.lib.utils._combine")] == {
        cs.EdgeResolution.EXACT
    }, edges


CPP_USE = (
    "#include <algorithm>\n"
    "#include <vector>\n"
    "\n"
    "struct S {\n"
    "  int cb;\n"
    "};\n"
    "\n"
    "void use(std::vector<int>& v, S s) {\n"
    "  std::sort(v.begin(), v.end(), s.cb);\n"
    "}\n"
)


def test_cpp_passed_member_names_no_free_function(temp_repo: Path) -> None:
    edges = _index(
        temp_repo / "proj",
        {
            "a.cpp": "bool cb(int a, int b) { return a < b; }\n",
            "b.cpp": CPP_USE,
        },
        cs.SupportedLanguage.CPP,
    )
    assert "proj.a.cb" not in _callees(edges, "proj.b.use"), edges


def _cs_handlers(namespace: str) -> str:
    return (
        f"namespace {namespace}\n"
        "{\n"
        "    public static class Handlers\n"
        "    {\n"
        "        public static void Handle(int x) { }\n"
        "    }\n"
        "}\n"
    )


CS_USE = (
    "using System.Collections.Generic;\n"
    "using static B.Handlers;\n"
    "\n"
    "namespace C\n"
    "{\n"
    "    public class Use\n"
    "    {\n"
    "        public void Run(List<int> xs)\n"
    "        {\n"
    "            xs.ForEach(Handle);\n"
    "        }\n"
    "    }\n"
    "}\n"
)


def test_csharp_method_group_prefers_the_imported_type(temp_repo: Path) -> None:
    edges = _index(
        temp_repo / "proj",
        {
            "A/Handlers.cs": _cs_handlers("A"),
            "B/Handlers.cs": _cs_handlers("B"),
            "C/Use.cs": CS_USE,
        },
        cs.SupportedLanguage.CSHARP,
    )
    callees = _callees(edges, "proj.C.Use.Use.Run(List)")
    assert callees == {"proj.B.Handlers.Handlers.Handle(int)"}, edges
