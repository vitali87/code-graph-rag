# PHP namespace-aware qualification (issue #1185, stage 1).
#
# CGR qualifies PHP functions by FILE PATH and ignores the `namespace`
# declaration, which `parsers/call_resolver.py` records as the repo's only
# `LIMITATION:` comment. In modern PHP the namespace and the directory layout
# are independent -- PSR-4 maps a namespace PREFIX to a directory, so
# `Vendor\Pkg\helper` may live anywhere under the mapped root, and a global
# helper commonly declares `namespace Illuminate\Support` from
# `Collections/functions.php`.
#
# The consequence is not a missing edge but a WRONG one: two same-named
# functions in different namespaces are indistinguishable by file path alone,
# so a call binds to whichever the trie happens to reach first.
#
# The fixtures below deliberately give the two candidates the SAME simple name
# in DIFFERENT namespaces, because that is the only shape where a correct
# implementation and the current path-based one disagree. A fixture with one
# `format()` would pass under both.
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.tests.conftest import get_relationships, run_updater
from codebase_rag.types_defs import NodeType

SKIP = "php"


def _registry(entries: dict[str, str]) -> FunctionRegistryTrie:
    """A REAL registry trie, not a stand-in.

    A `dict` double hid two defects in this file already: it iterates (the
    protocol does not), and its `startswith` prefix matching accepts a
    trailing separator where the trie's part-wise walk returns nothing. Both
    passed the tests and failed in production.

    The trie is cheap to construct, so there is no reason to approximate it --
    and a double that is more permissive than the real object makes the suite
    green against an interface production never provides.
    """
    trie = FunctionRegistryTrie()
    for qn in entries:
        trie[qn] = NodeType.FUNCTION
    return trie


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in get_relationships(mock_ingestor, "CALLS")
    }


def test_a_namespaced_call_binds_to_its_own_namespace(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`use App\\Text\\format;` must bind to App\\Text, not to a same-named sibling.

    Two files declare `format()` in different namespaces. The caller imports
    exactly one of them. Path-based qualification cannot tell them apart, so
    the call resolves by simple name and may reach either.

    Asserts the (source, target) PAIR rather than that some CALLS edge exists:
    an edge to the wrong `format` is the actual defect, and a test asserting
    only "a call was recorded" passes against it.
    """
    project = temp_repo / "php_ns"
    project.mkdir()

    (project / "text.php").write_text(
        encoding="utf-8",
        data="""<?php
namespace App\\Text;

function format(string $s): string {
    return trim($s);
}
""",
    )

    (project / "money.php").write_text(
        encoding="utf-8",
        data="""<?php
namespace App\\Money;

function format(int $cents): string {
    return (string) $cents;
}
""",
    )

    (project / "caller.php").write_text(
        encoding="utf-8",
        data="""<?php
namespace App\\Report;

use function App\\Text\\format;

function render(string $raw): string {
    return format($raw);
}
""",
    )

    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _calls(mock_ingestor)
    callers = {(src, dst) for src, dst in pairs if src.endswith("render")}

    assert callers, f"no CALLS edge from render at all; pairs={sorted(pairs)}"

    # Asserted on the MODULE the target lives in, not on namespace text in the
    # qn. Qualified names stay path-based by design -- the namespace is held in
    # a side map (`php_module_namespaces`) and used to RESOLVE the import,
    # rather than folded into every PHP qn, which would rewrite the identity of
    # every existing PHP node in the graph.
    #
    # `text.php` declares `namespace App\Text` and `money.php` declares
    # `namespace App\Money`, so which module the edge lands in is exactly the
    # question, and the two are distinguishable.
    assert any(dst.endswith("text.format") for _, dst in callers), (
        "render() did not resolve to the `format` in the module declaring "
        "namespace App\\Text; the `use function App\\Text\\format` import was "
        f"not honoured. edges={sorted(callers)}"
    )
    assert not any(dst.endswith("money.format") for _, dst in callers), (
        "render() resolved to the `format` in the App\\Money module, which it "
        "never imported -- a WRONG edge rather than a missing one. "
        f"edges={sorted(callers)}"
    )


def test_an_ambiguous_namespace_target_resolves_to_nothing(tmp_path: Path) -> None:
    """Two modules in ONE namespace both defining the symbol must not be guessed.

    PHP allows a namespace to span any number of files, so a namespace may be
    declared by several. If more than one of them defines the symbol, the
    import alone cannot say which is meant -- and picking the first would
    reintroduce exactly the arbitrary binding this change removes, only with
    more confidence behind it.

    Found by mutation: relaxing the uniqueness check from `len(found) != 1` to
    "take the first match" left every other test in this file passing, because
    no other fixture has two candidates inside ONE namespace.

    Exercises the resolver helper directly with a registry holding both
    candidates. Going through the updater would assert on whichever edge the
    trie fallback then produced, which is not the question -- the question is
    whether the IMPORT resolution claims a unique answer it does not have.
    """
    from types import SimpleNamespace

    from codebase_rag.parsers.call_resolver import CallResolver

    resolver = object.__new__(CallResolver)
    resolver.import_processor = SimpleNamespace(
        php_module_namespaces={"proj.one": "App.Text", "proj.two": "App.Text"}
    )
    resolver.function_registry = _registry(
        {
            "proj.one.format": "Function",
            "proj.two.format": "Function",
        }
    )

    assert resolver._php_target_for_namespace_import("App.Text.format") is None, (
        "an import naming a namespace declared by TWO modules that both define "
        "the symbol was resolved to one of them; with two equally valid "
        "candidates the import cannot say which is meant"
    )

    # The paired positive: with ONE candidate it resolves, so the None above is
    # the ambiguity guard rather than the helper being inert.
    resolver.function_registry = _registry({"proj.one.format": "Function"})
    assert (
        resolver._php_target_for_namespace_import("App.Text.format")
        == "proj.one.format"
    )


def test_a_reindex_replaces_the_recorded_namespace(tmp_path: Path) -> None:
    """Re-parsing a module must not leave its OLD namespace bound.

    `parse_imports` is called again whenever a file changes. Without the
    per-module reset, editing `namespace App\\Text` to `namespace App\\Money`
    would leave both bound -- and since resolution matches on the declared
    namespace, imports naming the OLD one would keep resolving to this module
    forever.

    Found by mutation: dropping the `php_module_namespaces.pop(module_qn)` line
    left every other test passing, because none of them parses the same module
    twice. Incremental re-index is the normal path in a running cgr, so the
    untested case is the common one.
    """
    from tree_sitter import QueryCursor

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.import_processor import ImportProcessor

    parsers, queries = load_parsers()
    language = cs.SupportedLanguage.PHP
    if language not in parsers:
        pytest.skip("php grammar not installed")

    processor = ImportProcessor(repo_path=tmp_path, project_name="proj")

    def parse(source: bytes) -> None:
        tree = parsers[language].parse(source)
        cursor = QueryCursor(queries[language]["imports"])
        processor.parse_imports(
            root_node=tree.root_node,
            module_qn="proj.svc",
            language=language,
            queries=queries,
            pre_captures=cursor.captures(tree.root_node),
        )

    parse(b"<?php\nnamespace App\\Text;\nfunction f(): int { return 1; }\n")
    assert processor.php_module_namespaces["proj.svc"] == "App.Text"

    parse(b"<?php\nnamespace App\\Money;\nfunction f(): int { return 1; }\n")

    assert processor.php_module_namespaces["proj.svc"] == "App.Money", (
        "after re-parsing with a changed `namespace` declaration the module is "
        "still bound to "
        f"{processor.php_module_namespaces['proj.svc']!r}; imports naming the "
        "old namespace would keep resolving here"
    )

    # And a module whose namespace declaration is REMOVED must unbind, not
    # keep the last one it ever had.
    parse(b"<?php\nfunction f(): int { return 1; }\n")
    assert "proj.svc" not in processor.php_module_namespaces, (
        "removing the `namespace` declaration left "
        f"{processor.php_module_namespaces.get('proj.svc')!r} bound"
    )


def test_a_class_use_does_not_resolve_to_a_php_function(tmp_path: Path) -> None:
    """`use App\\Text\\format` is a CLASS import and must not bind a function.

    PHP keeps classes and functions in separate symbol tables: `use X` imports
    a class, `use function X` imports a function. Without the distinction, a
    class or constant import whose name happens to match a registered PHP
    function resolved to that function.

    Reproduced in review on #1484, and it survived the caller-language guard
    because the caller IS PHP -- the language gate and the binding-kind gate
    are independent axes, and covering one said nothing about the other.

    `php_function_imports` already records which local names arrived via
    `use function`, so the distinction needs no new parsing.
    """
    from codebase_rag.parsers.call_resolver import CallResolver

    resolver = object.__new__(CallResolver)
    resolver.import_processor = SimpleNamespace(
        php_module_namespaces={"proj.text": "App.Text"},
        commonjs_direct_exports={},
        # The caller imported the name, but NOT via `use function`.
        php_function_imports={"proj.caller": set()},
    )
    resolver.function_registry = _registry({"proj.text.format": "Function"})
    import_map = {"format": "App.Text.format"}

    assert (
        resolver._try_resolve_direct_import(
            "format", import_map, cs.SupportedLanguage.PHP, "proj.caller"
        )
        is None
    ), (
        "a class-style `use App\\Text\\format` resolved to the PHP FUNCTION "
        "`format`; PHP holds classes and functions in separate symbol tables, "
        "so this is an edge the source never expressed"
    )

    # The paired positive: recording it as a `use function` binding DOES
    # resolve, so the None above is the binding-kind gate rather than the
    # fallback being switched off.
    resolver.import_processor.php_function_imports = {"proj.caller": {"format"}}
    assert resolver._try_resolve_direct_import(
        "format", import_map, cs.SupportedLanguage.PHP, "proj.caller"
    ) == (NodeType.FUNCTION, "proj.text.format")


def test_a_call_through_a_use_function_alias_is_case_insensitive(
    tmp_path: Path,
) -> None:
    """`use function App\\Text\\format` then calling `FORMAT()` must resolve.

    PHP function names are case-insensitive at the CALL SITE, not only in the
    import path. Verified by executing it under PHP 8.5:

        namespace App\\Text; function format(...)
        use function App\\Text\\format;
        echo FORMAT("x");        // prints F:x

    An exact-case membership test recognised the IMPORT but not the CALL, so
    the resolver declined and the call fell through to the simple-name trie --
    the same wrong-edge path, reached through the alias instead of the target.

    Distinct from the mixed-case import test: that one varies the spelling of
    the imported PATH, this one varies the spelling of the CALL. Two axes of
    the same case-insensitivity, and covering one said nothing about the other.
    """
    from codebase_rag.parsers.call_resolver import CallResolver

    resolver = object.__new__(CallResolver)
    resolver.import_processor = SimpleNamespace(
        php_module_namespaces={"proj.text": "App.Text"},
        commonjs_direct_exports={},
        php_function_imports={"proj.caller": {"format"}},
    )
    resolver.function_registry = _registry({"proj.text.format": "Function"})
    # PRODUCTION SHAPE: the import map is keyed by the DECLARATION spelling
    # only. Verified against the import processor -- `use function
    # App\\Text\\format` records exactly {'format': ...}, whatever casing the
    # call later uses.
    #
    # An earlier version of this test seeded BOTH spellings, which production
    # never produces. That made the assertion pass against a fixture the
    # system cannot generate, hiding the fact that the exact-case map lookup
    # in front of the folded gate is where the miss actually happens
    # (unreachable-fixture defect, reported on #1484).
    import_map = {"format": "App.Text.format"}

    for spelling in ("format", "FORMAT", "Format"):
        assert resolver._try_resolve_direct_import(
            spelling, import_map, cs.SupportedLanguage.PHP, "proj.caller"
        ) == (NodeType.FUNCTION, "proj.text.format"), (
            f"calling the alias as {spelling!r} did not resolve; PHP function "
            "names are case-insensitive at the call site, so this call is "
            "valid and runs"
        )

    # The control: a name that was never imported must still decline, so the
    # fold is case-insensitivity rather than accepting anything.
    assert (
        resolver._try_resolve_direct_import(
            "unrelated",
            {"unrelated": "App.Text.format"},
            cs.SupportedLanguage.PHP,
            "proj.caller",
        )
        is None
    )


def test_a_fully_qualified_import_resolves(tmp_path: Path) -> None:
    """`use function \\App\\Text\\format` (leading backslash) must resolve.

    A fully-qualified import is idiomatic PHP and runs identically to the
    unqualified spelling -- verified by executing it under PHP 8.5, which
    prints `B:x`. In PHP a `use` path is ALWAYS resolved from the global
    namespace, so the backslash is emphasis rather than a different meaning.

    The import processor dots the path, so the backslash becomes a LEADING
    separator: the recorded target is `.App.Text.format`, which never matches
    a declared `App.Text`.

    Found by probing this function's contract axes directly rather than
    waiting for review to surface a fourth one -- the previous three
    (import-path case, binding kind, call-site case) each arrived that way.
    """
    from codebase_rag.parsers.call_resolver import CallResolver

    resolver = object.__new__(CallResolver)
    resolver.import_processor = SimpleNamespace(
        php_module_namespaces={"proj.text": "App.Text"},
        commonjs_direct_exports={},
        php_function_imports={"proj.caller": {"format"}},
    )
    resolver.function_registry = _registry({"proj.text.format": "Function"})

    assert resolver._try_resolve_direct_import(
        "format",
        {"format": ".App.Text.format"},
        cs.SupportedLanguage.PHP,
        "proj.caller",
    ) == (NodeType.FUNCTION, "proj.text.format"), (
        "a fully-qualified `use function \\App\\Text\\format` did not resolve; "
        "the leading separator from the backslash was compared against a "
        "namespace declared without one"
    )

    # The control: the unqualified spelling still resolves, so the strip is a
    # normalisation rather than something that only works with a backslash.
    assert resolver._try_resolve_direct_import(
        "format",
        {"format": "App.Text.format"},
        cs.SupportedLanguage.PHP,
        "proj.caller",
    ) == (NodeType.FUNCTION, "proj.text.format")


def test_an_unknown_caller_module_does_not_resolve(tmp_path: Path) -> None:
    """No `module_qn` means the binding kind cannot be shown, so decline.

    Defaulting to True when the module is unknown would reinstate the class-use
    defect wherever the caller module is not threaded through. Declining costs
    only the trie fallback, which is where such calls went before this feature.
    """
    from codebase_rag.parsers.call_resolver import CallResolver

    resolver = object.__new__(CallResolver)
    resolver.import_processor = SimpleNamespace(
        php_module_namespaces={"proj.text": "App.Text"},
        commonjs_direct_exports={},
        php_function_imports={"proj.caller": {"format"}},
    )
    resolver.function_registry = _registry({"proj.text.format": "Function"})

    assert (
        resolver._try_resolve_direct_import(
            "format", {"format": "App.Text.format"}, cs.SupportedLanguage.PHP, None
        )
        is None
    )


def test_a_mixed_case_import_resolves_like_php_does(tmp_path: Path) -> None:
    """`use function app\\text\\FORMAT` binds to `App\\Text\\format`.

    PHP namespaces and function names are case-insensitive. Verified by
    executing it under PHP 8.5 rather than taken from documentation:

        namespace App\\Text; function format(...)
        use function app\\text\\FORMAT;  ->  prints "formatted:x"

    Exact-case comparison sent such imports to the simple-name trie, which is
    the same wrong-edge path this feature exists to avoid -- a valid import
    silently binding to an unrelated same-named function.

    Covers BOTH halves independently: the namespace differing in case, and the
    symbol differing in case. An implementation folding only one would satisfy
    a test that varied both together.
    """
    from codebase_rag.parsers.call_resolver import CallResolver

    resolver = object.__new__(CallResolver)
    resolver.import_processor = SimpleNamespace(
        php_module_namespaces={"proj.text": "App.Text"},
        commonjs_direct_exports={},
    )
    resolver.function_registry = _registry({"proj.text.format": "Function"})

    for target in (
        "App.Text.format",  # exact
        "app.text.format",  # namespace folded
        "APP.TEXT.format",  # namespace folded, upper
        "App.Text.FORMAT",  # symbol folded
        "app.text.FORMAT",  # both, the reported case
    ):
        assert (
            resolver._php_target_for_namespace_import(target) == "proj.text.format"
        ), (
            f"{target!r} did not resolve; PHP treats namespace and function "
            "names as case-insensitive, so this import is valid and binds"
        )


def test_the_case_fold_is_ascii_only(tmp_path: Path) -> None:
    """Folding must not reach beyond A-Z.

    PHP folds ASCII only, so identifiers differing outside that range are
    DISTINCT. `str.casefold()` or `str.lower()` would match names the language
    does not -- trading a missed binding for a WRONG one, which is the
    direction that matters here.

    The Kelvin sign folds to plain `k` under Unicode rules and must not match.
    """
    from codebase_rag.parsers.call_resolver import _php_fold

    assert _php_fold("App.Text") == "app.text"
    assert _php_fold("FORMAT") == "format"

    kelvin = "\u212a"  # KELVIN SIGN; casefolds to "k" under Unicode
    assert _php_fold(kelvin) != "k", (
        "the fold reached beyond ASCII; PHP would treat these identifiers as "
        "distinct, so matching them creates an edge the language does not"
    )
    assert kelvin.casefold() == "k", "control: Unicode folding DOES match here"


def test_a_non_php_caller_never_resolves_through_php_namespaces(
    tmp_path: Path,
) -> None:
    """The namespace fallback is gated on the CALLER being PHP.

    The map holds only PHP modules, but the import targets matched against it
    are dotted strings any language can produce. A JS `import { format }`
    recorded as `App.Text.format` would resolve straight into a PHP function
    -- an edge ACROSS languages that no source expressed, and a new class of
    wrong edge introduced by the fix itself.

    Reproduced in review on #1484: a JavaScript caller bound to a PHP target.

    Asserts a non-PHP language resolves to nothing AND that PHP still does, so
    the guard is a language gate rather than the path being switched off.
    """
    from codebase_rag.parsers.call_resolver import CallResolver

    resolver = object.__new__(CallResolver)
    resolver.import_processor = SimpleNamespace(
        php_module_namespaces={"proj.text": "App.Text"},
        # The non-PHP path falls through to the commonjs lookup, so the double
        # must carry it or the test fails on the fixture rather than the guard.
        commonjs_direct_exports={},
        # `use function` bindings, which the PHP path now requires.
        php_function_imports={"proj.caller": {"format"}},
    )
    resolver.function_registry = _registry({"proj.text.format": "Function"})
    import_map = {"format": "App.Text.format"}

    for language in (
        cs.SupportedLanguage.JS,
        cs.SupportedLanguage.TS,
        cs.SupportedLanguage.PYTHON,
        None,
    ):
        assert (
            resolver._try_resolve_direct_import(
                "format", import_map, language, "proj.caller"
            )
            is None
        ), (
            f"a {language} caller resolved through the PHP namespace map to a "
            "PHP function; the fallback must be gated on the caller's language"
        )

    assert resolver._try_resolve_direct_import(
        "format", import_map, cs.SupportedLanguage.PHP, "proj.caller"
    ) == (NodeType.FUNCTION, "proj.text.format"), (
        "the PHP caller stopped resolving too, so the guard disabled the "
        "feature rather than scoping it"
    )


def test_a_file_with_several_namespace_blocks_records_nothing(
    tmp_path: Path,
) -> None:
    """Two top-level blocks have no single answer, so none is recorded.

    PHP allows `namespace A { } namespace B { }` in one file. Both blocks'
    functions land in the SAME module qn, so recording the first makes an
    import of `A\\helper` resolve to the `helper` defined in B.

    Reproduced in review on #1484 -- `App.First` recorded, resolution returned
    the `App.Second` function.

    Recording nothing sends such files to the trie fallback, exactly where
    they were before this feature existed: no worse than the status quo, and
    unlike a first-block guess it never asserts an answer it does not have.
    """
    from tree_sitter import QueryCursor

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.import_processor import ImportProcessor

    parsers, queries = load_parsers()
    language = cs.SupportedLanguage.PHP
    if language not in parsers:
        pytest.skip("php grammar not installed")

    source = b"""<?php
namespace App\\First {
    function helper(): int { return 1; }
}
namespace App\\Second {
    function helper(): int { return 2; }
}
"""
    tree = parsers[language].parse(source)
    processor = ImportProcessor(repo_path=tmp_path, project_name="proj")
    cursor = QueryCursor(queries[language]["imports"])

    processor.parse_imports(
        root_node=tree.root_node,
        module_qn="proj.multi",
        language=language,
        queries=queries,
        pre_captures=cursor.captures(tree.root_node),
    )

    recorded = processor.php_module_namespaces.get("proj.multi")
    assert recorded is None, (
        f"a file with two top-level namespace blocks recorded {recorded!r}; "
        "both blocks' functions share one module qn, so an import naming that "
        "namespace can bind to a function defined in the OTHER block"
    )


def test_the_global_namespace_block_is_not_recorded(tmp_path: Path) -> None:
    """`namespace { ... }` names nothing and must bind nothing.

    PHP's braced form with no name declares the GLOBAL namespace. Recording it
    as the empty string would make `""` the module's namespace, and the
    namespace part of any unqualified import target is also `""` -- so every
    such lookup would match this module.

    Found by mutation: removing the empty-name guard left all other tests
    passing, since no other fixture uses the braced global form.
    """
    from tree_sitter import QueryCursor

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.import_processor import ImportProcessor

    parsers, queries = load_parsers()
    language = cs.SupportedLanguage.PHP
    if language not in parsers:
        pytest.skip("php grammar not installed")

    source = b"""<?php
namespace {
    function helper(): int { return 1; }
}
"""
    tree = parsers[language].parse(source)
    processor = ImportProcessor(repo_path=tmp_path, project_name="proj")
    cursor = QueryCursor(queries[language]["imports"])

    processor.parse_imports(
        root_node=tree.root_node,
        module_qn="proj.global",
        language=language,
        queries=queries,
        pre_captures=cursor.captures(tree.root_node),
    )

    recorded = processor.php_module_namespaces.get("proj.global")
    assert recorded is None, (
        "the unnamed `namespace { }` global block was recorded as "
        f"{recorded!r}; an empty namespace matches every unqualified import "
        "target, so this module would capture all of them"
    )


def test_the_namespace_declaration_is_captured_for_the_module(tmp_path: Path) -> None:
    """The `namespace` a module declares must be recorded, dotted.

    The narrower precondition of the test above, pinned separately so a
    failure says WHICH half broke: without the declaration nothing can bind an
    import target to a file, and the resolution test could only pass by luck
    of the trie.

    Exercises the import processor directly rather than the whole updater,
    because this is a statement about one map and the end-to-end path is
    already covered above.
    """
    from tree_sitter import QueryCursor

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.import_processor import ImportProcessor

    parsers, queries = load_parsers()
    language = cs.SupportedLanguage.PHP
    if language not in parsers:
        pytest.skip("php grammar not installed")

    source = b"""<?php
namespace Vendor\\Pkg\\Sub;

function helper(): int { return 1; }
"""
    tree = parsers[language].parse(source)
    processor = ImportProcessor(repo_path=tmp_path, project_name="proj")
    cursor = QueryCursor(queries[language]["imports"])

    processor.parse_imports(
        root_node=tree.root_node,
        module_qn="proj.svc",
        language=language,
        queries=queries,
        pre_captures=cursor.captures(tree.root_node),
    )

    assert processor.php_module_namespaces.get("proj.svc") == "Vendor.Pkg.Sub", (
        "the `namespace Vendor\\Pkg\\Sub` declaration was not recorded, so an "
        "import target naming that namespace cannot be matched to this module "
        f"(issue #1185). map={processor.php_module_namespaces}"
    )


def test_a_file_with_no_namespace_records_nothing(tmp_path: Path) -> None:
    """A control: absence must stay absent rather than binding to "".

    An empty-string namespace would compare equal to the namespace part of any
    unqualified import target, making every such lookup match this module --
    turning the fix into a new source of confident wrong edges.
    """
    from tree_sitter import QueryCursor

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.import_processor import ImportProcessor

    parsers, queries = load_parsers()
    language = cs.SupportedLanguage.PHP
    if language not in parsers:
        pytest.skip("php grammar not installed")

    source = b"""<?php
function helper(): int { return 1; }
"""
    tree = parsers[language].parse(source)
    processor = ImportProcessor(repo_path=tmp_path, project_name="proj")
    cursor = QueryCursor(queries[language]["imports"])

    processor.parse_imports(
        root_node=tree.root_node,
        module_qn="proj.plain",
        language=language,
        queries=queries,
        pre_captures=cursor.captures(tree.root_node),
    )

    assert "proj.plain" not in processor.php_module_namespaces, (
        "a file with no `namespace` declaration was bound to a namespace "
        f"anyway: {processor.php_module_namespaces}"
    )
