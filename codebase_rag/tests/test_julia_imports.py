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
