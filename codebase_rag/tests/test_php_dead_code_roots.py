# `cgr dead-code` on PHP reported 1,620 of flysystem's 1,647 functions and
# methods (issue #2472), for two reasons the other languages don't share:
# - PHP never set `is_exported`, so a `public` method or a top-level function
#   was not an API root the way `public` in C#/Java and `export` in TS are;
# - PHPUnit tests were recognised only under a `tests/` path, so a test class
#   kept beside the code (`src/**/XxxTest.php`, flysystem's
#   `src/AdapterTestUtilities/*TestCase.php`, Symfony's `Tests/`) was
#   reported as unreachable production code.
# Each test indexes a small project end to end and reads the dead set from
# the same node properties the `CYPHER_DEAD_CODE_NODES` fetch returns.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import (
    _is_php_test_path,
    _node_props,
    dead_code_from_graph,
    default_dead_code_config,
)
from evals.cgr_graph import _capture, _CapturingIngestor

_PROJECT = "phpdc"

_LIB = r"""<?php
namespace App;

class Lib
{
    public function __construct() {}
    public function publicApi(): int { return $this->internal(); }
    private function internal(): int { return 1; }
    function implicitlyPublic(): int { return 2; }
    protected function hook(): int { return $this->forHook(); }
    private function forHook(): int { return 3; }
    private function unused(): int { return 4; }
}

final class Sealed
{
    public function run(): int { return 1; }
    private function neverCalled(): int { return 2; }
    PRIVATE function shoutedPrivate(): int { return 3; }
}

interface Port { public function send(): void; }

trait Greets { protected function greet(): string { return 'hi'; } }

function helper(): int { return 1; }

if (!function_exists('App\polyfill')) {
    function polyfill(): int { return 2; }
}
"""

# The issue's repro plus an unused private fixture: the same class under
# `tests/` reports nothing, so it must report nothing beside the code either.
_LIB_TEST = r"""<?php
namespace App;

use PHPUnit\Framework\TestCase;
use PHPUnit\Framework\Attributes\Test;

class LibTest extends TestCase
{
    public function testPrefixed(): void { $this->helper(); }

    /** @test */
    public function annotated(): void { $this->helper(); }

    #[Test]
    public function attributed(): void { $this->helper(); }

    private function helper(): void {}

    private function leftoverFixture(): void {}
}
"""


def _index(tmp_path: Path, files: dict[str, str]) -> _CapturingIngestor:
    project = tmp_path / _PROJECT
    for rel, source in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return _capture(project, _PROJECT)


def _dead(
    ingestor: _CapturingIngestor, include_tests: bool, include_classes: bool = False
) -> set[str]:
    # Only what the dead-code fetch returns reaches the engine in
    # production, so a rule keyed on any other property would pass here and
    # do nothing against Memgraph.
    nodes = {key: _node_props(props) for key, props in ingestor.nodes.items()}
    return dead_code_from_graph(
        nodes,
        list(ingestor.rels),
        f"{_PROJECT}.",
        default_dead_code_config(include_tests, include_classes),
    )


def _defined(ingestor: _CapturingIngestor, prefix: str) -> set[str]:
    # Every Function/Method under `prefix`, so "not dead" never passes for a
    # symbol that was not indexed at all.
    labels = {cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value}
    found = {
        str(uid)
        for label, uid in ingestor.nodes
        if label in labels and str(uid).startswith(prefix)
    }
    assert found, f"nothing indexed under {prefix!r}"
    return found


def _exported(ingestor: _CapturingIngestor, qn: str) -> bool:
    matches = [
        props
        for (label, uid), props in ingestor.nodes.items()
        if str(uid) == qn
        and label in (cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value)
    ]
    assert matches, f"{qn} not indexed"
    return matches[0].get(cs.KEY_IS_EXPORTED) is True


# --- 1. Public PHP members and top-level functions are API roots ----------

_LIB_QN = f"{_PROJECT}.src.Lib"


@pytest.mark.parametrize("include_tests", [True, False])
def test_public_method_is_an_api_root_and_keeps_its_private_callee(
    tmp_path: Path, include_tests: bool
) -> None:
    ingestor = _index(tmp_path, {"src/Lib.php": _LIB})
    dead = _dead(ingestor, include_tests)

    assert f"{_LIB_QN}.Lib.publicApi" not in dead, sorted(dead)
    assert f"{_LIB_QN}.Lib.internal" not in dead, sorted(dead)


def test_implicit_public_protected_and_constructor_are_api_roots(
    tmp_path: Path,
) -> None:
    ingestor = _index(tmp_path, {"src/Lib.php": _LIB})
    dead = _dead(ingestor, include_tests=True)

    # PHP members without a visibility keyword are public; `protected` is
    # the inheritance surface a subclass outside the repo calls, as it is
    # for Java and TS.
    for member in ("__construct", "implicitlyPublic", "hook", "forHook"):
        assert f"{_LIB_QN}.Lib.{member}" not in dead, (member, sorted(dead))
    assert f"{_LIB_QN}.Sealed.run" not in dead, sorted(dead)
    assert f"{_LIB_QN}.Port.send" not in dead, sorted(dead)
    assert f"{_LIB_QN}.Greets.greet" not in dead, sorted(dead)


def test_top_level_and_conditionally_declared_functions_are_api_roots(
    tmp_path: Path,
) -> None:
    ingestor = _index(tmp_path, {"src/Lib.php": _LIB})
    dead = _dead(ingestor, include_tests=True)

    # A PHP function is global wherever its file is loaded, including the
    # `if (!function_exists(...))` polyfill shape.
    assert f"{_LIB_QN}.helper" not in dead, sorted(dead)
    assert f"{_LIB_QN}.polyfill" not in dead, sorted(dead)


def test_private_members_without_callers_are_still_reported(tmp_path: Path) -> None:
    ingestor = _index(tmp_path, {"src/Lib.php": _LIB})
    dead = _dead(ingestor, include_tests=True)

    assert f"{_LIB_QN}.Lib.unused" in dead, sorted(dead)
    # `final` hides nothing: a private method is not API on any class.
    assert f"{_LIB_QN}.Sealed.neverCalled" in dead, sorted(dead)
    # PHP keywords are case-insensitive.
    assert f"{_LIB_QN}.Sealed.shoutedPrivate" in dead, sorted(dead)
    assert not _exported(ingestor, f"{_LIB_QN}.Lib.internal")


def test_closures_nested_functions_and_anonymous_class_members_are_not_api(
    tmp_path: Path,
) -> None:
    source = r"""<?php
namespace App;

function outer(): void {
    function inner(): void {}
    $f = function () { return 1; };
    $g = fn() => 2;
}

$obj = new class {
    public function anonPub(): void {}
};
"""
    ingestor = _index(tmp_path, {"src/scopes.php": source})
    qn = f"{_PROJECT}.src.scopes"
    callables = _defined(ingestor, f"{qn}.")

    assert _exported(ingestor, f"{qn}.outer")
    # Reached only by running its enclosing scope, or by a value that
    # escapes it: the graph's own edges decide, not a root.
    assert not _exported(ingestor, f"{qn}.inner")
    closures = {c for c in callables if c.startswith(f"{qn}.outer.")}
    assert len(closures) == 2, sorted(callables)
    for closure in closures:
        assert not _exported(ingestor, closure), closure
    anon_members = {c for c in callables if c.endswith(".anonPub")}
    assert len(anon_members) == 1, sorted(callables)
    (anon_pub,) = anon_members
    assert not _exported(ingestor, anon_pub)

    dead = _dead(ingestor, include_tests=True)
    assert f"{qn}.inner" in dead, sorted(dead)
    assert anon_pub in dead, sorted(dead)


def test_named_classes_are_api_roots_and_anonymous_ones_are_not(
    tmp_path: Path,
) -> None:
    source = r"""<?php
namespace App;

class Shown {}
final class Sealed {}
trait Mixin {}
$o = new class {};
"""
    ingestor = _index(tmp_path, {"src/types.php": source})
    qn = f"{_PROJECT}.src.types"
    classes = {
        str(uid): props
        for (label, uid), props in ingestor.nodes.items()
        if label == cs.NodeLabel.CLASS.value and str(uid).startswith(f"{qn}.")
    }
    anonymous = [c for c in classes if c.rsplit(".", 1)[-1].startswith("anonymous_")]
    assert len(anonymous) == 1, sorted(classes)
    assert classes[anonymous[0]].get(cs.KEY_IS_EXPORTED) is not True

    dead = _dead(ingestor, include_tests=True, include_classes=True)
    for name in ("Shown", "Sealed", "Mixin"):
        assert f"{qn}.{name}" in classes, sorted(classes)
        assert f"{qn}.{name}" not in dead, (name, sorted(dead))


_NESTED = r"""<?php
namespace App;

class Host
{
    private function build(): object
    {
        class Inner { public function m(): int { return 1; } }
        return new Inner();
    }
}
"""


def test_class_declared_inside_a_callable_is_not_an_api_root(tmp_path: Path) -> None:
    # PHP declares a class written inside a function body only when that
    # body runs, so nothing outside can name it before then: an uncalled
    # builder leaves the class and its methods dead.
    ingestor = _index(tmp_path, {"src/nested.php": _NESTED})
    qn = f"{_PROJECT}.src.nested"
    inner_qn = f"{qn}.Host.Inner"
    inner = next(
        props
        for (label, uid), props in ingestor.nodes.items()
        if label == cs.NodeLabel.CLASS.value and str(uid) == inner_qn
    )

    assert inner.get(cs.KEY_IS_EXPORTED) is not True
    assert not _exported(ingestor, f"{inner_qn}.m")
    dead = _dead(ingestor, include_tests=True, include_classes=True)
    assert {f"{qn}.Host.build", inner_qn, f"{inner_qn}.m"} <= dead, sorted(dead)


def test_class_built_by_a_live_callable_is_revived_through_it(tmp_path: Path) -> None:
    # Not a root of its own, but a class its live builder declares escapes
    # it (the factory-class rule), so its methods stay live.
    source = _NESTED.replace(
        "    private function build",
        "    public function run(): object { return $this->build(); }\n\n"
        "    private function build",
    )
    ingestor = _index(tmp_path, {"src/nested.php": source})
    qn = f"{_PROJECT}.src.nested"
    inner_qn = f"{qn}.Host.Inner"
    dead = _dead(ingestor, include_tests=True, include_classes=True)

    assert not {f"{qn}.Host.build", inner_qn, f"{inner_qn}.m"} & dead, sorted(dead)


def test_conditionally_declared_top_level_class_is_still_an_api_root(
    tmp_path: Path,
) -> None:
    source = r"""<?php
namespace App;

if (!class_exists('App\Shim')) {
    class Shim { public function s(): int { return 1; } }
}
"""
    ingestor = _index(tmp_path, {"src/shim.php": source})
    qn = f"{_PROJECT}.src.shim"
    dead = _dead(ingestor, include_tests=True, include_classes=True)

    assert _exported(ingestor, f"{qn}.Shim.s")
    assert not {f"{qn}.Shim", f"{qn}.Shim.s"} & dead, sorted(dead)


# --- 2. PHPUnit tests are recognised wherever they live --------------------

_TEST_CLASS_QN = f"{_PROJECT}.src.LibTest.LibTest"


@pytest.mark.parametrize("include_tests", [True, False])
def test_co_located_phpunit_test_reports_like_the_same_test_under_tests(
    tmp_path: Path, include_tests: bool
) -> None:
    moved = _LIB_TEST.replace("class LibTest", "class LibMovedTest")
    ingestor = _index(
        tmp_path,
        {
            "src/Lib.php": _LIB,
            "src/LibTest.php": _LIB_TEST,
            "tests/LibMovedTest.php": moved,
        },
    )
    dead = _dead(ingestor, include_tests)
    beside = _defined(ingestor, f"{_TEST_CLASS_QN}.")
    under_tests = _defined(ingestor, f"{_PROJECT}.tests.LibMovedTest.LibMovedTest.")

    assert not under_tests & dead, sorted(dead)
    assert not beside & dead, sorted(beside & dead)


@pytest.mark.parametrize("include_tests", [True, False])
def test_test_case_subclass_outside_any_test_path_is_test_code(
    tmp_path: Path, include_tests: bool
) -> None:
    # Neither a `tests/` path nor a `*Test.php` name: only the class says
    # it is a test. Its data provider, its `@dataProvider` target and its
    # helpers are test code with it.
    source = r"""<?php
namespace App\Checks;

use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

final class LibChecks extends TestCase
{
    protected function setUp(): void {}

    public static function cases(): array { return [[1]]; }

    #[DataProvider('cases')]
    public function testCases(int $n): void { $this->check($n); }

    /** @dataProvider moreCases */
    public function testMore(int $n): void { $this->check($n); }

    public static function moreCases(): array { return [[2]]; }

    private function check(int $n): void {}

    private function leftoverFixture(): void {}

    public function testDouble(): void
    {
        $double = new class {
            public function stubbed(): int { return 1; }
        };
    }
}
"""
    ingestor = _index(tmp_path, {"src/Checks/LibChecks.php": source})
    qn = f"{_PROJECT}.src.Checks.LibChecks.LibChecks"
    members = _defined(ingestor, f"{qn}.")
    dead = _dead(ingestor, include_tests)

    assert any(m.endswith(".stubbed") for m in members), sorted(members)
    assert not members & dead, sorted(members & dead)


@pytest.mark.parametrize("include_tests", [True, False])
def test_project_base_test_case_is_followed_to_its_subclasses(
    tmp_path: Path, include_tests: bool
) -> None:
    # flysystem's layout: an abstract `*TestCase` in a non-test directory,
    # extended by adapter tests in files that are not `*Test.php` either.
    base = r"""<?php
namespace App\AdapterTestUtilities;

use PHPUnit\Framework\TestCase;

abstract class FilesystemAdapterTestCase extends TestCase
{
    abstract protected static function createAdapter(): object;

    /** @test */
    public function writing_and_reading(): void { $this->givenAFile(); }

    private function givenAFile(): void {}

    private function unusedBaseHelper(): void {}
}
"""
    adapter = r"""<?php
namespace App\Local;

use App\AdapterTestUtilities\FilesystemAdapterTestCase;

class LocalAdapterChecks extends FilesystemAdapterTestCase
{
    protected static function createAdapter(): object { return new \stdClass(); }

    /** @test */
    public function local_only(): void {}

    private function unusedLocalHelper(): void {}
}
"""
    ingestor = _index(
        tmp_path,
        {
            "src/AdapterTestUtilities/FilesystemAdapterTestCase.php": base,
            "src/Local/LocalAdapterChecks.php": adapter,
        },
    )
    base_members = _defined(
        ingestor,
        f"{_PROJECT}.src.AdapterTestUtilities.FilesystemAdapterTestCase."
        "FilesystemAdapterTestCase.",
    )
    adapter_members = _defined(
        ingestor, f"{_PROJECT}.src.Local.LocalAdapterChecks.LocalAdapterChecks."
    )
    dead = _dead(ingestor, include_tests)

    assert not base_members & dead, sorted(base_members & dead)
    assert not adapter_members & dead, sorted(adapter_members & dead)


@pytest.mark.parametrize("imported", [True, False])
@pytest.mark.parametrize(
    "base",
    [
        "Symfony\\Bundle\\FrameworkBundle\\Test\\KernelTestCase",
        "Symfony\\Bundle\\FrameworkBundle\\Test\\WebTestCase",
        "PHPUnit\\Framework\\TestCase",
        "TestCase",
    ],
)
def test_framework_test_case_base_is_recognised(
    tmp_path: Path, base: str, imported: bool
) -> None:
    # Framework bases live outside the repo, so the graph holds them by
    # name: namespaced through a `use` import, bare when spelled fully
    # qualified or not imported at all.
    leaf = base.rsplit("\\", 1)[-1]
    head = (
        f"use {base};\n\nclass BootChecks extends {leaf}"
        if imported
        else f"class BootChecks extends \\{base}"
    )
    source = rf"""<?php
namespace App\Kernel;

{head}
{{
    public function testBoots(): void {{}}

    private function leftoverFixture(): void {{}}
}}
"""
    ingestor = _index(tmp_path, {"src/Kernel/BootChecks.php": source})
    members = _defined(ingestor, f"{_PROJECT}.src.Kernel.BootChecks.BootChecks.")

    for include_tests in (True, False):
        dead = _dead(ingestor, include_tests)
        assert not members & dead, (include_tests, sorted(members & dead))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/LibTest.php", True),
        ("src/Adapter/Local/LocalFilesystemAdapterTest.php", True),
        ("src/Symfony/Component/Console/Tests/Fixtures/DummyCommand.php", True),
        ("Tests/Fixtures/DummyCommand.php", True),
        ("tests/LibMovedTest.php", True),
    ],
)
def test_php_test_paths_are_recognised(path: str, expected: bool) -> None:
    assert _is_php_test_path(path) is expected


@pytest.mark.parametrize("include_tests", [True, False])
def test_symfony_tests_directory_is_test_code(
    tmp_path: Path, include_tests: bool
) -> None:
    fixture = r"""<?php
namespace Symfony\Component\Console\Tests\Fixtures;

class DummyCommand
{
    private function leftoverFixture(): void {}
}
"""
    ingestor = _index(
        tmp_path, {"src/Component/Console/Tests/Fixtures/DummyCommand.php": fixture}
    )
    members = _defined(ingestor, f"{_PROJECT}.src.Component.Console.Tests.")
    dead = _dead(ingestor, include_tests)

    assert not members & dead, sorted(members & dead)


# --- Negative: what must stay as it was -------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        # Only PHP's own conventions: the capitalised directory and the
        # `Test.php` suffix keep their meaning in other languages.
        "src/Tests/Fixture.java",
        "src/LibTest.py",
        "src/Contest.php",
        "src/Attest.php",
        # A class literally named `Test` is not a `*Test.php` test file.
        "src/Quiz/Test.php",
        "src/Testing/Fake.php",
        "src/TestsHelper/Fake.php",
    ],
)
def test_non_test_paths_are_not_test_code(path: str) -> None:
    assert _is_php_test_path(path) is False


@pytest.mark.parametrize("include_tests", [True, False])
def test_production_class_is_not_test_code(tmp_path: Path, include_tests: bool) -> None:
    # A `test*` name, a base merely containing `TestCase`, or a class named
    # like a test helper is not a PHPUnit test: its unused private methods
    # stay reported.
    source = r"""<?php
namespace App\Db;

class TestCaseFactory {}

class Connection extends TestCaseFactory
{
    private function testConnection(): bool { return true; }
}

class TestHelper
{
    private function unusedHelper(): void {}
}
"""
    ingestor = _index(tmp_path, {"src/Db/Connection.php": source})
    qn = f"{_PROJECT}.src.Db.Connection"
    dead = _dead(ingestor, include_tests)

    assert f"{qn}.Connection.testConnection" in dead, sorted(dead)
    assert f"{qn}.TestHelper.unusedHelper" in dead, sorted(dead)


@pytest.mark.parametrize("include_tests", [True, False])
def test_production_class_named_like_a_test_case_is_not_test_code(
    tmp_path: Path, include_tests: bool
) -> None:
    # A test-management domain has a `TestCase` entity; an importer may be
    # an `ImportTestCase`; a vendor base may end in `TestCase` without being
    # a test framework's. None has PHPUnit ancestry, so neither they nor
    # their subclasses are test code, and their unused methods are reported.
    source = r"""<?php
namespace App\Domain;

use Acme\Qa\ScenarioTestCase;
use Illuminate\Database\Eloquent\Model;

class TestCase extends Model { private function unusedBase(): void {} }

class RegressionTestCase extends TestCase { private function unusedSub(): void {} }

class ImportTestCase { private function unusedImport(): void {} }

class CsvImport extends ImportTestCase { private function unusedCsv(): void {} }

class Scenario extends ScenarioTestCase { private function unusedScenario(): void {} }
"""
    # The same entity imported from another namespace reaches the graph as
    # an external `App.Domain.TestCase`, not as the project class.
    smoke = r"""<?php
namespace App\Domain\Regression;

use App\Domain\TestCase;

class Smoke extends TestCase { private function unusedSmoke(): void {} }
"""
    ingestor = _index(
        tmp_path,
        {"src/Domain/Cases.php": source, "src/Domain/Regression/Smoke.php": smoke},
    )
    qn = f"{_PROJECT}.src.Domain.Cases"
    dead = _dead(ingestor, include_tests)

    smoke_qn = f"{_PROJECT}.src.Domain.Regression.Smoke.Smoke.unusedSmoke"
    assert smoke_qn in dead, sorted(dead)

    for member in (
        "TestCase.unusedBase",
        "RegressionTestCase.unusedSub",
        "ImportTestCase.unusedImport",
        "CsvImport.unusedCsv",
        "Scenario.unusedScenario",
    ):
        assert f"{qn}.{member}" in dead, (member, sorted(dead))


@pytest.mark.parametrize("include_tests", [True, False])
def test_first_party_base_under_tests_is_followed_to_subclasses_elsewhere(
    tmp_path: Path, include_tests: bool
) -> None:
    # The project's own base in `tests/` extends PHPUnit; a test class kept
    # in `src/` under another namespace extends it through a `use` import,
    # which the graph records as an external name.
    base = r"""<?php
namespace Tests;

abstract class BaseTestCase extends \PHPUnit\Framework\TestCase
{
    private function unusedBaseHelper(): void {}
}
"""
    checks = r"""<?php
namespace App\Feature;

use Tests\BaseTestCase;

class FeatureChecks extends BaseTestCase
{
    public function testFeature(): void {}

    private function leftoverFixture(): void {}
}
"""
    ingestor = _index(
        tmp_path,
        {"tests/BaseTestCase.php": base, "src/Feature/FeatureChecks.php": checks},
    )
    members = _defined(ingestor, f"{_PROJECT}.src.Feature.FeatureChecks.FeatureChecks.")
    dead = _dead(ingestor, include_tests)

    assert not members & dead, sorted(members & dead)


@pytest.mark.parametrize("include_tests", [True, False])
def test_imported_base_names_a_project_class_only_through_its_namespace(
    tmp_path: Path, include_tests: bool
) -> None:
    # `Acme\Shared\BaseTestCase` is a vendor class; the project's own
    # `tests/Shared/BaseTestCase.php` declares `Tests\Shared`. Sharing the
    # class name and the last directory does not make them one class, so
    # the production subclass keeps its unused method reported.
    base = r"""<?php
namespace Tests\Shared;

abstract class BaseTestCase extends \PHPUnit\Framework\TestCase {}
"""
    handler = r"""<?php
namespace App\Handlers;

use Acme\Shared\BaseTestCase;

class ProductionHandler extends BaseTestCase
{
    private function unusedBusinessLogic(): void {}
}
"""
    ingestor = _index(
        tmp_path,
        {
            "tests/Shared/BaseTestCase.php": base,
            "src/Handlers/ProductionHandler.php": handler,
        },
    )
    qn = f"{_PROJECT}.src.Handlers.ProductionHandler.ProductionHandler"
    dead = _dead(ingestor, include_tests)

    assert f"{qn}.unusedBusinessLogic" in dead, sorted(dead)


@pytest.mark.parametrize("include_tests", [True, False])
def test_imported_base_is_followed_through_its_namespace_in_any_directory(
    tmp_path: Path, include_tests: bool
) -> None:
    # The declared namespace, not the directory, is what a `use` import
    # names: a base kept outside the PSR-4 layout is still the same class.
    base = r"""<?php
namespace Tests;

abstract class IntegrationTestCase extends \PHPUnit\Framework\TestCase {}
"""
    checks = r"""<?php
namespace App\Feature;

use Tests\IntegrationTestCase;

class ApiChecks extends IntegrationTestCase
{
    private function leftoverFixture(): void {}
}
"""
    ingestor = _index(
        tmp_path,
        {
            "tests/Support/Legacy/IntegrationTestCase.php": base,
            "src/Feature/ApiChecks.php": checks,
        },
    )
    base_qn = f"{_PROJECT}.tests.Support.Legacy.IntegrationTestCase.IntegrationTestCase"
    # The engine reads the namespace from the dead-code fetch, so both the
    # stored property and the query column must be there.
    assert (
        ingestor.nodes[(cs.NodeLabel.CLASS.value, base_qn)].get(cs.KEY_NAMESPACE)
        == "Tests"
    )
    assert f" AS {cs.KEY_NAMESPACE}" in cq.CYPHER_DEAD_CODE_NODES
    members = _defined(ingestor, f"{_PROJECT}.src.Feature.ApiChecks.ApiChecks.")
    dead = _dead(ingestor, include_tests)

    assert not members & dead, sorted(members & dead)


# One file, two namespaces holding a class of the same name: only the one
# in `Tests` extends PHPUnit.
_MULTI_NAMESPACE_BASES = {
    "bracketed": r"""<?php
namespace Tests {
    abstract class BaseTestCase extends \PHPUnit\Framework\TestCase {}
}

namespace Other {
    class BaseTestCase {}
}
""",
    "sequential": r"""<?php
namespace Tests;

abstract class BaseTestCase extends \PHPUnit\Framework\TestCase {}

namespace Other;

class BaseTestCase {}
""",
    # The second same-named class registers as `BaseTestCase@<line>`.
    "sequential_test_base_second": r"""<?php
namespace Other;

class BaseTestCase {}

namespace Tests;

abstract class BaseTestCase extends \PHPUnit\Framework\TestCase {}
""",
}


def _index_multi_namespace(tmp_path: Path, form: str) -> _CapturingIngestor:
    checks = r"""<?php
namespace App\Feature;

use Tests\BaseTestCase;

class FeatureChecks extends BaseTestCase
{
    private function leftoverFixture(): void {}
}
"""
    prod = r"""<?php
namespace App\Feature;

use Other\BaseTestCase;

class Prod extends BaseTestCase
{
    private function unusedLogic(): void {}
}
"""
    return _index(
        tmp_path,
        {
            "src/Support/Bases.php": _MULTI_NAMESPACE_BASES[form],
            "src/Feature/FeatureChecks.php": checks,
            "src/Feature/Prod.php": prod,
        },
    )


@pytest.mark.parametrize("include_tests", [True, False])
@pytest.mark.parametrize("form", sorted(_MULTI_NAMESPACE_BASES))
def test_base_in_a_multi_namespace_file_is_linked_by_its_own_namespace(
    tmp_path: Path, form: str, include_tests: bool
) -> None:
    # Each class takes the namespace that lexically encloses it, so a test
    # base declared beside another namespace is still the class
    # `use Tests\BaseTestCase` names, and its subclass is test code.
    ingestor = _index_multi_namespace(tmp_path, form)
    namespaces = sorted(
        str(props.get(cs.KEY_NAMESPACE))
        for (label, uid), props in ingestor.nodes.items()
        if label == cs.NodeLabel.CLASS.value and ".src.Support.Bases." in str(uid)
    )
    assert namespaces == ["Other", "Tests"], namespaces
    dead = _dead(ingestor, include_tests)

    fixture = f"{_PROJECT}.src.Feature.FeatureChecks.FeatureChecks.leftoverFixture"
    assert fixture not in dead, sorted(dead)


@pytest.mark.parametrize("include_tests", [True, False])
@pytest.mark.parametrize("form", sorted(_MULTI_NAMESPACE_BASES))
def test_same_named_class_in_the_other_namespace_is_not_the_test_base(
    tmp_path: Path, form: str, include_tests: bool
) -> None:
    ingestor = _index_multi_namespace(tmp_path, form)
    dead = _dead(ingestor, include_tests)

    assert f"{_PROJECT}.src.Feature.Prod.Prod.unusedLogic" in dead, sorted(dead)


def test_class_outside_any_namespace_records_none(tmp_path: Path) -> None:
    ingestor = _index(tmp_path, {"src/plain.php": "<?php\nclass Plain {}\n"})
    plain = ingestor.nodes[(cs.NodeLabel.CLASS.value, f"{_PROJECT}.src.plain.Plain")]

    assert cs.KEY_NAMESPACE not in plain


@pytest.mark.parametrize("include_tests", [True, False])
def test_test_case_rule_is_php_only(tmp_path: Path, include_tests: bool) -> None:
    # A Python unittest class outside any test path inherits a `TestCase`
    # too; the PHPUnit class rule must not reach it.
    source = """\
import unittest


class Checks(unittest.TestCase):
    def _unused(self):
        return 1
"""
    ingestor = _index(tmp_path, {"pkg/checks.py": source})
    dead = _dead(ingestor, include_tests)

    assert f"{_PROJECT}.pkg.checks.Checks._unused" in dead, sorted(dead)


def test_production_code_beside_tests_is_still_analysed(tmp_path: Path) -> None:
    # The test class rule covers the test class only: the production class
    # in the next file keeps its own verdicts.
    ingestor = _index(tmp_path, {"src/Lib.php": _LIB, "src/LibTest.php": _LIB_TEST})
    dead = _dead(ingestor, include_tests=True)

    assert f"{_LIB_QN}.Lib.unused" in dead, sorted(dead)
    assert f"{_LIB_QN}.Sealed.neverCalled" in dead, sorted(dead)
