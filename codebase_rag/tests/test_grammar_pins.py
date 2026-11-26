"""The grammars compiled into cgr-graph-rs, held to the Python packages cgr pins.

The crate compiles Go's grammar and the patched C and C++ forks in
(rust/cgr_graph_rs/src/grammars), and the parser loader hands py-tree-sitter
the C and C++ ones on every install (codebase_rag/crate_grammars.py). So each
compiled grammar must be exactly the release or commit `uv.lock` holds for
its Python package, and must be the same grammar, not just the same version
(docs/reports/PHASE2_FRAME.md, section 11): these compare the pins in
`uv.lock`, `pyproject.toml`, `Cargo.toml`, `Cargo.lock` and the built
extension, then each grammar's structure as py-tree-sitter reads the Python
package and as the crate reads its own, then whole trees parsed by the two
runtimes, and last where the loader takes each language from.

C's and C++'s Python packages are the forks in a checkout ([tool.uv.sources])
and are no longer loaded anywhere; they stay the independent copy the
compiled grammars are compared with. A base install has no grammar package,
so the comparisons that need one skip there, as other grammar tests do.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from unittest.mock import patch

import pytest
from packaging.requirements import Requirement
from tree_sitter import Language, Node, Parser

from codebase_rag import constants as cs
from codebase_rag import crate_grammars, parser_loader
from codebase_rag.crate_grammars import (
    crate_grammar,
    crate_language,
    grammar_fingerprint,
    native_fingerprint,
)
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.parsers.native_trees import describe

REPO_ROOT = Path(__file__).resolve().parents[2]
CRATE_DIR = REPO_ROOT / "rust" / "cgr_graph_rs"
BUMP_GUIDE = "see AGENTS.md, 'Bumping a compiled grammar'"
# The crates every grammar build needs that are not grammars themselves.
RUNTIME_CRATES = frozenset({"tree-sitter", "tree-sitter-language"})


class Compiled(NamedTuple):
    # The grammar's Python distribution and its crate. They share a name for
    # these three; SQL's and Markdown's will not (PHASE2_FRAME.md, section 11).
    dist: str
    crate: str


COMPILED: dict[str, Compiled] = {
    cs.SupportedLanguage.C: Compiled("tree-sitter-c", "tree-sitter-c"),
    cs.SupportedLanguage.CPP: Compiled("tree-sitter-cpp", "tree-sitter-cpp"),
    cs.SupportedLanguage.GO: Compiled("tree-sitter-go", "tree-sitter-go"),
}

# The shape of issue #555: a block comment between two tokens of a macro's
# value. Upstream tree-sitter-c has one `preproc_arg` for the value, so the
# rest matches no rule and the function after it is lost in an ERROR.
COMMENTED_MACRO = (
    b"#define TEN(x)   \\\n  do {           \\\n    f(x);        \\\n"
    b"    /*comment*/; \\\n  } while (0)\n\nvoid after(void) {}\n"
)

# Sources for each compiled grammar that reach what tends to break when one
# grammar is read by two runtimes: external scanners (C++ raw strings, Go's
# automatic semicolons), error recovery, MISSING nodes, fields, aliases,
# non-ASCII text, CRLF and an empty file.
SOURCES: dict[str, list[bytes]] = {
    cs.SupportedLanguage.C: [
        COMMENTED_MACRO,
        b"#include <stdio.h>\n#define M(x) ((x) + 1) /* c */\n"
        b"#if defined(A) && B > 2\nstatic int g = M(1);\n#elif C\n"
        b"extern const char *s;\n#else\nint h(void);\n#endif\n",
        b"typedef struct node { struct node *next; int v[4]; } node_t;\n"
        b"enum e { A = 1 << 2, B };\nunion u { float f; unsigned u; };\n",
        'int main(int argc, char **argv) {\r\n  const char *s = "é\\n";\r\n'
        "  for (int i = 0; i < argc; i++) { switch (i) { case 0: break;"
        " default: goto out; } }\r\nout:\r\n  return argc ? 0 : -1;\r\n}\r\n".encode(),
        b"int f(int a { return a +; }\nint g( {\n",
        b"",
    ],
    cs.SupportedLanguage.CPP: [
        COMMENTED_MACRO,
        b"template <typename T, int N = 3>\nconcept Small = sizeof(T) < N;\n"
        b"template <Small T> auto sum(const std::vector<T>& xs) -> T {\n"
        b"  return std::accumulate(xs.begin(), xs.end(), T{});\n}\n",
        b"namespace a::b { class C : public D<int>, private E {\n public:\n"
        b"  C() = default;\n  virtual ~C() override;\n"
        b"  C& operator+=(const C& o) noexcept;\n"
        b"  [[nodiscard]] static constexpr int k() { return 1; }\n};\n}\n",
        b'auto s = R"delim(raw " ) string)delim";\n'
        b"auto f = [&, x = 1](auto&&... ys) mutable { return (ys + ... + x); };\n"
        b"auto [p, q] = std::pair{1, 2};\n",
        "class K {\n#ifdef X\n  int a;\n#else\n  long a;\n#endif\n"
        '  const char* n = u8"é";\n};\n'.encode(),
        b"template <class T> struct S { void f() { if (x) { } ; } ;\n",
    ],
    cs.SupportedLanguage.GO: [
        b'package p\n\nimport (\n\t"fmt"\n\tx "strings"\n)\n\n'
        b'type Pair[K comparable, V any] struct {\n\tKey K `json:"k"`\n\tVal V\n}\n\n'
        b'func (p *Pair[K, V]) String() string { return fmt.Sprint(p.Key, x.ToUpper("\xc3\xa9")) }\n',
        b"package p\n\nfunc f(ch chan<- int, xs ...int) (n int, err error) {\n"
        b"\tdefer func() { recover() }()\n\tselect {\n\tcase ch <- 1:\n\tdefault:\n\t}\n"
        b"\tfor i := range 10 {\n\t\tn += i\n\t}\n\tgo f(ch)\n\treturn\n}\n",
        b"package p\n\nvar s = `raw\nstring`\nconst (\n\tA = iota\n\tB\n)\n",
        b"package p\r\n\r\nfunc g() {\r\n\tswitch v := x.(type) {\r\n\tcase int:\r\n\t}\r\n}\r\n",
        b"package p\n\nfunc broken( {\n\tx :=\n}\n",
        b"",
    ],
}
# The repository's own sources in each language, files no snippet imitates.
# It has no C++, and C++ parses most C.
REPO_FILES: dict[str, list[str]] = {
    cs.SupportedLanguage.C: ["codebase_rag/trace/c_agent/cgr_trace_shim.c"],
    cs.SupportedLanguage.CPP: ["codebase_rag/trace/c_agent/cgr_trace_shim.c"],
    cs.SupportedLanguage.GO: [
        "codebase_rag/parsers/go_frontend/gotypes/main.go",
        "evals/oracles/go_ast.go",
        "evals/flows_corpus/go/app.go",
    ],
}


def _toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _uv_package(dist: str) -> dict:
    found = [p for p in _toml(REPO_ROOT / "uv.lock")["package"] if p["name"] == dist]
    assert len(found) == 1, f"uv.lock holds {len(found)} {dist}"
    return found[0]


def _cargo_package(crate: str) -> dict:
    found = [
        p for p in _toml(CRATE_DIR / "Cargo.lock")["package"] if p["name"] == crate
    ]
    assert len(found) == 1, f"Cargo.lock holds {len(found)} {crate}"
    return found[0]


class Pin(NamedTuple):
    # A crates.io or PyPI release, or a git repository and commit.
    version: str | None
    repository: str | None
    commit: str | None


def _uv_pin(dist: str) -> Pin:
    package = _uv_package(dist)
    if (git := package["source"].get("git")) is None:
        return Pin(package["version"], None, None)
    url, _, commit = git.partition("#")
    return Pin(None, url.partition("?")[0], commit)


def _cargo_toml_pin(crate: str) -> Pin:
    spec = _toml(CRATE_DIR / "Cargo.toml")["dependencies"][crate]
    if isinstance(spec, str):
        assert spec.startswith("="), f"{crate} = {spec!r} is not an exact pin"
        return Pin(spec.removeprefix("="), None, None)
    return Pin(None, spec["git"], spec["rev"])


def _cargo_lock_pin(crate: str) -> Pin:
    package = _cargo_package(crate)
    source: str = package["source"]
    if not source.startswith("git+"):
        return Pin(package["version"], None, None)
    url, _, commit = source.removeprefix("git+").partition("#")
    return Pin(None, url.partition("?")[0], commit)


def _built_pin(name: str) -> str:
    grammar = crate_grammar(name)
    assert grammar is not None, "the extension compiles no grammars; rebuild the crate"
    return grammar.pin


def _package_module(name: str) -> ModuleType:
    """The grammar's Python package, or a skip on an install without it."""
    module = cs.TREE_SITTER_MODULE_PREFIX + name
    try:
        return importlib.import_module(module)
    except ImportError:
        pytest.skip(f"{module} is not installed")


def _crate(name: str) -> Language:
    language = crate_language(name)
    assert language is not None, "the extension compiles no grammars; rebuild the crate"
    return language


def _sources(name: str) -> Iterator[tuple[str, bytes]]:
    for index, source in enumerate(SOURCES[name]):
        yield f"snippet {index}", source
    for rel in REPO_FILES.get(name, []):
        yield rel, (REPO_ROOT / rel).read_bytes()


def _parse(language: Language, source: bytes) -> Node:
    return Parser(language).parse(source).root_node


def test_the_extension_compiles_the_grammars() -> None:
    # cgr requires a cgr-graph-rs with them; a checkout builds its own.
    assert hasattr(rust_extension(), cs.EG_RUST_GRAMMAR), (
        "cgr_graph_rs has no Grammar; rebuild the crate"
    )


class TestPins:
    """One release or commit per grammar, wherever it is pinned."""

    def test_the_crate_compiles_exactly_these_grammars(self) -> None:
        dependencies = _toml(CRATE_DIR / "Cargo.toml")["dependencies"]
        grammar_crates = {
            crate
            for crate in dependencies
            if crate.startswith("tree-sitter-") and crate not in RUNTIME_CRATES
        }
        assert grammar_crates == {c.crate for c in COMPILED.values()}
        names = getattr(rust_extension(), cs.EG_RUST_GRAMMAR).NAMES
        assert sorted(names) == sorted(COMPILED)

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_uv_lock_cargo_and_the_built_crate_agree(self, name: str) -> None:
        dist, crate = COMPILED[name]
        uv, toml, lock = _uv_pin(dist), _cargo_toml_pin(crate), _cargo_lock_pin(crate)
        assert uv == toml == lock, (
            f"{name}: uv.lock has {uv}, Cargo.toml {toml}, Cargo.lock {lock}; "
            f"the Python package and the crate move together, {BUMP_GUIDE}"
        )
        assert _built_pin(name) == (uv.version or uv.commit), (
            f"the installed cgr_graph_rs compiled {name} from {_built_pin(name)}, "
            f"not {uv}; rebuild the crate, or {BUMP_GUIDE}"
        )

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_a_fork_and_its_python_package_have_one_version(self, name: str) -> None:
        # A fork's release number is what treesitter-full pins, so a PyPI
        # resolution finds the release the fork branched from.
        dist, crate = COMPILED[name]
        assert _uv_package(dist)["version"] == _cargo_package(crate)["version"]

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_treesitter_full_pins_it_exactly(self, name: str) -> None:
        dist = COMPILED[name].dist
        extra = _toml(REPO_ROOT / "pyproject.toml")["project"]["optional-dependencies"]
        found = [
            Requirement(r)
            for r in extra["treesitter-full"]
            if Requirement(r).name == dist
        ]
        assert len(found) == 1, f"treesitter-full lists {dist} {len(found)} times"
        specifiers = [(s.operator, s.version) for s in found[0].specifier]
        assert specifiers == [("==", _uv_package(dist)["version"])], (
            f"treesitter-full must pin {dist} exactly, so a PyPI install cannot "
            f"float away from the grammar compiled into cgr-graph-rs; {BUMP_GUIDE}"
        )

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_the_installed_python_package_is_the_locked_one(self, name: str) -> None:
        dist = COMPILED[name].dist
        try:
            installed = importlib.metadata.distribution(dist)
        except importlib.metadata.PackageNotFoundError:
            pytest.skip(f"{dist} is not installed")
        pin = _uv_pin(dist)
        if pin.commit is None:
            assert installed.version == pin.version
            return
        origin = json.loads(installed.read_text("direct_url.json") or "{}")
        assert (origin.get("url"), origin.get("vcs_info", {}).get("commit_id")) == (
            pin.repository,
            pin.commit,
        ), f"{dist} is installed from {origin}, not the fork uv.lock pins"


class TestFingerprints:
    """The same grammar, not only the same version (an ABI can differ)."""

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_the_python_package_is_the_grammar_the_crate_compiles(
        self, name: str
    ) -> None:
        package = Language(_package_module(name).language())
        native = native_fingerprint(crate_grammar(name))
        assert grammar_fingerprint(package) == native, (
            f"{name}: the Python package's grammar differs from the crate's; "
            f"{BUMP_GUIDE}"
        )

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_py_tree_sitter_reads_the_compiled_grammar_as_the_crate_does(
        self, name: str
    ) -> None:
        native = native_fingerprint(crate_grammar(name))
        assert grammar_fingerprint(_crate(name)) == native
        assert native.node_kind_count > 0
        assert native.field_count > 0

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_the_highlights_query_is_the_python_packages(self, name: str) -> None:
        assert crate_grammar(name).highlights == _package_module(name).HIGHLIGHTS_QUERY

    def test_a_different_grammar_has_a_different_fingerprint(self) -> None:
        fingerprints = {
            name: grammar_fingerprint(_crate(name)).digest for name in COMPILED
        }
        assert len(set(fingerprints.values())) == len(COMPILED)
        import tree_sitter_python

        python = grammar_fingerprint(Language(tree_sitter_python.language()))
        assert python.digest not in fingerprints.values()

    def test_a_missing_name_is_marked_apart_from_an_empty_one(self) -> None:
        assert crate_grammars._name_bytes(None) != crate_grammars._name_bytes("")
        assert crate_grammars._name_bytes("é") == b"\x00\x00\x00\x02\xc3\xa9"


class TestBothRuntimes:
    """One source parsed by py-tree-sitter and by the crate, node for node."""

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_the_compiled_grammar_parses_alike_on_both(self, name: str) -> None:
        grammar = crate_grammar(name)
        language = _crate(name)
        compared = 0
        for label, source in _sources(name):
            python = describe(_parse(language, source))
            assert grammar.describe(source) == python, f"{name}, {label}"
            compared += len(python)
        assert compared > 1_000

    @pytest.mark.parametrize("name", sorted(COMPILED))
    def test_the_python_package_parses_as_the_compiled_grammar(self, name: str) -> None:
        package = Language(_package_module(name).language())
        for label, source in _sources(name):
            assert describe(_parse(package, source)) == crate_grammar(name).describe(
                source
            ), f"{name}, {label}"

    def test_the_samples_reach_error_recovery(self) -> None:
        # Recovery is where two copies of a grammar would part first.
        for name in COMPILED:
            roots = [_parse(_crate(name), source) for _, source in _sources(name)]
            assert any(root.has_error for root in roots), name


class TestLoader:
    """Where the parser loader takes each compiled grammar from."""

    @pytest.fixture(autouse=True)
    def fresh_store(self) -> Iterator[None]:
        parser_loader._reset_parser_cache()
        with patch.dict(parser_loader._loader_cache, clear=True):
            yield
        parser_loader._reset_parser_cache()

    @pytest.mark.parametrize("name", sorted(cs.CRATE_GRAMMAR_LANGUAGES))
    def test_c_and_cpp_come_from_the_crate(self, name: str) -> None:
        parsers, queries = parser_loader.load_parsers()
        crate = _crate(name)
        # Equal Language objects hold one grammar in memory: the crate's.
        assert queries[name][cs.QUERY_LANGUAGE] == crate
        assert parsers[name].language == crate
        try:
            package = importlib.import_module(cs.TREE_SITTER_MODULE_PREFIX + name)
        except ImportError:
            return
        assert queries[name][cs.QUERY_LANGUAGE] != Language(package.language())

    @pytest.mark.parametrize("name", sorted(cs.CRATE_GRAMMAR_LANGUAGES))
    def test_c_and_cpp_load_without_any_grammar_package(self, name: str) -> None:
        with patch.object(parser_loader, "_import_pip_grammar", return_value=None):
            assert parser_loader.grammar_installed(name)
            assert name not in parser_loader.missing_grammars()
            _, queries = parser_loader.load_parsers()
            assert queries[name][cs.QUERY_LANGUAGE] == _crate(name)

    @pytest.mark.parametrize("name", sorted(cs.CRATE_GRAMMAR_LANGUAGES))
    def test_the_highlights_query_is_the_crates_without_the_package(
        self, name: str
    ) -> None:
        real = importlib.import_module

        def no_grammar_package(module: str, *args: str | None) -> ModuleType:
            if module.startswith(cs.TREE_SITTER_MODULE_PREFIX):
                raise ImportError(module)
            return real(module, *args)

        with patch.object(parser_loader.importlib, "import_module", no_grammar_package):
            assert parser_loader._highlights_source(name) == (
                crate_grammar(name).highlights
            )
            _, queries = parser_loader.load_parsers()
            assert queries[name][cs.QUERY_HIGHLIGHTS] is not None

    def test_an_extension_without_grammars_gives_no_c_rather_than_upstreams(
        self,
    ) -> None:
        # No fallback to a Python package: a PyPI install's would be the
        # unpatched upstream grammar, the very difference this removes.
        with patch.object(crate_grammars, "rust_extension", return_value=None):
            assert parser_loader._get_language_library(cs.SupportedLanguage.C) is None
            assert not parser_loader.grammar_installed(cs.SupportedLanguage.C)

    def test_go_stays_on_its_python_package(self) -> None:
        package = Language(_package_module(cs.SupportedLanguage.GO).language())
        _, queries = parser_loader.load_parsers()
        language = queries[cs.SupportedLanguage.GO][cs.QUERY_LANGUAGE]
        assert language == package
        assert language != _crate(cs.SupportedLanguage.GO)

    @pytest.mark.parametrize("name", sorted(cs.CRATE_GRAMMAR_LANGUAGES))
    def test_a_commented_macro_keeps_the_function_after_it(self, name: str) -> None:
        parsers, _ = parser_loader.load_parsers()
        root = parsers[name].parse(COMMENTED_MACRO).root_node
        assert not root.has_error, str(root)
        assert [child.type for child in root.named_children] == [
            "preproc_function_def",
            "function_definition",
        ]
