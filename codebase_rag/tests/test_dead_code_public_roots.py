# `cgr dead-code` roots every public or exported symbol, which suits a
# library: its API is called from outside the repository. In an application
# nearly every function is public by convention, yet only a few entry points
# run it, so that rule hid almost all dead code and only `_private` helpers
# could ever be reported (issue #2527). `--no-public-roots` drops the rule;
# entry points, tests, decorated handlers, runtime hooks, `--entry-point` and
# `--decorator-root` still root what they reach.
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.dead_code import (
    collect_dead_code,
    dead_code_from_graph,
    default_dead_code_config,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.types_defs import (
    DeadCodeConfig,
    PropertyDict,
    PropertyValue,
    ResultRow,
)
from codec import schema_pb2 as pb
from evals.cgr_graph import _CapturingIngestor

_PROJECT = "proj"
_PREFIX = _PROJECT + cs.SEPARATOR_DOT

_Graph = tuple[
    dict[tuple[str, PropertyValue], PropertyDict],
    list[tuple[str, PropertyValue, str, str, PropertyValue]],
]

_LANGUAGES = (
    cs.SupportedLanguage.PYTHON,
    cs.SupportedLanguage.TS,
    cs.SupportedLanguage.JAVA,
    cs.SupportedLanguage.CSHARP,
    cs.SupportedLanguage.DART,
    cs.SupportedLanguage.GO,
)

_FIXTURE: dict[str, str] = {
    "svc/__init__.py": "",
    # The issue's tasks/jobs.py, plus the neighbours each root rule covers.
    "svc/jobs.py": """\
def registry(fn):
    return fn


@celery_app.task
def nightly_cleanup():
    return _purge()


def _purge():
    return 0


@registry
def plugin_hook():
    return 1


def truly_unused():
    return _only_from_unused()


def _only_from_unused():
    return 2


def exercised_by_tests():
    return 3


class Report:
    def __repr__(self):
        return self._label()

    def _label(self):
        return "report"

    def render(self):
        return 4
""",
    # A console script's target: `[project.scripts]` calls it, nothing here.
    "svc/cli.py": """\
def main():
    return serve()


def serve():
    return 5


class Command:
    def main(self):
        return 6

    def build(self):
        def main():
            return 8

        return main


def build_command():
    def main():
        return 7

    return main
""",
    "svc/tool.py": """\
def start():
    return _boot()


def _boot():
    return 8


if __name__ == "__main__":
    start()
""",
    "tests/test_jobs.py": """\
from svc.jobs import exercised_by_tests


def test_exercised():
    assert exercised_by_tests() == 3
""",
    "web/index.ts": """\
export class Svc {
  constructor() {}
  go(): number { return 1; }
  stale(): number { return 2; }
}

export function start(): number {
  const s = new Svc();
  return s.go();
}

export function neverCalled(): number {
  return 3;
}

export function main(): number {
  return 4;
}

start();
""",
    "java/app/App.java": """\
package app;

public class App {
    public App() {}

    public static void main(String[] args) {
        App app = new App();
        app.run();
    }

    public void run() {
        helper();
    }

    private void helper() {}

    public void unusedPublic() {}

    public static int main(int code) {
        return code;
    }
}
""",
    "cs/Program.cs": """\
namespace Demo;

public class Program
{
    public Program() {}

    static void Main(string[] args)
    {
        var p = new Program();
        p.Run();
    }

    public void Run() {}

    public void Unused() {}
}
""",
    "dartapp/main.dart": """\
void main(List<String> args) {
  run();
}

void run() {}

void neverCalled() {}
""",
    # Dart registers a local function under its module, as it does a
    # top-level one, so only the parser can tell this `main` is local.
    "dartapp/tool.dart": """\
void outer() {
  void main() {
    helper();
  }

  main();
}

void helper() {}
""",
    "gocmd/main.go": """\
package main

func main() {
	run()
}

func run() {}

func Unused() {}
""",
}

# Public symbols nothing calls: application mode must report every one.
_UNUSED_PUBLIC = (
    "proj.svc.jobs.truly_unused",
    "proj.svc.jobs.Report.render",
    "proj.svc.cli.build_command",
    "proj.svc.cli.Command.build",
    "proj.web.index.neverCalled",
    "proj.web.index.Svc.stale",
    "proj.java.app.App.App.unusedPublic()",
    "proj.cs.Program.Demo.Program.Unused",
    "proj.dartapp.main.neverCalled",
    "proj.dartapp.tool.outer",
    "proj.gocmd.main.Unused",
)

# Reached from a root that is not a public-symbol root, so live in both modes.
_LIVE_IN_BOTH_MODES = (
    # A decorated task handler and the private helper it calls.
    "proj.svc.jobs.nightly_cleanup",
    "proj.svc.jobs._purge",
    # Called at module load to apply `@registry`.
    "proj.svc.jobs.registry",
    # Reached only from a test.
    "proj.svc.jobs.exercised_by_tests",
    "proj.tests.test_jobs.test_exercised",
    # A dunder method, and what it calls.
    "proj.svc.jobs.Report.__repr__",
    "proj.svc.jobs.Report._label",
    # An `if __name__ == "__main__":` block's call.
    "proj.svc.tool.start",
    "proj.svc.tool._boot",
    # Program entry points.
    "proj.svc.cli.main",
    "proj.svc.cli.serve",
    "proj.web.index.start",
    "proj.web.index.Svc.constructor",
    "proj.web.index.Svc.go",
    "proj.java.app.App.App.main(String[])",
    "proj.java.app.App.App.App()",
    "proj.java.app.App.App.run()",
    "proj.java.app.App.App.helper()",
    "proj.cs.Program.Demo.Program.Main(string[])",
    "proj.cs.Program.Demo.Program.Program",
    "proj.cs.Program.Demo.Program.Run",
    "proj.dartapp.main.main",
    "proj.dartapp.main.run",
    "proj.gocmd.main.main",
    "proj.gocmd.main.run",
)


def _parse(
    tmp_path_factory: pytest.TempPathFactory,
    files: dict[str, str],
    languages: tuple[cs.SupportedLanguage, ...],
) -> _Graph:
    # Parsed once per module, so this runs before the per-test autouse
    # fixtures: it must skip a missing grammar, keep off the developer's
    # vector store and pin the C# frontend itself.
    parsers, queries = load_parsers()
    missing = [str(lang) for lang in languages if lang not in parsers]
    if missing:
        pytest.skip(f"{', '.join(missing)} parser not available")
    root = tmp_path_factory.mktemp("public_roots") / _PROJECT
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    ingestor = _CapturingIngestor()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)
        mp.setattr(settings, "QDRANT_URL", None)
        mp.setattr(
            settings, "QDRANT_DB_PATH", str(tmp_path_factory.mktemp("qdrant-iso"))
        )
        GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=_PROJECT,
            skip_embeddings=True,
        ).run(force=True)
    return ingestor.nodes, list(ingestor.rels)


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> _Graph:
    return _parse(tmp_path_factory, _FIXTURE, _LANGUAGES)


# One class per file: `main(String args[])` and `main(String arg)` record the
# same parameter type, `String`, and so the same qualified name.
_JAVA_MAINS: dict[str, str] = {
    "CStyle": "public static void main(String args[])",
    "Spaced": "public static void main(String [] args)",
    "Varargs": "public static void main(String... args)",
    "SpacedVarargs": "public static void main(final String ... args)",
    "Qualified": "public static void main(java.lang.String[] a)",
    "QualifiedCStyle": "public static void main(java.lang.String args[])",
    "Annotated": "public static void main(String @NonNull [] a)",
    "Instance": "public void main()",
    "Scalar": "public static void main(String arg)",
    "IntMain": "public static int main(String[] a)",
    "TwoDims": "public static void main(String[][] a)",
    "CStyleTwoDims": "public static void main(String[] args[])",
    "ArrayVarargs": "public static void main(String[]... a)",
    "Objects": "public static void main(Object[] a)",
    "TwoParams": "public static void main(String[] a, String b)",
    "PackagePrivate": "static void main(String[] args)",
    # Nothing to launch: no body.
    "AbstractMain": "public abstract void main(String[] args);",
    "InterfaceMain": "public void main(String[] args);",
    # A concrete static main in an abstract class launches as usual.
    "AbstractStatic": "public static void main(String[] args)",
    # A private main is no candidate.
    "PrivateMain": "private static void main(String[] args)",
    # An instance main runs on an object the launcher builds with a
    # non-private zero-argument constructor; the implicit default counts.
    "ProtectedInstance": "protected void main()",
    "ArgsOnlyConstructor": "public void main(String[] args)",
    "PrivateConstructor": "public void main()",
    "AbstractInstance": "public void main(String[] args)",
    "InterfaceDefault": "default void main()",
}
_JAVA_TYPE_HEADERS = {
    "AbstractMain": "public abstract class",
    "AbstractStatic": "public abstract class",
    "AbstractInstance": "public abstract class",
    "InterfaceMain": "public interface",
    "InterfaceDefault": "public interface",
}
_JAVA_EXTRA_MEMBERS = {
    "ArgsOnlyConstructor": "ArgsOnlyConstructor(int x) {}",
    "PrivateConstructor": "private PrivateConstructor() {}",
}
_JAVA_ENTRY_CLASSES = (
    "CStyle",
    "Spaced",
    "Varargs",
    "SpacedVarargs",
    "Qualified",
    "QualifiedCStyle",
    "Annotated",
    "Instance",
    "AbstractStatic",
    "ProtectedInstance",
    # The launcher starts a package-private main too.
    "PackagePrivate",
)
_JAVA_MAIN_DECLARATIONS = _JAVA_ENTRY_CLASSES
# Not exported, so library mode does not root them: the exported rule reads
# a class member written without `public` or `protected` as not public. An
# interface member is public without a modifier (JLS 9.4, #2749), so
# `InterfaceDefault` roots as public code.
_JAVA_NOT_EXPORTED = ("PackagePrivate", "PrivateMain")


def _java_main_source(name: str, signature: str) -> str:
    if signature.endswith(";"):
        body = ""
    else:
        body = "{ return helper(); }" if " int " in signature else "{ helper(); }"
    header = _JAVA_TYPE_HEADERS.get(name, "public class")
    extra = _JAVA_EXTRA_MEMBERS.get(name)
    members = f"    {extra}\n\n" if extra else ""
    return (
        f"package j;\n\n{header} {name} {{\n{members}    {signature} {body}\n\n"
        "    private static int helper() { return 0; }\n}\n"
    )


# A class declaring both overloads: the launcher picks a non-private
# `main(String[])`, static or not (JEP 512), and keeps it even when it cannot
# run it. Each entry: the two mains, an extra member, and which one runs.
_JAVA_OVERLOADED_MAINS: dict[str, tuple[str, str, str, str | None]] = {
    "BothMains": ("public void main(String[] args)", "public void main()", "", "args"),
    "StaticAndInstance": (
        "public static void main(String[] args)",
        "public void main()",
        "",
        "args",
    ),
    # A private `main(String[])` is no candidate, so `main()` runs.
    "PrivateArgs": (
        "private void main(String[] args)",
        "public void main()",
        "",
        "noargs",
    ),
    # The chosen instance `main(String[])` has no object to run on, so the
    # launch fails rather than fall back to `main()`.
    "UnlaunchableArgs": (
        "public void main(String[] args)",
        "public static void main()",
        "UnlaunchableArgs(int x) {}",
        None,
    ),
}


def _overloaded_mains_source(
    name: str, with_args: str, without_args: str, extra: str
) -> str:
    members = f"    {extra}\n\n" if extra else ""
    return (
        f"package j;\n\npublic class {name} {{\n{members}"
        f"    {with_args} {{ helper(); }}\n\n"
        f"    {without_args} {{ other(); }}\n\n"
        "    private static int helper() { return 0; }\n\n"
        "    private static int other() { return 0; }\n}\n"
    )


@pytest.fixture(scope="module")
def java_graph(tmp_path_factory: pytest.TempPathFactory) -> _Graph:
    files = {
        f"j/{name}.java": _java_main_source(name, signature)
        for name, signature in _JAVA_MAINS.items()
    }
    files.update(
        (f"j/{name}.java", _overloaded_mains_source(name, *spec[:3]))
        for name, spec in _JAVA_OVERLOADED_MAINS.items()
    )
    return _parse(tmp_path_factory, files, (cs.SupportedLanguage.JAVA,))


def _java_main_qn(graph: _Graph, name: str) -> str:
    nodes, _ = graph
    prefix = f"proj.j.{name}.{name}.main("
    matches = [str(uid) for _, uid in nodes if str(uid).startswith(prefix)]
    assert len(matches) == 1, (name, matches)
    return matches[0]


def _config(
    *,
    public_roots: bool | None = None,
    include_tests: bool = True,
    entry_points: tuple[str, ...] = (),
    decorator_roots: frozenset[str] = frozenset(),
) -> DeadCodeConfig:
    config = default_dead_code_config(include_tests, include_classes=False)
    config = config._replace(
        entry_points=entry_points,
        root_decorators=config.root_decorators | decorator_roots,
    )
    if public_roots is None:
        return config
    return config._replace(public_roots=public_roots)


def _dead(graph: _Graph, config: DeadCodeConfig) -> set[str]:
    nodes, rels = graph
    return dead_code_from_graph(nodes, rels, _PREFIX, config)


class TestApplicationMode:
    def test_reports_the_issues_truly_unused(self, graph: _Graph) -> None:
        dead = _dead(graph, _config(public_roots=False))

        assert "proj.svc.jobs.truly_unused" in dead, sorted(dead)

    def test_reports_exactly_the_unreached_code(self, graph: _Graph) -> None:
        dead = _dead(graph, _config(public_roots=False))

        assert dead == {
            *_UNUSED_PUBLIC,
            "proj.svc.jobs._only_from_unused",
            "proj.svc.jobs.plugin_hook",
            "proj.svc.cli.Command.main",
            "proj.svc.cli.build_command.main",
            "proj.svc.cli.Command.build.main",
            "proj.dartapp.tool.main",
            "proj.dartapp.tool.helper",
            "proj.web.index.main",
            "proj.java.app.App.App.main(int)",
        }

    @pytest.mark.parametrize("qn", _UNUSED_PUBLIC)
    def test_reports_unused_public_code_in_every_language(
        self, graph: _Graph, qn: str
    ) -> None:
        dead = _dead(graph, _config(public_roots=False))

        assert qn in dead, sorted(dead)

    def test_reports_a_private_helper_only_unused_public_code_reaches(
        self, graph: _Graph
    ) -> None:
        # Library mode keeps `_only_from_unused` alive through its public
        # caller, so it can never be reported there.
        dead = _dead(graph, _config(public_roots=False))

        assert "proj.svc.jobs._only_from_unused" in dead, sorted(dead)

    def test_an_undeclared_decorator_does_not_root(self, graph: _Graph) -> None:
        # `@registry` is the project's own decorator, not a built-in root.
        dead = _dead(graph, _config(public_roots=False))

        assert "proj.svc.jobs.plugin_hook" in dead, sorted(dead)

    @pytest.mark.parametrize(
        "qn",
        [
            # Only a module-level Python `main` is a console script's target.
            "proj.svc.cli.Command.main",
            "proj.svc.cli.build_command.main",
            "proj.svc.cli.Command.build.main",
            # Only a top-level Dart `main` is the program's entry, and the
            # helper only a local `main` calls is dead with it.
            "proj.dartapp.tool.main",
            "proj.dartapp.tool.helper",
            # JS and TS have no `main` convention.
            "proj.web.index.main",
            # The JVM calls `void main()` or `void main(String[])` only.
            "proj.java.app.App.App.main(int)",
        ],
    )
    def test_a_main_that_is_no_program_entry_is_reported(
        self, graph: _Graph, qn: str
    ) -> None:
        dead = _dead(graph, _config(public_roots=False))

        assert qn in dead, sorted(dead)


class TestJavaMainFromItsDeclaration:
    # The JVM starts a `void main` taking nothing or one String array,
    # however the array is spelled; the recorded parameter types cannot
    # tell `String args[]` from a scalar `String arg`, so the declaration
    # decides.
    @pytest.mark.parametrize("name", _JAVA_ENTRY_CLASSES)
    def test_an_entry_signature_roots_its_call_tree(
        self, java_graph: _Graph, name: str
    ) -> None:
        dead = _dead(java_graph, _config(public_roots=False))

        assert _java_main_qn(java_graph, name) not in dead, sorted(dead)
        assert f"proj.j.{name}.{name}.helper()" not in dead, sorted(dead)

    @pytest.mark.parametrize(
        "name", [name for name in _JAVA_MAINS if name not in _JAVA_ENTRY_CLASSES]
    )
    def test_any_other_main_is_reported(self, java_graph: _Graph, name: str) -> None:
        dead = _dead(java_graph, _config(public_roots=False))

        assert _java_main_qn(java_graph, name) in dead, sorted(dead)
        assert f"proj.j.{name}.{name}.helper()" in dead, sorted(dead)

    @pytest.mark.parametrize("name", list(_JAVA_MAINS))
    def test_the_declaration_is_recorded_on_the_node(
        self, java_graph: _Graph, name: str
    ) -> None:
        nodes, _ = java_graph
        qn = _java_main_qn(java_graph, name)
        props = nodes[(cs.NodeLabel.METHOD.value, qn)]

        assert (props.get(cs.KEY_IS_PROGRAM_MAIN) is True) == (
            name in _JAVA_MAIN_DECLARATIONS
        ), props

    @pytest.mark.parametrize("name", list(_JAVA_MAINS))
    def test_library_mode_is_unchanged(self, java_graph: _Graph, name: str) -> None:
        dead = _dead(java_graph, _config())

        # Every public main roots as public code; the package-private and
        # private ones are reported, as on main.
        assert (_java_main_qn(java_graph, name) in dead) == (
            name in _JAVA_NOT_EXPORTED
        ), sorted(dead)

    @pytest.mark.parametrize("name", list(_JAVA_OVERLOADED_MAINS))
    @pytest.mark.parametrize(
        ("kind", "signature", "helper"),
        [("args", "main(String[])", "helper()"), ("noargs", "main()", "other()")],
    )
    def test_only_the_main_the_launcher_runs_roots(
        self, java_graph: _Graph, name: str, kind: str, signature: str, helper: str
    ) -> None:
        nodes, _ = java_graph
        qn = f"proj.j.{name}.{name}.{signature}"
        runs = _JAVA_OVERLOADED_MAINS[name][3] == kind

        dead = _dead(java_graph, _config(public_roots=False))

        assert (qn not in dead) == runs, sorted(dead)
        assert (f"proj.j.{name}.{name}.{helper}" not in dead) == runs, sorted(dead)
        props = nodes[(cs.NodeLabel.METHOD.value, qn)]
        assert (props.get(cs.KEY_IS_PROGRAM_MAIN) is True) == runs, props

    @pytest.mark.parametrize("name", list(_JAVA_OVERLOADED_MAINS))
    def test_overloads_root_as_public_code_in_library_mode(
        self, java_graph: _Graph, name: str
    ) -> None:
        with_args, without_args = _JAVA_OVERLOADED_MAINS[name][:2]
        dead = _dead(java_graph, _config())

        for signature, written in (
            ("main(String[])", with_args),
            ("main()", without_args),
        ):
            exported = not written.startswith("private")
            qn = f"proj.j.{name}.{name}.{signature}"
            assert (qn not in dead) == exported, sorted(dead)


class TestProgramMainMark:
    def test_only_the_top_level_dart_main_is_marked(self, graph: _Graph) -> None:
        nodes, _ = graph
        function = cs.NodeLabel.FUNCTION.value

        top_level = nodes[(function, "proj.dartapp.main.main")]
        local = nodes[(function, "proj.dartapp.tool.main")]

        assert top_level.get(cs.KEY_IS_PROGRAM_MAIN) is True
        assert local.get(cs.KEY_IS_PROGRAM_MAIN) is not True

    def test_the_marks_travel_in_a_protobuf_export(self, tmp_path: Path) -> None:
        # Both marks decide a launcher main's root, so an exported index
        # without them reads every such main as plain unused code.
        root = tmp_path / _PROJECT
        files = {
            "j/Launcher.java": _java_main_source(
                "Launcher", "public static void main(String args[])"
            ),
            "j/Scalar.java": _java_main_source(
                "Scalar", "public static void main(String arg)"
            ),
            "dartapp/main.dart": _FIXTURE["dartapp/main.dart"],
            "dartapp/tool.dart": _FIXTURE["dartapp/tool.dart"],
        }
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        out = tmp_path / "export"
        out.mkdir()
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=ProtobufFileIngestor(str(out), split_index=False),
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=_PROJECT,
            skip_embeddings=True,
        ).run(force=True)

        index = pb.GraphCodeIndex()
        index.ParseFromString((out / cs.PROTOBUF_INDEX_FILE).read_bytes())
        methods = {
            node.method.qualified_name: node.method
            for node in index.nodes
            if node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) == "method"
        }
        functions = {
            node.function.qualified_name: node.function
            for node in index.nodes
            if node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) == "function"
        }

        launcher = methods["proj.j.Launcher.Launcher.main(String)"]
        scalar = methods["proj.j.Scalar.Scalar.main(String)"]
        assert (launcher.is_exported, launcher.is_program_main) == (True, True)
        assert (scalar.is_exported, scalar.is_program_main) == (True, False)
        assert functions["proj.dartapp.main.main"].is_program_main is True
        assert functions["proj.dartapp.tool.main"].is_program_main is False


class TestRootsThatStayInApplicationMode:
    @pytest.mark.parametrize("qn", _LIVE_IN_BOTH_MODES)
    def test_live_without_public_roots(self, graph: _Graph, qn: str) -> None:
        nodes, _ = graph
        assert any(uid == qn for _, uid in nodes), f"fixture lost {qn}"

        dead = _dead(graph, _config(public_roots=False))

        assert qn not in dead, sorted(dead)

    def test_entry_point_option_still_roots(self, graph: _Graph) -> None:
        dead = _dead(
            graph,
            _config(public_roots=False, entry_points=("jobs.truly_unused",)),
        )

        assert "proj.svc.jobs.truly_unused" not in dead
        assert "proj.svc.jobs._only_from_unused" not in dead

    def test_decorator_root_option_still_roots(self, graph: _Graph) -> None:
        dead = _dead(
            graph,
            _config(public_roots=False, decorator_roots=frozenset({"registry"})),
        )

        assert "proj.svc.jobs.plugin_hook" not in dead

    def test_no_include_tests_still_applies(self, graph: _Graph) -> None:
        dead = _dead(graph, _config(public_roots=False, include_tests=False))

        # Only a test reaches it, and tests are no longer roots.
        assert "proj.svc.jobs.exercised_by_tests" in dead
        # A test is not reported as dead production code.
        assert "proj.tests.test_jobs.test_exercised" not in dead


class TestLibraryModeIsUnchanged:
    def test_public_roots_is_the_default(self) -> None:
        config = default_dead_code_config(include_tests=True, include_classes=False)

        assert config.public_roots is True

    @pytest.mark.parametrize("qn", _UNUSED_PUBLIC)
    def test_default_still_roots_public_code(self, graph: _Graph, qn: str) -> None:
        dead = _dead(graph, _config())

        assert qn not in dead, sorted(dead)

    def test_default_still_keeps_what_public_code_reaches(self, graph: _Graph) -> None:
        dead = _dead(graph, _config())

        assert "proj.svc.jobs._only_from_unused" not in dead
        assert "proj.svc.jobs.plugin_hook" not in dead

    @pytest.mark.parametrize("qn", _LIVE_IN_BOTH_MODES)
    def test_default_keeps_the_other_roots(self, graph: _Graph, qn: str) -> None:
        dead = _dead(graph, _config())

        assert qn not in dead, sorted(dead)

    def test_default_reports_nothing_in_the_fixture(self, graph: _Graph) -> None:
        assert _dead(graph, _config()) == set()

    def test_explicit_public_roots_matches_the_default(self, graph: _Graph) -> None:
        assert _dead(graph, _config(public_roots=True)) == _dead(graph, _config())


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        return self._nodes if query == cq.CYPHER_DEAD_CODE_NODES else self._rels


def _row(qn: str, path: str, *, label: str = "Function") -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": path,
        "start_line": 1,
        "end_line": 2,
        "decorators": [],
        "is_exported": label != "Module",
        "overrides_external": False,
    }


def _ruby_and_python_rows() -> list[ResultRow]:
    return [
        _row("proj.lib.orders", "lib/orders.rb", label="Module"),
        _row("proj.lib.orders.process_order", "lib/orders.rb"),
        _row("proj.app", "app.py", label="Module"),
        _row("proj.app.truly_unused", "app.py"),
    ]


def test_structural_tier_symbols_stay_roots_without_public_roots() -> None:
    # The ast-grep tier emits no CALLS edges, so nothing could ever reach its
    # symbols: dropping their root would report every one of them.
    config = _config(public_roots=False)

    rows = collect_dead_code(FakeIngestor(_ruby_and_python_rows(), []), "proj", config)

    assert [row["qualified_name"] for row in rows] == ["proj.app.truly_unused"]


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return " ".join(_ANSI_RE.sub("", output).split())


def _mock_ingestor(rows: list[ResultRow]) -> MagicMock:
    mock = MagicMock()
    mock.list_projects.return_value = ["proj"]
    mock.fetch_all.side_effect = lambda query, params=None: (
        rows if query == cq.CYPHER_DEAD_CODE_NODES else []
    )
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _invoke(rows: list[ResultRow], *args: str) -> tuple[int, str]:
    with patch("codebase_rag.cli.connect_memgraph", return_value=_mock_ingestor(rows)):
        result = CliRunner().invoke(app, ["dead-code", *args])
    return result.exit_code, result.output


_PUBLIC_ONLY = [
    _row("proj.app", "app.py", label="Module"),
    _row("proj.app.truly_unused", "app.py"),
]


class TestCommand:
    def test_no_public_roots_reports_unused_public_code(self) -> None:
        code, output = _invoke(_PUBLIC_ONLY, "--no-public-roots", "--format", "json")

        assert code == 0, output
        assert [row["qualified_name"] for row in json.loads(output)] == [
            "proj.app.truly_unused"
        ]

    def test_default_reports_nothing(self) -> None:
        code, output = _invoke(_PUBLIC_ONLY, "--format", "json")

        assert code == 0, output
        assert json.loads(output) == []

    def test_public_roots_flag_matches_the_default(self) -> None:
        code, output = _invoke(_PUBLIC_ONLY, "--public-roots", "--format", "json")

        assert code == 0, output
        assert json.loads(output) == []

    def test_help_states_the_root_rule(self) -> None:
        result = CliRunner().invoke(app, ["dead-code", "--help"])

        plain = _plain(result.output)
        assert result.exit_code == 0
        assert "--no-public-roots" in plain
        assert "public or exported symbol is a root" in plain

    def test_clean_report_names_the_public_roots_rule(self) -> None:
        code, output = _invoke(_PUBLIC_ONLY)

        plain = _plain(output)
        assert code == 0, output
        assert cs.CLI_DEADCODE_NONE in plain
        assert cs.CLI_DEADCODE_RULE_PUBLIC_ROOTS in plain

    def test_clean_application_report_names_its_rule(self) -> None:
        code, output = _invoke(
            _PUBLIC_ONLY, "--no-public-roots", "-e", "app.truly_unused"
        )

        plain = _plain(output)
        assert code == 0, output
        assert cs.CLI_DEADCODE_NONE in plain
        assert cs.CLI_DEADCODE_RULE_NO_PUBLIC_ROOTS in plain
        assert cs.CLI_DEADCODE_RULE_PUBLIC_ROOTS not in plain

    def test_json_clean_report_stays_parseable(self) -> None:
        code, output = _invoke(_PUBLIC_ONLY, "--format", "json")

        assert code == 0, output
        assert json.loads(output) == []

    @pytest.mark.parametrize(
        ("args", "rule", "other"),
        [
            pytest.param(
                (),
                cs.CLI_DEADCODE_RULE_PUBLIC_ROOTS,
                cs.CLI_DEADCODE_RULE_NO_PUBLIC_ROOTS,
                id="public-roots",
            ),
            pytest.param(
                ("--no-public-roots", "-e", "app.truly_unused"),
                cs.CLI_DEADCODE_RULE_NO_PUBLIC_ROOTS,
                cs.CLI_DEADCODE_RULE_PUBLIC_ROOTS,
                id="no-public-roots",
            ),
        ],
    )
    def test_saved_clean_report_names_its_rule(
        self, tmp_path: Path, args: tuple[str, ...], rule: str, other: str
    ) -> None:
        report = tmp_path / "report.txt"

        code, output = _invoke(_PUBLIC_ONLY, *args, "-o", str(report))

        saved = _plain(report.read_text(encoding="utf-8"))
        assert code == 0, output
        assert rule in saved
        assert other not in saved
        assert rule in _plain(output)

    def test_saved_report_with_candidates_names_no_rule(self, tmp_path: Path) -> None:
        report = tmp_path / "report.txt"

        code, output = _invoke(_PUBLIC_ONLY, "--no-public-roots", "-o", str(report))

        saved = _plain(report.read_text(encoding="utf-8"))
        assert code == 0, output
        assert "proj.app.truly_unused" in saved
        assert cs.CLI_DEADCODE_RULE_NO_PUBLIC_ROOTS not in saved

    def test_saved_clean_json_report_stays_a_list(self, tmp_path: Path) -> None:
        report = tmp_path / "report.json"

        code, output = _invoke(_PUBLIC_ONLY, "--format", "json", "-o", str(report))

        assert code == 0, output
        assert json.loads(report.read_text(encoding="utf-8")) == []

    def test_saved_report_is_unchanged(self, tmp_path: Path) -> None:
        report = tmp_path / "report.json"

        code, output = _invoke(
            _PUBLIC_ONLY, "--no-public-roots", "--format", "json", "-o", str(report)
        )

        assert code == 0, output
        rows = json.loads(report.read_text(encoding="utf-8"))
        assert [row["qualified_name"] for row in rows] == ["proj.app.truly_unused"]
