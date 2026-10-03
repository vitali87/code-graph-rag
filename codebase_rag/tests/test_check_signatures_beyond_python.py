"""Issue #2517: `cgr check` reports signature changes outside Python.

Only Python ingestion wrote `positional_params`, and the structural delta
reads nothing else, so a TypeScript function that gained a required
parameter was listed under `symbols.changed` while `signature_changes` and
`arity_findings` stayed empty: the unchanged caller no longer type-checked
and nothing said so. TypeScript, JavaScript, Go, Rust, PHP, Java and C# now
store their declared parameters too, with the optionality the signature
spells out (`pad?`, `= 1`, `...rest`), and each call site gets a verdict
under the rules of its language.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tree_sitter import Node
from typer.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.crash_correlation import (
    CYPHER_CRASH_POSITIONAL_PARAMS,
    _CrashGraph,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.call_processor import call_site_properties
from codebase_rag.parsers.positional_params import declared_positional_params
from codebase_rag.services.graph_diff import _SITE_PROPS
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import (
    CallSite,
    Definition,
    StructuralDelta,
    _declared_arity_verdict,
    _filled_counts,
    _site,
    has_findings,
)
from evals.cgr_graph import _StatefulIngestor

PROJECT = "sigs"

Files = dict[str, str]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _write(root: Path, files: Files) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _index(root: Path, files: Files) -> _StatefulIngestor:
    """Commit `files` and index the commit, as the graph `cgr check` expects."""
    root.mkdir()
    _write(root, files)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return store


def _check(root: Path, store: _StatefulIngestor, edits: Files) -> StructuralDelta:
    """What `cgr check` computes after `edits` land in the working tree."""
    _write(root, edits)
    parsers, queries = load_parsers()
    return run_check(root, "HEAD", PROJECT, store, parsers, queries)


def _delta(temp_repo: Path, files: Files, edits: Files) -> StructuralDelta:
    root = temp_repo / PROJECT
    return _check(root, _index(root, files), edits)


def _change(delta: StructuralDelta, suffix: str) -> dict:
    (change,) = [
        c for c in delta["signature_changes"] if c["qualified_name"].endswith(suffix)
    ]
    return dict(change)


def _verdicts(delta: StructuralDelta, suffix: str) -> list[str]:
    return [site["verdict"] for site in _change(delta, suffix)["sites"]]


# --- the languages, each with a callee file and an unchanged caller ----------

TS_UTIL = "export function fmt(n: number): string { return String(n); }\n"
TS_VIEW = (
    'import { fmt } from "./util";\nexport function show(): string { return fmt(1); }\n'
)
JS_UTIL = "export function fmt(n) { return String(n); }\n"
JS_VIEW = 'import { fmt } from "./util";\nexport function show() { return fmt(1); }\n'
GO_MOD = "module example.com/sigs\n\ngo 1.21\n"
# Across packages: a same-package call in another file binds by name alone
# for now, and such a site never gets a definite verdict.
GO_UTIL = 'package util\n\nfunc Fmt(n int) string {\n\treturn ""\n}\n'
GO_MAIN = (
    'package main\n\nimport "example.com/sigs/util"\n\n'
    "func Show() string {\n\treturn util.Fmt(1)\n}\n"
)
RS_CARGO = '[package]\nname = "sigs"\nversion = "0.1.0"\n'
RS_LIB = (
    "pub fn fmt(n: i32) -> i32 {\n    n\n}\n\npub fn show() -> i32 {\n    fmt(1)\n}\n"
)
PHP_LIB = (
    "<?php\nfunction fmt($n) {\n    return $n;\n}\n\n"
    "function show() {\n    return fmt(1);\n}\n"
)
CS_UTIL = (
    "public static class Util {\n"
    '    public static string Fmt(int n, int pad = 0) { return ""; }\n'
    "}\n"
)
CS_VIEW = "public class View {\n    public string Show() { return Util.Fmt(1); }\n}\n"
JAVA_UTIL = (
    'public class Util {\n    public static String fmt(int n) { return "" + n; }\n}\n'
)
JAVA_VIEW = "public class View {\n    public String show() { return Util.fmt(1); }\n}\n"

LANGUAGES: dict[str, Files] = {
    "ts": {"src/util.ts": TS_UTIL, "src/view.ts": TS_VIEW},
    "js": {"src/util.js": JS_UTIL, "src/view.js": JS_VIEW},
    "go": {"go.mod": GO_MOD, "util/util.go": GO_UTIL, "main.go": GO_MAIN},
    "rust": {"Cargo.toml": RS_CARGO, "src/lib.rs": RS_LIB},
    "php": {"lib.php": PHP_LIB},
    "csharp": {"Util.cs": CS_UTIL, "View.cs": CS_VIEW},
    "java": {"src/Util.java": JAVA_UTIL, "src/View.java": JAVA_VIEW},
}


def _edit(language: str, old: str, new: str) -> Files:
    """The language's callee file with `old` replaced by `new`."""
    files = LANGUAGES[language]
    (path,) = [p for p, text in files.items() if old in text]
    return {path: files[path].replace(old, new)}


# --- red: the issue's repro ----------------------------------------------------


def test_a_required_ts_parameter_added_to_a_called_function_is_reported(
    temp_repo: Path,
) -> None:
    delta = _delta(
        temp_repo,
        LANGUAGES["ts"],
        _edit(
            "ts",
            "fmt(n: number): string { return String(n); }",
            "fmt(n: number, pad: number): string { return String(n).padStart(pad); }",
        ),
    )

    change = _change(delta, ".src.util.fmt")
    assert change["before"] == ["n"]
    assert change["after"] == ["n", "pad"]
    (site,) = change["sites"]
    assert site["caller"] == f"{PROJECT}.src.view.show"
    assert site["arg_count"] == 1
    assert site["declared_count"] == 2
    # TypeScript declares optionality in the signature, so one argument for
    # two required parameters is tsc's TS2554, not a hint.
    assert site["verdict"] == cs.DELTA_ARITY_TOO_FEW
    assert has_findings(delta)


def _cli_check(root: Path, store: _StatefulIngestor) -> Result:
    cli_store = MagicMock(wraps=store)
    cli_store.list_projects = MagicMock(return_value=[PROJECT])
    context = MagicMock()
    context.__enter__.return_value = cli_store
    context.__exit__.return_value = False
    with patch("codebase_rag.cli.connect_memgraph", return_value=context):
        return CliRunner().invoke(
            app,
            [
                "check",
                "--repo-path",
                str(root),
                "--project",
                PROJECT,
                "--fail-on-found",
            ],
        )


def test_the_issue_repro_fails_the_gate(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    store = _index(root, LANGUAGES["ts"])
    _write(
        root,
        _edit(
            "ts",
            "fmt(n: number): string { return String(n); }",
            "fmt(n: number, pad: number): string { return String(n).padStart(pad); }",
        ),
    )

    result = _cli_check(root, store)

    assert result.exit_code == 1, result.output
    delta = json.loads(result.stdout)
    assert [c["after"] for c in delta["signature_changes"]] == [["n", "pad"]]


# --- red: every language with an explicit parameter list ---------------------

REQUIRED_ADDED = [
    pytest.param(
        "ts",
        ("fmt(n: number)", "fmt(n: number, pad: number)"),
        (["n"], ["n", "pad"]),
        cs.DELTA_ARITY_TOO_FEW,
        id="typescript",
    ),
    # Plain JavaScript passes `undefined` for a missing argument: a hint.
    pytest.param(
        "js",
        ("fmt(n)", "fmt(n, pad)"),
        (["n"], ["n", "pad"]),
        cs.DELTA_ARITY_POSSIBLY_MISSING,
        id="javascript",
    ),
    pytest.param(
        "go",
        ("Fmt(n int)", "Fmt(n int, pad int)"),
        (["n"], ["n", "pad"]),
        cs.DELTA_ARITY_TOO_FEW,
        id="go",
    ),
    pytest.param(
        "rust",
        ("fmt(n: i32)", "fmt(n: i32, pad: i32)"),
        (["n"], ["n", "pad"]),
        cs.DELTA_ARITY_TOO_FEW,
        id="rust",
    ),
    # PHP raises ArgumentCountError for a missing required argument.
    pytest.param(
        "php",
        ("fmt($n)", "fmt($n, $pad)"),
        (["n"], ["n", "pad"]),
        cs.DELTA_ARITY_TOO_FEW,
        id="php",
    ),
    # A C# method's qualified name carries its parameter types, so a default
    # dropped keeps the node and its callers: the signature is what changed.
    pytest.param(
        "csharp",
        ("int pad = 0", "int pad"),
        (["n", "pad?"], ["n", "pad"]),
        cs.DELTA_ARITY_TOO_FEW,
        id="csharp-default-dropped",
    ),
]


@pytest.mark.parametrize(("language", "edit", "lists", "verdict"), REQUIRED_ADDED)
def test_a_required_parameter_gives_every_site_a_verdict(
    temp_repo: Path,
    language: str,
    edit: tuple[str, str],
    lists: tuple[list[str], list[str]],
    verdict: str,
) -> None:
    delta = _delta(temp_repo, LANGUAGES[language], _edit(language, *edit))

    (change,) = delta["signature_changes"]
    before, after = lists
    assert change["before"] == before
    assert change["after"] == after
    assert [site["verdict"] for site in change["sites"]] == [verdict]
    assert has_findings(delta) is (verdict == cs.DELTA_ARITY_TOO_FEW)


REMOVED = [
    pytest.param(
        "ts",
        ("fmt(n: number)", "fmt()"),
        cs.DELTA_ARITY_TOO_MANY,
        id="typescript",
    ),
    pytest.param("go", ("Fmt(n int)", "Fmt()"), cs.DELTA_ARITY_TOO_MANY, id="go"),
    pytest.param("rust", ("fmt(n: i32)", "fmt()"), cs.DELTA_ARITY_TOO_MANY, id="rust"),
    # A surplus argument is dropped at run time in PHP and JavaScript: the
    # call still runs, so it is no finding.
    pytest.param("php", ("fmt($n)", "fmt()"), cs.DELTA_ARITY_OK, id="php"),
    pytest.param("js", ("fmt(n)", "fmt()"), cs.DELTA_ARITY_OK, id="javascript"),
]


@pytest.mark.parametrize(("language", "edit", "verdict"), REMOVED)
def test_a_removed_parameter_is_too_many_where_the_language_rejects_it(
    temp_repo: Path, language: str, edit: tuple[str, str], verdict: str
) -> None:
    delta = _delta(temp_repo, LANGUAGES[language], _edit(language, *edit))

    (change,) = delta["signature_changes"]
    assert change["after"] is None
    assert [site["verdict"] for site in change["sites"]] == [verdict]
    assert has_findings(delta) is (verdict == cs.DELTA_ARITY_TOO_MANY)


def test_a_renamed_java_parameter_is_a_signature_change(temp_repo: Path) -> None:
    """A Java qualified name carries the parameter types, so only a change
    that keeps them keeps the node; a renamed parameter is one."""
    delta = _delta(
        temp_repo, LANGUAGES["java"], _edit("java", "fmt(int n)", "fmt(int width)")
    )

    change = _change(delta, ".Util.fmt(int)")
    assert (change["before"], change["after"]) == (["n"], ["width"])
    assert _verdicts(delta, ".Util.fmt(int)") == [cs.DELTA_ARITY_OK]
    assert not has_findings(delta)


def test_a_caller_edited_to_pass_too_few_is_an_arity_finding(
    temp_repo: Path,
) -> None:
    files = {
        "src/util.ts": "export function fmt(n: number, pad: number): string {\n"
        "  return String(n).padStart(pad);\n}\n",
        "src/view.ts": TS_VIEW.replace("fmt(1)", "fmt(1, 2)"),
    }
    delta = _delta(temp_repo, files, {"src/view.ts": TS_VIEW})

    (finding,) = delta["arity_findings"]
    assert finding["verdict"] == cs.DELTA_ARITY_TOO_FEW
    assert finding["path"] == "src/view.ts"
    assert has_findings(delta)


# --- negative: what still fits ------------------------------------------------

OPTIONAL_ADDED = [
    pytest.param("ts", ("fmt(n: number)", "fmt(n: number, pad?: number)"), id="ts-?"),
    pytest.param("ts", ("fmt(n: number)", "fmt(n: number, pad = 2)"), id="ts-default"),
    pytest.param(
        "ts", ("fmt(n: number)", "fmt(n: number, ...pads: number[])"), id="ts-rest"
    ),
    pytest.param("js", ("fmt(n)", "fmt(n, pad = 2)"), id="js-default"),
    pytest.param("go", ("Fmt(n int)", "Fmt(n int, pads ...int)"), id="go-variadic"),
    pytest.param("php", ("fmt($n)", "fmt($n, $pad = 2)"), id="php-default"),
    pytest.param("php", ("fmt($n)", "fmt($n, ...$pads)"), id="php-variadic"),
]


@pytest.mark.parametrize(("language", "edit"), OPTIONAL_ADDED)
def test_an_optional_parameter_added_keeps_every_site_ok(
    temp_repo: Path, language: str, edit: tuple[str, str]
) -> None:
    delta = _delta(temp_repo, LANGUAGES[language], _edit(language, *edit))

    (change,) = delta["signature_changes"]
    assert [site["verdict"] for site in change["sites"]] == [cs.DELTA_ARITY_OK]
    assert not has_findings(delta)


def test_a_spread_argument_reads_unknown(temp_repo: Path) -> None:
    """`fmt(...args)` passes as many values as `args` holds; the one written
    argument says nothing about whether two parameters are filled."""
    files = {
        "src/util.ts": TS_UTIL,
        "src/view.ts": 'import { fmt } from "./util";\n'
        "export function show(args: [number, number]): string {\n"
        "  return fmt(...args);\n}\n",
    }
    delta = _delta(
        temp_repo, files, _edit("ts", "fmt(n: number)", "fmt(n: number, pad: number)")
    )

    assert _verdicts(delta, ".src.util.fmt") == [cs.DELTA_ARITY_UNKNOWN]
    assert not has_findings(delta)


def test_a_go_call_passing_a_call_result_reads_unknown(temp_repo: Path) -> None:
    """`Fmt(pair())` passes every result of `pair`, however many it returns."""
    files = {
        "go.mod": GO_MOD,
        "util/util.go": GO_UTIL,
        "main.go": GO_MAIN.replace("util.Fmt(1)", "util.Fmt(pair())")
        + "\nfunc pair() (int, int) {\n\treturn 1, 2\n}\n",
    }
    delta = _delta(temp_repo, files, _edit("go", "Fmt(n int)", "Fmt(n int, pad int)"))

    assert _verdicts(delta, ".util.Fmt") == [cs.DELTA_ARITY_UNKNOWN]
    assert not has_findings(delta)


def test_a_javascript_caller_of_a_typescript_callee_is_only_a_hint(
    temp_repo: Path,
) -> None:
    """Nothing type-checks the JavaScript caller: it runs with `undefined`."""
    files = {"src/util.ts": TS_UTIL, "src/view.js": JS_VIEW}
    delta = _delta(
        temp_repo, files, _edit("ts", "fmt(n: number)", "fmt(n: number, pad: number)")
    )

    assert _verdicts(delta, ".src.util.fmt") == [cs.DELTA_ARITY_POSSIBLY_MISSING]
    assert not has_findings(delta)


def test_a_rust_method_called_through_its_type_passes_the_receiver(
    temp_repo: Path,
) -> None:
    """`S::m(s, 1)` passes the receiver that `s.m(1)` does not: either count
    fits `fn m(&self, a: i32)`, so a renamed parameter leaves the site ok."""
    lib = (
        "pub struct S;\n\nimpl S {\n"
        "    pub fn m(&self, a: i32) -> i32 {\n        a\n    }\n}\n\n"
        "pub fn run(s: &S) -> i32 {\n    S::m(s, 1)\n}\n"
    )
    delta = _delta(
        temp_repo,
        {"Cargo.toml": RS_CARGO, "src/lib.rs": lib},
        {
            "src/lib.rs": lib.replace(
                "a: i32) -> i32 {\n        a", "b: i32) -> i32 {\n        b"
            )
        },
    )

    change = _change(delta, ".S.m")
    assert (change["before"], change["after"]) == (["self", "a"], ["self", "b"])
    assert _verdicts(delta, ".S.m") == [cs.DELTA_ARITY_OK]


def test_a_rust_path_call_past_the_receiver_is_too_many(temp_repo: Path) -> None:
    lib = (
        "pub struct S;\n\nimpl S {\n"
        "    pub fn m(&self, a: i32) -> i32 {\n        a\n    }\n}\n\n"
        "pub fn run(s: &S) -> i32 {\n    S::m(s, 1)\n}\n"
    )
    delta = _delta(
        temp_repo,
        {"Cargo.toml": RS_CARGO, "src/lib.rs": lib},
        {
            "src/lib.rs": lib.replace(
                "&self, a: i32) -> i32 {\n        a", "&self) -> i32 {\n        0"
            )
        },
    )

    assert _verdicts(delta, ".S.m") == [cs.DELTA_ARITY_TOO_MANY]
    assert has_findings(delta)


def test_a_csharp_extension_call_does_not_pass_its_receiver(
    temp_repo: Path,
) -> None:
    """`"x".Ext(1)` fills `int a` alone; `this string s` is the receiver."""
    util = (
        "public static class Util {\n"
        "    public static string Ext(this string s, int a) { return s; }\n"
        "}\n"
    )
    view = 'public class View {\n    public string Show() { return "x".Ext(1); }\n}\n'
    delta = _delta(
        temp_repo,
        {"Util.cs": util, "View.cs": view},
        {"Util.cs": util.replace("int a)", "int b)")},
    )

    change = _change(delta, ".Util.Ext(string, int)")
    assert (change["before"], change["after"]) == (["this s", "a"], ["this s", "b"])
    assert _verdicts(delta, ".Util.Ext(string, int)") == [cs.DELTA_ARITY_OK]


RS_RECEIVER_LIB = (
    "pub struct S;\n\nimpl S {\n"
    "    pub fn m(&self, a: i32) -> i32 {\n        a\n    }\n}\n\n"
    "pub fn run(s: &S) -> i32 {\n    {call}\n}\n"
)


@pytest.mark.parametrize(
    ("call", "verdict"),
    [
        # Greptile's case on #2832: the receiver fills `self`, `1` fills `a`
        # and nothing fills `b`; rustc rejects it.
        pytest.param("S::m(s, 1)", cs.DELTA_ARITY_TOO_FEW, id="path-call-short"),
        pytest.param("S::m(s, 1, 2)", cs.DELTA_ARITY_OK, id="path-call-full"),
        pytest.param("s.m(1, 2)", cs.DELTA_ARITY_OK, id="method-call-full"),
    ],
)
def test_a_rust_receiver_counts_where_the_call_passes_it(
    temp_repo: Path, call: str, verdict: str
) -> None:
    """`m` gains a required `b`; each call form is judged by what it passes."""
    lib = RS_RECEIVER_LIB.replace("{call}", call)
    delta = _delta(
        temp_repo,
        {"Cargo.toml": RS_CARGO, "src/lib.rs": lib},
        {"src/lib.rs": lib.replace("a: i32) -> i32", "a: i32, b: i32) -> i32")},
    )

    assert _verdicts(delta, ".S.m") == [verdict]
    assert has_findings(delta) is (verdict == cs.DELTA_ARITY_TOO_FEW)


# A parameter only Rust could spell `self` as a receiver: elsewhere it is an
# ordinary name, stored as `self` all the same (CodeRabbit on #2832).
CS_SELF_UTIL = (
    "public class Util {\n    public int M(int self, int b = 0) { return b; }\n}\n"
)
CS_SELF_VIEW = (
    "public class View {\n"
    "    public int Two(Util obj) { return obj.M(1, 2); }\n"
    "    public int One(Util obj) { return obj.M(1); }\n"
    "}\n"
)
TS_SELF_UTIL = (
    "export function fmt(self: number, pad?: number): number { return self; }\n"
)
TS_SELF_VIEW = (
    'import { fmt } from "./util";\n'
    "export function two(): number { return fmt(1, 2); }\n"
    "export function one(): number { return fmt(1); }\n"
)


def _verdicts_by_caller(delta: StructuralDelta, suffix: str) -> dict[str, str]:
    return {
        site["caller"].rsplit(cs.SEPARATOR_DOT, 1)[-1]: site["verdict"]
        for site in _change(delta, suffix)["sites"]
    }


def test_a_csharp_parameter_named_self_is_not_a_receiver(temp_repo: Path) -> None:
    """`M(int self, int b)` takes two arguments from `obj.M(...)`."""
    delta = _delta(
        temp_repo,
        {"Util.cs": CS_SELF_UTIL, "View.cs": CS_SELF_VIEW},
        {"Util.cs": CS_SELF_UTIL.replace("int b = 0", "int b")},
    )

    verdicts = _verdicts_by_caller(delta, ".Util.M(int, int)")
    assert verdicts == {
        "Two(Util)": cs.DELTA_ARITY_OK,
        "One(Util)": cs.DELTA_ARITY_TOO_FEW,
    }
    assert has_findings(delta)


def test_a_typescript_parameter_named_self_is_not_a_receiver(
    temp_repo: Path,
) -> None:
    delta = _delta(
        temp_repo,
        {"src/util.ts": TS_SELF_UTIL, "src/view.ts": TS_SELF_VIEW},
        {"src/util.ts": TS_SELF_UTIL.replace("pad?: number", "pad: number")},
    )

    verdicts = _verdicts_by_caller(delta, ".src.util.fmt")
    assert verdicts == {"two": cs.DELTA_ARITY_OK, "one": cs.DELTA_ARITY_TOO_FEW}


@pytest.mark.parametrize(
    ("path", "short"),
    [
        pytest.param("util/util.go", cs.DELTA_ARITY_TOO_FEW, id="go"),
        pytest.param("src/util.js", cs.DELTA_ARITY_POSSIBLY_MISSING, id="js"),
        pytest.param("lib.php", cs.DELTA_ARITY_TOO_FEW, id="php"),
        pytest.param("src/Util.java", cs.DELTA_ARITY_TOO_FEW, id="java"),
    ],
)
def test_a_parameter_named_self_counts_outside_rust(path: str, short: str) -> None:
    """Each extractor stores an ordinary `self` parameter as `self`."""
    definition = _receiver_definition()._replace(
        label=cs.NodeLabel.FUNCTION.value,
        path=path,
        positional_params=("self", "b"),
    )
    site = _receiver_site(2, None)._replace(caller_path=path)

    assert _declared_arity_verdict(site, definition) == (2, cs.DELTA_ARITY_OK)
    short_site = site._replace(arg_count=1)
    assert _declared_arity_verdict(short_site, definition) == (2, short)


@pytest.mark.parametrize(
    ("entry", "path"),
    [
        pytest.param("this s", "src/lib.rs", id="this-marker-in-rust"),
        pytest.param("this s", "src/util.ts", id="this-marker-in-typescript"),
        pytest.param("self", "Util.cs", id="self-marker-in-csharp"),
        pytest.param("self", "", id="unknown-language"),
    ],
)
def test_a_receiver_marker_counts_only_in_its_own_language(
    entry: str, path: str
) -> None:
    """Rust's `self` and C#'s `this name` mark a receiver nowhere else."""
    definition = _receiver_definition()._replace(
        path=path, positional_params=(entry, "b")
    )
    site = _receiver_site(2, None)._replace(caller_path=path)

    assert _declared_arity_verdict(site, definition)[0] == 2


CS_EXT_UTIL = (
    "public static class Util {\n"
    "    public static string Ext(this string s, int a, int b = 0) { return s; }\n"
    "}\n"
)


@pytest.mark.parametrize(
    ("call", "verdict"),
    [
        # Greptile's case on #2832: called through its class, the extension
        # method takes the receiver as its first argument.
        pytest.param("Util.Ext(s, 1)", cs.DELTA_ARITY_TOO_FEW, id="static-short"),
        pytest.param("Util.Ext(s, 1, 2)", cs.DELTA_ARITY_OK, id="static-full"),
        pytest.param("s.Ext(1, 2)", cs.DELTA_ARITY_OK, id="instance-full"),
        # Through a literal the call binds by name alone: no definite claim.
        pytest.param('"x".Ext(1)', cs.DELTA_ARITY_UNKNOWN, id="instance-heuristic"),
    ],
)
def test_a_csharp_extension_receiver_counts_where_the_call_passes_it(
    temp_repo: Path, call: str, verdict: str
) -> None:
    """Dropping `b`'s default keeps the qn `Ext(string, int, int)`."""
    view = (
        "public class View {\n"
        f"    public string Show(string s) {{ return {call}; }}\n}}\n"
    )
    delta = _delta(
        temp_repo,
        {"Util.cs": CS_EXT_UTIL, "View.cs": view},
        {"Util.cs": CS_EXT_UTIL.replace("int b = 0", "int b")},
    )

    assert _verdicts(delta, ".Util.Ext(string, int, int)") == [verdict]
    assert has_findings(delta) is (verdict == cs.DELTA_ARITY_TOO_FEW)


@pytest.mark.parametrize(
    ("view", "verdict"),
    [
        # Greptile on #2832: a string named like the extension's class is
        # still a value, and `Util.Ext(1, 2)` an instance call that fits.
        pytest.param(
            "    public string Show(string Util) { return Util.Ext(1, 2); }\n",
            cs.DELTA_ARITY_OK,
            id="parameter-named-like-the-class",
        ),
        pytest.param(
            '    string Util = "x";\n'
            "    public string Show() { return Util.Ext(1, 2); }\n",
            cs.DELTA_ARITY_OK,
            id="field-named-like-the-class",
        ),
    ],
)
def test_a_csharp_value_receiver_is_never_counted_as_an_argument(
    temp_repo: Path, view: str, verdict: str
) -> None:
    files = {"Util.cs": CS_EXT_UTIL, "View.cs": f"public class View {{\n{view}}}\n"}
    delta = _delta(
        temp_repo, files, {"Util.cs": CS_EXT_UTIL.replace("int b = 0", "int b")}
    )

    assert _verdicts(delta, ".Util.Ext(string, int, int)") == [verdict]
    assert not has_findings(delta)


@pytest.mark.parametrize(
    ("param", "body", "verdict"),
    [
        # Greptile on #2832: a local in a sibling block is out of scope, so
        # `Util` names the class and `s` fills the receiver, leaving `b` out.
        pytest.param(
            "s",
            '{ var Util = "x"; } return Util.Ext(s, 1);',
            cs.DELTA_ARITY_TOO_FEW,
            id="sibling-block-local",
        ),
        pytest.param(
            "s",
            'if (s == "") { var Util = "x"; } return Util.Ext(s, 1);',
            cs.DELTA_ARITY_TOO_FEW,
            id="branch-local",
        ),
        # A case guard's pattern variable binds into its own section only.
        pytest.param(
            "s",
            "switch (s.Length) { case 0 when s is string Util: break; "
            "default: return Util.Ext(s, 1); } return s;",
            cs.DELTA_ARITY_TOO_FEW,
            id="case-guard-in-another-section",
        ),
        # The parameter's scope encloses the nested block: an instance call.
        pytest.param(
            "Util",
            "{ return Util.Ext(1, 2); }",
            cs.DELTA_ARITY_OK,
            id="parameter-around-a-nested-block",
        ),
    ],
)
def test_a_csharp_local_names_a_value_only_inside_its_scope(
    temp_repo: Path, param: str, body: str, verdict: str
) -> None:
    view = (
        "public class View {\n"
        f"    public string Show(string {param}) {{ {body} }}\n}}\n"
    )
    delta = _delta(
        temp_repo,
        {"Util.cs": CS_EXT_UTIL, "View.cs": view},
        {"Util.cs": CS_EXT_UTIL.replace("int b = 0", "int b")},
    )

    assert _verdicts(delta, ".Util.Ext(string, int, int)") == [verdict]
    assert has_findings(delta) is (verdict == cs.DELTA_ARITY_TOO_FEW)


@pytest.mark.parametrize(
    ("source", "verdict"),
    [
        # Greptile on #2832: a local string named like the extension's class
        # makes `Util.Ext(1, 2)` an instance call that fits.
        pytest.param(
            'void M() { var Util = "x"; Util.Ext(1, 2); }',
            cs.DELTA_ARITY_OK,
            id="local-named-like-the-class",
        ),
        # CodeRabbit on #2832: `?.` takes the receiver from the value before it.
        pytest.param(
            "void M(string s) { s?.Ext(1, 2); }",
            cs.DELTA_ARITY_OK,
            id="conditional-access",
        ),
        pytest.param(
            "void M(string s) { s?.Ext(1); }",
            cs.DELTA_ARITY_TOO_FEW,
            id="conditional-access-short",
        ),
        # Under `using static` a bare call passes the receiver as an argument.
        pytest.param(
            "void M(string s) { Ext(s, 1); }", cs.DELTA_ARITY_TOO_FEW, id="bare-short"
        ),
        pytest.param(
            "void M(string s) { Ext(s, 1, 2); }", cs.DELTA_ARITY_OK, id="bare-full"
        ),
        pytest.param(
            "void M(string s) { Util.Ext(s, 1); }",
            cs.DELTA_ARITY_TOO_FEW,
            id="through-the-class",
        ),
        # Greptile on #2832: the sibling block's `Util` is out of scope here.
        pytest.param(
            'void M(string s) { { var Util = "x"; } Util.Ext(s, 1); }',
            cs.DELTA_ARITY_TOO_FEW,
            id="through-the-class-past-a-sibling-block-local",
        ),
    ],
)
def test_a_csharp_site_counts_its_receiver_as_written(
    source: str, verdict: str
) -> None:
    """The recorded site against `Ext(this string s, int a, int b)`.

    The resolver binds none of the first four on main, so the site is built
    from the call as ingestion records it rather than read from a graph.
    """
    call = _first(
        _call_tree(cs.SupportedLanguage.CSHARP, f"class V {{ {source} }}"),
        frozenset({"invocation_expression"}),
    )
    props = call_site_properties(call)
    arg_count, qualifier = props[cs.KEY_ARG_COUNT], props.get(cs.KEY_CALL_QUALIFIER)
    assert isinstance(arg_count, int)
    site = _receiver_site(arg_count, None)._replace(
        caller_path="View.cs",
        call_qualifier=qualifier if isinstance(qualifier, str) else None,
    )
    definition = _receiver_definition()._replace(
        qualified_name="p.Util.Util.Ext(string, int, int)",
        path="Util.cs",
        positional_params=("this s", "a", "b"),
    )

    assert _declared_arity_verdict(site, definition)[1] == verdict


def test_a_bare_csharp_extension_call_that_fits_stays_ok(temp_repo: Path) -> None:
    """Through the graph: `using static Util;` and `Ext(s, 1, 2)`."""
    view = (
        "using static Util;\n\npublic class View {\n"
        "    public string Show(string s) { return Ext(s, 1, 2); }\n}\n"
    )
    delta = _delta(
        temp_repo,
        {"Util.cs": CS_EXT_UTIL, "View.cs": view},
        {"Util.cs": CS_EXT_UTIL.replace("int b = 0", "int b")},
    )

    assert _verdicts(delta, ".Util.Ext(string, int, int)") == [cs.DELTA_ARITY_OK]


def _receiver_site(arg_count: int, qualifier: str | None) -> CallSite:
    return CallSite(
        caller="p.lib.run",
        caller_path="src/lib.rs",
        rel=cs.RelationshipType.CALLS.value,
        resolution=cs.EdgeResolution.EXACT.value,
        spread_args=False,
        call_qualifier=qualifier,
        callee="p.lib.S.m",
        callee_path="src/lib.rs",
        line=1,
        col=0,
        arg_count=arg_count,
        kwarg_names=(),
        star_args=False,
    )


def _receiver_definition() -> Definition:
    return Definition(
        label=cs.NodeLabel.METHOD.value,
        qualified_name="p.lib.S.m",
        name="m",
        path="src/lib.rs",
        start_line=1,
        end_line=2,
        positional_params=("self", "a", "b"),
        fingerprint="",
        fingerprint_nodes=0,
        branches=frozenset(),
    )


@pytest.mark.parametrize(
    ("arg_count", "verdict"),
    [
        # Two arguments fill `a, b` as a method call, `self, a` as a path
        # call: the forms disagree, so nothing is claimed.
        pytest.param(2, cs.DELTA_ARITY_UNKNOWN, id="forms-disagree"),
        # Three overflow `a, b` and fill `self, a, b`: again no claim.
        pytest.param(3, cs.DELTA_ARITY_UNKNOWN, id="forms-disagree-high"),
        pytest.param(0, cs.DELTA_ARITY_TOO_FEW, id="short-either-way"),
        pytest.param(4, cs.DELTA_ARITY_TOO_MANY, id="over-either-way"),
    ],
)
def test_an_unreadable_call_form_keeps_only_a_verdict_both_forms_share(
    arg_count: int, verdict: str
) -> None:
    site = _receiver_site(arg_count, None)

    assert _declared_arity_verdict(site, _receiver_definition())[1] == verdict


def test_an_undecided_csharp_name_keeps_only_a_verdict_both_forms_share() -> None:
    """`Other.Ext(1)` binds no visible value and names another type: an
    inherited member or an alias, so neither form is assumed."""
    site = _receiver_site(1, "Other")._replace(caller_path="View.cs")
    definition = _receiver_definition()._replace(
        qualified_name="p.Util.Util.Ext(string, int)",
        path="Util.cs",
        positional_params=("this s", "a"),
    )

    assert _declared_arity_verdict(site, definition)[1] == cs.DELTA_ARITY_UNKNOWN
    explicit = site._replace(call_qualifier="Util")
    implicit = site._replace(call_qualifier="")
    assert _declared_arity_verdict(explicit, definition)[1] == cs.DELTA_ARITY_TOO_FEW
    assert _declared_arity_verdict(implicit, definition)[1] == cs.DELTA_ARITY_OK


@pytest.mark.parametrize(
    ("receiver", "qualifier", "counts"),
    [
        pytest.param(False, None, (2, 2), id="no-receiver"),
        # `S::m(s, a)` or `s.m(a, b)`: the receiver may be among the two.
        pytest.param(True, None, (2, 1), id="undecided-form"),
        pytest.param(True, "S", (1, 1), id="receiver-written"),
        pytest.param(True, "", (2, 2), id="receiver-implicit"),
    ],
)
def test_the_filled_counts_are_one_pair_whatever_the_call_form(
    receiver: bool, qualifier: str | None, counts: tuple[int, int]
) -> None:
    site = _receiver_site(2, qualifier)

    assert _filled_counts(2, site, _receiver_definition(), receiver) == counts


@pytest.mark.parametrize(
    ("row", "qualifier"),
    [
        pytest.param({cs.KEY_CALL_QUALIFIER: "Util"}, "Util", id="type-name"),
        pytest.param({cs.KEY_CALL_QUALIFIER: ""}, "", id="value"),
        pytest.param({cs.KEY_CALL_QUALIFIER: None}, None, id="null"),
        pytest.param({cs.KEY_CALL_QUALIFIER: 3}, None, id="not-a-string"),
        pytest.param({}, None, id="absent"),
    ],
)
def test_a_graph_row_keeps_only_a_string_call_qualifier(
    row: dict[str, object], qualifier: str | None
) -> None:
    assert _site(row).call_qualifier == qualifier  # type: ignore[arg-type]


def test_a_python_signature_change_is_reported_as_before(temp_repo: Path) -> None:
    """Python keeps its rules: no defaults recorded, so fewer is a hint."""
    files = {
        "lib.py": "def send(msg):\n    return msg\n",
        "app.py": 'from lib import send\n\n\ndef run():\n    return send("hi")\n',
    }
    delta = _delta(
        temp_repo, files, {"lib.py": files["lib.py"].replace("(msg)", "(msg, channel)")}
    )

    change = _change(delta, ".lib.send")
    assert (change["before"], change["after"]) == (["msg"], ["msg", "channel"])
    assert _verdicts(delta, ".lib.send") == [cs.DELTA_ARITY_POSSIBLY_MISSING]
    assert not has_findings(delta)


def test_a_graph_indexed_before_the_lists_reports_no_phantom_change(
    temp_repo: Path,
) -> None:
    """A graph whose non-Python definitions carry no list yet must not read
    every touched definition as a signature change on the first check."""
    root = temp_repo / PROJECT
    files = {
        "src/util.ts": TS_UTIL + "export function other(a: number) { return a; }\n",
        "src/view.ts": TS_VIEW,
    }
    store = _index(root, files)
    for props in store.nodes.values():
        props.pop(cs.KEY_POSITIONAL_PARAMS, None)

    delta = _check(
        root,
        store,
        {"src/util.ts": files["src/util.ts"].replace("String(n)", "`${n}`")},
    )

    assert delta["signature_changes"] == []
    assert delta["symbols"]["changed"] == [f"{PROJECT}.src.util.fmt"]


def test_an_unchanged_signature_is_not_listed(temp_repo: Path) -> None:
    delta = _delta(
        temp_repo,
        LANGUAGES["ts"],
        _edit("ts", "return String(n);", "return `${n}`;"),
    )

    assert delta["symbols"]["changed"] == [f"{PROJECT}.src.util.fmt"]
    assert delta["signature_changes"] == []


def test_a_language_without_declared_optionality_stays_unread(
    temp_repo: Path,
) -> None:
    """Lua lets any call pass any number of arguments; nothing is stored."""
    files = {
        "lib.lua": "local M = {}\n\nfunction M.fmt(n)\n  return n\nend\n\nreturn M\n"
    }
    delta = _delta(
        temp_repo, files, {"lib.lua": files["lib.lua"].replace("(n)", "(n, pad)")}
    )

    assert delta["signature_changes"] == []


# --- the stored lists ------------------------------------------------------------


def _first(root: Node, node_types: frozenset[str]) -> Node:
    stack = [root]
    while stack:
        node = stack.pop(0)
        if node.type in node_types:
            return node
        stack.extend(node.named_children)
    raise AssertionError(f"no {sorted(node_types)} in the snippet")


DEFINITIONS = frozenset(
    {
        "function_declaration",
        "method_declaration",
        "method_definition",
        "function_definition",
        "function_item",
        "arrow_function",
        "constructor_declaration",
    }
)

STORED = [
    pytest.param(
        cs.SupportedLanguage.TS,
        "function f(this: K, n: number, pad?: number, d = 1, "
        "{ a, b }: O, ...rest: number[]) {}",
        ["n", "pad?", "d?", "{ a, b }", "...rest"],
        id="typescript",
    ),
    pytest.param(
        cs.SupportedLanguage.JS,
        "function f(n, pad = 1, [x], ...rest) {}",
        ["n", "pad?", "[x]", "...rest"],
        id="javascript",
    ),
    pytest.param(cs.SupportedLanguage.JS, "const g = x => x;", ["x"], id="js-arrow"),
    pytest.param(
        cs.SupportedLanguage.GO,
        "package p\nfunc f(a, b int, _ string, xs ...int) {}",
        ["a", "b", "_", "...xs"],
        id="go",
    ),
    pytest.param(
        cs.SupportedLanguage.GO,
        "package p\nfunc (t T) f(int, string) {}",
        ["_", "_"],
        id="go-unnamed",
    ),
    pytest.param(
        cs.SupportedLanguage.RUST,
        "impl S { fn m(&mut self, a: i32, (x, y): (i32, i32)) {} }",
        ["self", "a", "(x, y)"],
        id="rust",
    ),
    pytest.param(
        cs.SupportedLanguage.RUST,
        "impl S { fn n(self: Box<Self>, b: u8) {} }",
        ["self", "b"],
        id="rust-typed-self",
    ),
    pytest.param(
        cs.SupportedLanguage.PHP,
        "<?php\nfunction f(int $n, ?int $p = null, &$r, int ...$rest) {}",
        ["n", "p?", "r", "...rest"],
        id="php",
    ),
    pytest.param(
        cs.SupportedLanguage.JAVA,
        "class K { void f(final int a, String... xs) {} }",
        ["a", "...xs"],
        id="java",
    ),
    pytest.param(
        cs.SupportedLanguage.CSHARP,
        "static class K { static void F(this string s, int a = 1, ref int r, "
        "params int[] xs) {} }",
        ["this s", "a?", "r", "...xs"],
        id="csharp",
    ),
]


@pytest.mark.parametrize(("language", "source", "expected"), STORED)
def test_the_declared_list_marks_optional_and_rest_parameters(
    language: cs.SupportedLanguage, source: str, expected: list[str]
) -> None:
    parsers, _queries = load_parsers()
    tree = parsers[language].parse(source.encode())

    node = _first(tree.root_node, DEFINITIONS)

    assert declared_positional_params(node, language) == expected


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("function f(a: string): void;", id="overload"),
        pytest.param("declare function f(a: number): void;", id="ambient"),
        pytest.param("class K { m(a: string): void; m(a: any) {} }", id="method"),
        pytest.param("interface I { n(a: number): void; }", id="interface"),
        pytest.param(
            "abstract class A { abstract q(x: number): void; }", id="abstract"
        ),
    ],
)
def test_a_bodiless_ts_signature_gets_no_list(source: str) -> None:
    parsers, _queries = load_parsers()
    tree = parsers[cs.SupportedLanguage.TS].parse(source.encode())

    node = _first(
        tree.root_node,
        frozenset(
            {"function_signature", "method_signature", "abstract_method_signature"}
        ),
    )

    assert declared_positional_params(node, cs.SupportedLanguage.TS) is None


@pytest.mark.parametrize(
    "language",
    [
        cs.SupportedLanguage.LUA,
        cs.SupportedLanguage.C,
        cs.SupportedLanguage.CPP,
        cs.SupportedLanguage.SCALA,
        cs.SupportedLanguage.DART,
        cs.SupportedLanguage.PYTHON,
    ],
)
def test_other_languages_get_no_declared_list(
    language: cs.SupportedLanguage,
) -> None:
    """Absent, never empty: C/C++ defaults live on the header declaration,
    Scala and Dart have named and curried parameter lists a count cannot
    check, and Lua accepts any count. Python has its own extractor."""
    parsers, _queries = load_parsers()
    source = {
        cs.SupportedLanguage.LUA: "function f(a, b) end",
        cs.SupportedLanguage.C: "int f(int a) { return a; }",
        cs.SupportedLanguage.CPP: "int f(int a, int b = 2) { return a; }",
        cs.SupportedLanguage.SCALA: "object A { def f(a: Int, b: Int = 2): Int = a }",
        cs.SupportedLanguage.DART: "int f(int a, [int b = 2]) => a;",
        cs.SupportedLanguage.PYTHON: "def f(a, b=1):\n    return a\n",
    }[language]
    tree = parsers[language].parse(source.encode())

    assert declared_positional_params(tree.root_node, language) is None


def test_ingestion_stores_the_list_on_every_covered_definition(
    temp_repo: Path,
) -> None:
    store = _index(
        temp_repo / PROJECT,
        {
            "src/util.ts": "export function fmt(n: number, pad?: number) { return n; }\n"
            "export const arrow = (a: number, ...rest: number[]) => a;\n"
            "export class K {\n  m(this: K, a = 1) { return a; }\n}\n",
            "lib.php": "<?php\nclass P {\n  public function m($a, $b = 1) { return $a; }\n}\n",
            "mod.lua": "local function f(a)\n  return a\nend\n",
        },
    )

    stored = {
        str(props[cs.KEY_QUALIFIED_NAME]): props.get(cs.KEY_POSITIONAL_PARAMS)
        for (label, _uid), props in store.nodes.items()
        if label in (cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value)
    }
    assert stored[f"{PROJECT}.src.util.fmt"] == ["n", "pad?"]
    assert stored[f"{PROJECT}.src.util.arrow"] == ["a", "...rest"]
    assert stored[f"{PROJECT}.src.util.K.m"] == ["a?"]
    assert stored[f"{PROJECT}.lib.P.m"] == ["a", "b?"]
    assert stored[f"{PROJECT}.mod.f"] is None


# --- the recorded site ---------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "source", "spread"),
    [
        pytest.param(cs.SupportedLanguage.TS, "f(a, ...xs);", True, id="ts-spread"),
        pytest.param(cs.SupportedLanguage.JS, "f(...xs);", True, id="js-spread"),
        pytest.param(
            cs.SupportedLanguage.PHP, "<?php\nf(1, ...$xs);", True, id="php-unpack"
        ),
        pytest.param(
            cs.SupportedLanguage.GO,
            "package p\nfunc m() { f(a, xs...) }",
            True,
            id="go-variadic",
        ),
        pytest.param(
            cs.SupportedLanguage.GO,
            "package p\nfunc m() { f(pair()) }",
            True,
            id="go-lone-call",
        ),
        pytest.param(
            cs.SupportedLanguage.GO,
            "package p\nfunc m() { f(a, g()) }",
            None,
            id="go-call-beside-another",
        ),
        pytest.param(
            cs.SupportedLanguage.C,
            "int m(void) { return f(g()); }",
            None,
            id="c-lone-call",
        ),
        pytest.param(
            cs.SupportedLanguage.TS, "html`<p>${a}</p>`;", True, id="ts-tagged-template"
        ),
        pytest.param(cs.SupportedLanguage.TS, "f(a, [...xs]);", None, id="ts-nested"),
        pytest.param(cs.SupportedLanguage.JS, "f(a, b);", None, id="js-plain"),
        pytest.param(
            cs.SupportedLanguage.PYTHON, "f(a, *xs)\n", None, id="python-left-alone"
        ),
    ],
)
def test_the_site_records_a_spread_argument(
    language: cs.SupportedLanguage, source: str, spread: bool | None
) -> None:
    props = call_site_properties(
        _first(
            _call_tree(language, source),
            frozenset({"call_expression", "function_call_expression", "call"}),
        )
    )

    assert props.get(cs.KEY_SPREAD_ARGS) is spread


def _call_tree(language: cs.SupportedLanguage, source: str) -> Node:
    parsers, _queries = load_parsers()
    return parsers[language].parse(source.encode()).root_node


@pytest.mark.parametrize(
    ("language", "source", "qualifier"),
    [
        pytest.param(
            cs.SupportedLanguage.RUST, "fn r() { S::m(s, 1); }", "S", id="rs-path"
        ),
        pytest.param(
            cs.SupportedLanguage.RUST, "fn r() { Self::m(s); }", "Self", id="rs-self"
        ),
        pytest.param(
            cs.SupportedLanguage.RUST,
            "fn r() { crate::a::S::m(s); }",
            "S",
            id="rs-long-path",
        ),
        pytest.param(
            cs.SupportedLanguage.RUST,
            "fn r() { S::m::<u8>(s); }",
            "S",
            id="rs-turbofish",
        ),
        pytest.param(
            cs.SupportedLanguage.RUST,
            "fn r() { <S as T>::m(&s); }",
            "<S as T>",
            id="rs-qself",
        ),
        pytest.param(
            cs.SupportedLanguage.RUST, "fn r() { s.m(1); }", "", id="rs-method"
        ),
        pytest.param(cs.SupportedLanguage.RUST, "fn r() { f(1); }", None, id="rs-bare"),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { Util.Ext("x", 1); } }',
            "Util",
            id="cs-class",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { NS.Util.Ext("x"); } }',
            "Util",
            id="cs-qualified-class",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { s.Ext(1); } }",
            "s",
            id="cs-variable",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { "x".Ext(1); } }',
            "",
            id="cs-literal",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { this.Ext(1); } }",
            "",
            id="cs-this",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { Ext("x", 1); } }',
            None,
            id="cs-bare",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { var Util = "x"; Util.Ext(1); } }',
            "",
            id="cs-local-named-like-a-class",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(string Util) { Util.Ext(1); } }",
            "",
            id="cs-parameter",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { string Util; void M() { Util.Ext(1); } }",
            "",
            id="cs-field",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { string Util { get; } void M() { Util.Ext(1); } }",
            "",
            id="cs-property",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V(string Util) { void M() { Util.Ext(1); } }",
            "",
            id="cs-primary-constructor",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { foreach (var Util in xs) { Util.Ext(1); } } }",
            "",
            id="cs-foreach",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(string s) { s?.Ext(1); } }",
            "",
            id="cs-conditional-access",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { this.Util.Ext(1); } }",
            "",
            id="cs-this-member",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { global::Util.Ext("x"); } }',
            "Util",
            id="cs-global-alias",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { Ext<int>("x"); } }',
            None,
            id="cs-generic-bare",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class W { string Util; } class V { void M() { Util.Ext(1); } }",
            "Util",
            id="cs-another-types-field",
        ),
        # Greptile on #2832: a binder whose scope does not hold the call.
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { { var Util = "x"; } Util.Ext("x"); } }',
            "Util",
            id="cs-sibling-block-local",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { foreach (var Util in xs) {} Util.Ext(1); } }",
            "Util",
            id="cs-foreach-after-the-loop",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { try {} catch (E Util) {} Util.Ext(1); } }",
            "Util",
            id="cs-catch-after-the-clause",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { Func<int, int> f = Util => 1; Util.Ext(1); } }",
            "Util",
            id="cs-lambda-parameter-outside-the-lambda",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { void F(string Util) {} Util.Ext(1); } }",
            "Util",
            id="cs-local-function-parameter-outside-it",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { while (o is string Util) {} Util.Ext(1); } }",
            "Util",
            id="cs-while-pattern-after-the-loop",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            '{ var Util = "x"; }\nUtil.Ext("x");\n',
            "Util",
            id="cs-top-level-sibling-block-local",
        ),
        # Greptile's case on #2832. An iteration statement is a declaration
        # space of its own (ECMA-334 §7.3), so its condition's pattern
        # variable is out of scope after the loop.
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { while (!(o is string Util)) "
            '{ o = "x"; } Util.Ext(1, 2); } }',
            "Util",
            id="cs-negated-while-pattern-after-the-loop",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { for (; o is string Util; ) {} Util.Ext(1); } }",
            "Util",
            id="cs-for-condition-pattern-after-the-loop",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { using (var Util = x) {} Util.Ext(1); } }",
            "Util",
            id="cs-using-after-the-statement",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { try {} catch (E e) when (e.D is string Util) {} "
            "Util.Ext(1); } }",
            "Util",
            id="cs-catch-filter-after-the-clause",
        ),
        # A case label's pattern and `when` guard bind into their section
        # alone; an embedded statement is its own declaration space.
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { switch (o) { case string Util: break; "
            'default: Util.Ext("x"); break; } } }',
            "Util",
            id="cs-case-pattern-in-another-section",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { switch (o) { case string s when s is "
            'var Util: break; default: Util.Ext("x"); break; } } }',
            "Util",
            id="cs-when-guard-in-another-section",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { if (c) b = o is string Util; "
            'Util.Ext("x"); } }',
            "Util",
            id="cs-if-embedded-statement-pattern",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { if (c) {} else b = o is string Util; "
            'Util.Ext("x"); } }',
            "Util",
            id="cs-else-embedded-statement-pattern",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { lock (o) b = o is string Util; "
            'Util.Ext("x"); } }',
            "Util",
            id="cs-lock-body-pattern",
        ),
        # A binder whose scope holds the call is still a value.
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'class V { void M() { var Util = "x"; { Util.Ext(1); } } }',
            "",
            id="cs-enclosing-block-local",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { if (o is not string Util) return; "
            "Util.Ext(1); } }",
            "",
            id="cs-if-pattern-in-the-enclosing-block",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { Func<string, string> f = Util => Util.Ext(1); } }",
            "",
            id="cs-lambda-parameter",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { try {} catch (E Util) { Util.Ext(1); } } }",
            "",
            id="cs-catch",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(int n) { switch (n) { case 1: var Util = "
            '"x"; break; default: Util = "y"; Util.Ext(1); break; } } }',
            "",
            id="cs-switch-section-local",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            'var Util = "x";\n{ Util.Ext(1); }\n',
            "",
            id="cs-top-level-local",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { for (; o is string Util; ) { Util.Ext(1); } } }",
            "",
            id="cs-for-condition-pattern",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M() { try {} catch (E e) when (e.D is string Util) "
            "{ Util.Ext(1); } } }",
            "",
            id="cs-catch-filter",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { switch (o) { case string Util: "
            "Util.Ext(1); break; } } }",
            "",
            id="cs-case-pattern-in-its-section",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { switch (o) { case string s when s is "
            "var Util: Util.Ext(1); break; } } }",
            "",
            id="cs-when-guard-in-its-section",
        ),
        # A `lock` expression's pattern variable, like an `if` condition's,
        # binds into the enclosing block.
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { lock (o is string Util ? o : o) {} "
            "Util.Ext(1); } }",
            "",
            id="cs-lock-expression-pattern-in-the-enclosing-block",
        ),
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            "class V { void M(object o) { if (c) {} else if (o is string Util) "
            "Util.Ext(1); } }",
            "",
            id="cs-else-if-pattern",
        ),
        pytest.param(
            cs.SupportedLanguage.CPP, "int r() { return s.f(1); }", None, id="cpp"
        ),
        pytest.param(cs.SupportedLanguage.TS, "o.m(1);", None, id="ts"),
    ],
)
def test_the_site_records_what_a_call_is_written_through(
    language: cs.SupportedLanguage, source: str, qualifier: str | None
) -> None:
    node = _first(
        _call_tree(language, source),
        frozenset({"call_expression", "invocation_expression"}),
    )

    assert call_site_properties(node).get(cs.KEY_CALL_QUALIFIER) == qualifier


def test_the_delta_reads_the_site_shape_back_from_the_graph() -> None:
    assert "r.spread_args AS spread_args" in cq.CYPHER_DELTA_SITES
    assert "r.call_qualifier AS call_qualifier" in cq.CYPHER_DELTA_SITES
    assert "r.resolution AS resolution" in cq.CYPHER_DELTA_SITES


def test_the_spread_flag_is_location_not_structure() -> None:
    assert cs.KEY_SPREAD_ARGS in _SITE_PROPS
    assert cs.KEY_CALL_QUALIFIER in _SITE_PROPS


def test_a_call_to_an_overloaded_ts_function_is_not_judged(
    temp_repo: Path,
) -> None:
    """`fmt(1, 2)` matches the second overload; the call binds to the first
    signature, whose one parameter says nothing about it."""
    util = (
        "export function fmt(n: number): string;\n"
        "export function fmt(n: number, pad: number): string;\n"
        "export function fmt(n: number, pad?: number): string {\n"
        "  return String(n);\n}\n"
    )
    delta = _delta(
        temp_repo,
        {"src/util.ts": util, "src/view.ts": TS_VIEW},
        {"src/view.ts": TS_VIEW.replace("fmt(1)", "fmt(1, 2)")},
    )

    assert delta["arity_findings"] == []
    assert not has_findings(delta)


def test_a_heuristic_binding_never_gives_a_definite_verdict(
    temp_repo: Path,
) -> None:
    """`$x->render(1)` bound by name alone may not call this `render`."""
    lib = (
        "<?php\nclass A {\n    public function render($a) { return $a; }\n}\n\n"
        "function show($x) {\n    return $x->render(1);\n}\n"
    )
    delta = _delta(
        temp_repo,
        {"lib.php": lib},
        {"lib.php": lib.replace("render($a)", "render($a, $b)")},
    )

    assert _verdicts(delta, ".lib.A.render") == [cs.DELTA_ARITY_UNKNOWN]
    assert not has_findings(delta)


# --- crash correlation keeps to Python ----------------------------------------


def test_a_traceback_diagnosis_reads_only_python_signatures() -> None:
    """A Python TypeError never comes from a TypeScript function, so its
    stored list must not corroborate one."""
    rows = [
        {"qn": "p.web.fmt", "positional_params": ["n", "pad?"], cs.KEY_PATH: "web.ts"},
        {"qn": "p.lib.send", "positional_params": ["msg"], cs.KEY_PATH: "lib.py"},
    ]

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        return rows if query == CYPHER_CRASH_POSITIONAL_PARAMS else []

    with patch("codebase_rag.trace.ingest.load_callables", return_value=[]):
        graph = _CrashGraph(fetch_all, "p")

    assert graph.positional_params == {"p.lib.send": ("msg",)}
