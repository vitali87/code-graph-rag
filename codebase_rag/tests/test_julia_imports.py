# Julia import edges: using/import statements, selected imports, aliases,
# and first-party module resolution (path + unique-stem + proximity).
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_relationships,
)

SKIP = "julia"


def test_using_first_party_module(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_imports"
    project.mkdir()
    (project / "Utils.jl").write_text(
        """
util_fn(x) = x + 1
""",
        encoding="utf-8",
    )
    (project / "main.jl").write_text(
        """
using .Utils

function run()
    return util_fn(1)
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("Utils", "").endswith(".Utils"), mapping

    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert any(
        src.endswith(".main") and dst.endswith(".Utils") for src, dst in imports
    ), imports


def test_using_dotted_relative(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_imports"
    project.mkdir()
    sub = project / "lib"
    sub.mkdir()
    (sub / "models.jl").write_text(
        """
model_fn() = 1
""",
        encoding="utf-8",
    )
    (project / "main.jl").write_text(
        """
using .lib.models

function run()
    return model_fn()
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("models", "").endswith(".lib.models"), mapping


def test_import_selected_members(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_imports"
    project.mkdir()
    (project / "api.jl").write_text(
        """
alpha() = 1
beta() = 2
""",
        encoding="utf-8",
    )
    (project / "main.jl").write_text(
        """
import .api: alpha, beta

function run()
    return alpha() + beta()
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    # Selected members bind under the member name to the source module.
    assert mapping.get("alpha", "").endswith(".api"), mapping
    assert mapping.get("beta", "").endswith(".api"), mapping

    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert any(
        src.endswith(".main") and dst.endswith(".api") for src, dst in imports
    ), imports


def test_dotted_project_name_first_party(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A Julia repo dir is conventionally `Pkg.jl`: the project name itself
    carries a dot, and first-party targets must keep the FULL dotted root
    (`my.lib.jl.Utils`), not truncate to its first segment (`my.Utils`)."""
    project = temp_repo / "my.lib.jl"
    project.mkdir()
    (project / "Utils.jl").write_text(
        """
util_fn(x) = x + 1
""",
        encoding="utf-8",
    )
    (project / "main.jl").write_text(
        """
using .Utils

function run()
    return util_fn(1)
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("Utils") == f"{project.name}.Utils", mapping

    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert (module_qn, f"{project.name}.Utils") in imports, imports


def test_declared_module_name_differs_from_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`using my.lib.SimulationModels` where the module is declared in a
    file that is NOT named after it (src/router/models.jl declares
    `module SimulationModels`): the path and stem lookups miss it, and the
    declared `module X` is the only first-party link to its file."""
    project = temp_repo / "my.lib.jl"
    project.mkdir()
    router = project / "src" / "router"
    router.mkdir(parents=True)
    (router / "models.jl").write_text(
        """
module SimulationModels
    model_fn() = 1
end
""",
        encoding="utf-8",
    )
    (project / "main.jl").write_text(
        """
using my.lib.SimulationModels

function run()
    return SimulationModels.model_fn()
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("SimulationModels") == f"{project.name}.src.router.models", (
        mapping
    )

    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert (module_qn, f"{project.name}.src.router.models") in imports, imports


def test_declared_module_nested_package_boundary(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A vendored dependency with its own Project.toml is a SEPARATE Julia
    package: its same-named declared module must not shadow this package's
    own (a `using Pkg.Models` cannot reach it)."""
    project = temp_repo / "my.lib.jl"
    project.mkdir()
    (project / "Project.toml").write_text('name = "my.lib"\n', encoding="utf-8")
    src = project / "src"
    src.mkdir()
    (src / "models.jl").write_text(
        """
module Models
    own() = 1
end
""",
        encoding="utf-8",
    )
    vendor = project / "vendor" / "dep"
    vendor.mkdir(parents=True)
    (vendor / "Project.toml").write_text('name = "dep"\n', encoding="utf-8")
    (vendor / "models.jl").write_text(
        """
module Models
    vendored() = 2
end
""",
        encoding="utf-8",
    )
    (project / "main.jl").write_text(
        """
using my.lib.Models

function run()
    return Models.own()
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    # The vendor's same-named declaration is out of the package boundary,
    # so the first-party file is the unique candidate.
    assert mapping.get("Models") == f"{project.name}.src.models", mapping


def test_external_module_name_kept_as_written(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A Julia import that names no first-party module stays external with
    its path AS WRITTEN: `Pkg.SomeModule` is a module path, not a
    `package.Class` pair, so nothing is stripped off the end."""
    project = temp_repo / "julia_imports"
    project.mkdir()
    (project / "main.jl").write_text(
        """
using SomePkg.SomeModule

function run()
    return 1
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("SomeModule") == "SomePkg.SomeModule", mapping


def test_using_alias_and_external(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_imports"
    project.mkdir()
    (project / "main.jl").write_text(
        """
using JSON3 as JSON
using Dates

function run()
    return 1
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    # `as` binds the alias to the full external path.
    assert mapping.get("JSON") == "JSON3", mapping
    # Absolute single-segment names stay external as-is.
    assert mapping.get("Dates") == "Dates", mapping


def test_relative_import_parent_dir(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """`using ..Sibling` from `nested/a/b.jl` resolves one level UP (to
    `nested/Sibling`), not to the importer's own directory: the old
    `lstrip(".")` collapsed every depth to 1 (issue #1882 review)."""
    project = temp_repo / "julia_reldepth"
    (project / "nested" / "a").mkdir(parents=True)
    (project / "nested" / "a" / "b.jl").write_text(
        "using ..Sibling\n", encoding="utf-8"
    )
    (project / "nested" / "Sibling.jl").write_text("x = 1\n", encoding="utf-8")
    # The decoy at the importer's own depth: what the old code found.
    (project / "nested" / "a" / "Sibling.jl").write_text("y = 2\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.nested.a.b"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("Sibling") == f"{project.name}.nested.Sibling", mapping


def test_excluded_module_not_in_discovery(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A module under an excluded (generated) directory must not enter the
    Julia discovery indexes: the importer's own exclude/unignore filter now
    applies there, matching the indexer walk (issue #1882 review)."""
    project = temp_repo / "julia_excl"
    project.mkdir()
    (project / "generated").mkdir()
    (project / "generated" / "dep.jl").write_text("gen_fn(x) = x\n", encoding="utf-8")
    (project / "main.jl").write_text("using .generated.dep\n", encoding="utf-8")

    updater = create_and_run_updater(
        project,
        mock_ingestor,
        skip_if_missing=SKIP,
        exclude_paths=frozenset({"generated"}),
    )

    index = updater.factory.import_processor._julia_declared or {}
    assert "generated/dep.jl" not in index, index
    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    module_qn = f"{project.name}.main"
    assert not any(src == module_qn for src, _ in imports), imports


def test_over_climb_relative_import_external(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`using ....Sibling` from `a/b/c.jl` climbs above the project root: no
    first-party file can live there, so the import stays external and must
    not bind a root-level same-named decoy (issue #1882 review: the clamp to
    the root base produced a false internal IMPORTS edge)."""
    project = temp_repo / "julia_overclimb"
    (project / "a" / "b").mkdir(parents=True)
    (project / "a" / "b" / "c.jl").write_text("using ....Sibling\n", encoding="utf-8")
    # The root-level decoy the old clamp bound.
    (project / "Sibling.jl").write_text("x = 1\n", encoding="utf-8")

    create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    module_qn = f"{project.name}.a.b.c"
    prefix = f"{project.name}."
    # An ExternalModule edge for the as-written name is fine; binding the
    # root-level decoy as a first-party module is the bug.
    assert not any(
        src == module_qn and dst.startswith(prefix) for src, dst in imports
    ), imports


def test_nested_package_stem_not_matched(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A unique vendored file below a nested Project.toml is a SEPARATE
    package: it may sit in the stem index, but a cross-package lookup must
    reject it at the package-root boundary (issue #1882 review: the stem
    index had no boundary, so `using .Utils` mapped to an unreachable
    vendored module)."""
    project = temp_repo / "julia_nestedpkg"
    (project / "dep").mkdir(parents=True)
    (project / "dep" / "Project.toml").write_text('name = "Dep"\n', encoding="utf-8")
    (project / "dep" / "Utils.jl").write_text("util_fn(x) = x\n", encoding="utf-8")
    (project / "main.jl").write_text("using .Utils\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    module_qn = f"{project.name}.main"
    prefix = f"{project.name}."
    # No first-party target at all: neither the stem index nor the flush's
    # suffix recovery may bind the vendored module.
    assert not any(
        src == module_qn and dst.startswith(prefix) for src, dst in imports
    ), imports


def test_dotted_absolute_import_stays_external(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`using ExternalPkg.Models` names an external package: the repo-wide
    stem/declared fallback must not rewrite it to a local `models.jl`
    (issue #1882 review: the fallback ran for ANY dotted import)."""
    project = temp_repo / "julia_absdotted"
    project.mkdir()
    (project / "models.jl").write_text("m_fn(x) = x\n", encoding="utf-8")
    (project / "main.jl").write_text("using ExternalPkg.Models\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("Models") != f"{project.name}.models", mapping
    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    # An ExternalModule edge for the as-written name is fine; the local
    # `models.jl` must not be the target.
    assert not any(
        src == module_qn and dst == f"{project.name}.models" for src, dst in imports
    ), imports


def test_self_dotted_absolute_import_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A self-reference by the project's own name (`using <Pkg>.Sub`) must
    still resolve first-party when the absolute-dotted fallback is gated on
    the current-package prefix (issue #1882 review)."""
    project = temp_repo / "julia_selfdotted"
    project.mkdir()
    (project / "Sub.jl").write_text("sub_fn(x) = x\n", encoding="utf-8")
    (project / "main.jl").write_text(f"using {project.name}.Sub\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("Sub") == f"{project.name}.Sub", mapping


def test_gone_julia_file_invalidates_stem_cache(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A DELETED .jl file must invalidate the stem/declared caches on a
    retained-updater reingest: a later re-parse of the importer then selects
    the now-unique surviving same-named file instead of the deleted path
    (issue #1882 review: invalidation ran only for reparsed files)."""
    from watchdog.events import FileDeletedEvent, FileModifiedEvent

    import realtime_updater

    project = temp_repo / "julia_gonecache"
    (project / "old").mkdir(parents=True)
    (project / "Utils.jl").write_text("util_fn(x) = x\n", encoding="utf-8")
    (project / "old" / "Utils.jl").write_text("old_fn(x) = x\n", encoding="utf-8")
    (project / "old" / "main.jl").write_text("using .Utils\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)
    key = f"{project.name}.old.main"
    # Both files match the stem: the importer's own dir wins the proximity
    # tie-break, so the committed mapping points at the nested file.
    assert (
        updater.factory.import_processor.import_mapping.get(key, {}).get("Utils")
        == f"{project.name}.old.Utils"
    )

    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    (project / "old" / "Utils.jl").unlink()
    handler.dispatch(FileDeletedEvent(str(project / "old" / "Utils.jl")))

    # Touch the importer so it re-parses against the FRESH layout.
    main_jl = project / "old" / "main.jl"
    main_jl.write_text("using .Utils  # touched\n", encoding="utf-8")
    handler.dispatch(FileModifiedEvent(str(main_jl)))

    mapping = updater.factory.import_processor.import_mapping.get(key, {})
    # The deleted path is gone from the (rebuilt) stem index; the now-unique
    # survivor is the root-level file.
    assert mapping.get("Utils") == f"{project.name}.Utils", mapping


def test_selected_macro_import_mapping(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`using .M: @foo` names the member as a `macro_identifier`, not an
    `identifier`: the local mapping must use the name WITHOUT the `@` so
    the invocation `@foo` reduces to the same local name as
    julia_call_name (issue #1882 review: selected macro imports were
    silently skipped)."""
    project = temp_repo / "julia_selmacro"
    project.mkdir()
    (project / "M.jl").write_text("macro foo(x)\n    return x\nend\n", encoding="utf-8")
    (project / "main.jl").write_text("using .M: @foo\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("foo") == f"{project.name}.M", mapping


def test_selected_macro_import_alias(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """`using .M: @foo as f` keeps the alias on the `as` side while the
    macro_identifier head is stripped of its `@`."""
    project = temp_repo / "julia_selmacro_alias"
    project.mkdir()
    (project / "M.jl").write_text("macro foo(x)\n    return x\nend\n", encoding="utf-8")
    (project / "main.jl").write_text("using .M: @foo as f\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("f") == f"{project.name}.M", mapping


def test_same_nested_package_import(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """A relative import between two files of the SAME nested package
    (their own Project.toml) is a normal sibling import: the
    nested-package guard must reject only targets in ANOTHER nested
    package (issue #1882 review: the guard rejected every target below
    a nested boundary and dropped the edge)."""
    project = temp_repo / "julia_samenested"
    dep = project / "dep"
    dep.mkdir(parents=True)
    (project / "Project.toml").write_text('name = "Root"\n', encoding="utf-8")
    (dep / "Project.toml").write_text('name = "Dep"\n', encoding="utf-8")
    (dep / "Bar.jl").write_text("module Bar\nv = 1\nend\n", encoding="utf-8")
    (dep / "foo.jl").write_text("using .Bar\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    prefix = f"{project.name}."
    assert any(
        src == f"{prefix}dep.foo" and dst == f"{prefix}dep.Bar" for src, dst in imports
    ), imports


def test_selected_macro_import_at_alias(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`using .M: @view as @v` — the alias side itself is a macro name:
    strip the `@` from the local name, matching julia_call_name's
    reduction of the `@v` invocation (issue #1882 review: the alias
    kept its `@` and could never resolve)."""
    project = temp_repo / "julia_at_alias"
    project.mkdir()
    (project / "M.jl").write_text(
        "macro view(x)\n    return x\nend\n", encoding="utf-8"
    )
    (project / "main.jl").write_text("using .M: @view as @v\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("v") == f"{project.name}.M", mapping


def test_scoped_nested_module_import(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """A `using .X` inside a nested `module` names a submodule declared in
    the same file; two nested modules importing a same-named member must
    keep separate bindings (issue #1882 review: both wrote the file-level
    map and the last one won)."""
    project = temp_repo / "julia_scoped_nested"
    project.mkdir()
    (project / "main.jl").write_text(
        """
module Outer
module First
alpha() = 1
end
using .First: alpha
f() = alpha()
end
module Second
module SecondFirst
alpha() = 2
end
using .SecondFirst: alpha
g() = alpha()
end
""",
        encoding="utf-8",
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    prefix = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping
    assert mapping.get(f"{prefix}.Outer", {}).get("alpha") == f"{prefix}.Outer.First"
    assert mapping.get(f"{prefix}.Second", {}).get("alpha") == (
        f"{prefix}.Second.SecondFirst"
    )

    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert (f"{prefix}.Outer.f", f"{prefix}.Outer.First.alpha") in calls, calls
    assert (
        f"{prefix}.Second.g",
        f"{prefix}.Second.SecondFirst.alpha",
    ) in calls, calls


def test_nested_package_declared_module_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`using .SimulationModels` in a nested package's file binds a module
    declared in a sibling file of the SAME package; the declared-name index
    must stay reachable for nested packages (issue #1882 review: the
    index skipped every nested file and the edge was dropped)."""
    project = temp_repo / "julia_np_declared"
    dep = project / "dep"
    dep.mkdir(parents=True)
    (project / "Project.toml").write_text('name = "Root"\n', encoding="utf-8")
    (dep / "Project.toml").write_text('name = "Dep"\n', encoding="utf-8")
    (dep / "models.jl").write_text("module SimulationModels\nend\n", encoding="utf-8")
    (dep / "foo.jl").write_text(
        "module Foo\nusing .SimulationModels\nend\n", encoding="utf-8"
    )

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    prefix = f"{project.name}."
    assert any(
        src == f"{prefix}dep.foo" and dst == f"{prefix}dep.models"
        for src, dst in imports
    ), imports


def test_absolute_external_path_not_captured(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`using ExternalPkg.Models` must not be rewritten to a local
    `ExternalPkg/Models.jl` that happens to sit at the same path: the
    exact-path lookup is a first-party link only (issue #1882 review:
    it ran for every absolute import)."""
    project = temp_repo / "julia_abs_ext"
    ext = project / "ExternalPkg"
    ext.mkdir(parents=True)
    (project / "Project.toml").write_text('name = "MyProj"\n', encoding="utf-8")
    (ext / "Models.jl").write_text("module Models\nm = 1\nend\n", encoding="utf-8")
    (project / "main.jl").write_text("using ExternalPkg.Models\n", encoding="utf-8")

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})
    assert mapping.get("Models") == "ExternalPkg.Models", mapping
    imports = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    prefix = f"{project.name}."
    assert not any(
        src == module_qn and dst.startswith(prefix) for src, dst in imports
    ), imports
