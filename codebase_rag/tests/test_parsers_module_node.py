"""`ModuleNodes.write`: a code file's Module node, with the documentation,
interface fingerprint, flow coverage, language properties and generated
provenance it records, and the CONTAINS_MODULE edge from its package,
folder or project; and `module_docstring`, which reads a file's
documentation in its language's form."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.language_definitions import LanguageFileIngest
from codebase_rag.parsers.module_node import ModuleNodes, module_docstring
from codebase_rag.parsers.php.file_ingest import PhpFileIngest
from codebase_rag.tests.collaborator_support import parse
from codebase_rag.tests.conftest import get_nodes, get_relationships
from codebase_rag.types_defs import PropertyDict, RelEndpointSpec
from evals.recorded_graph import RecordingIngestor


class _Host:
    """The definition pass's side of a Module node: PHP's file collaborator
    records the file's namespace, every other language records nothing."""

    def __init__(self, generated_source_prefixes: list[str] | None = None) -> None:
        self.generated_source_prefixes = generated_source_prefixes or []

    def _for(self, language: cs.SupportedLanguage) -> LanguageFileIngest:
        if language == cs.SupportedLanguage.PHP:
            return PhpFileIngest()
        return LanguageFileIngest()


def _write(
    repo: Path,
    relative: str,
    source: str,
    language: cs.SupportedLanguage,
    structural_elements: dict[Path, str | None] | None = None,
    *,
    flow_capture_enabled: bool = True,
    host: _Host | None = None,
) -> tuple[PropertyDict, RelEndpointSpec]:
    ingestor = RecordingIngestor()
    nodes = ModuleNodes(
        host or _Host(),
        ingestor=ingestor,
        repo_path=repo,
        project_name="proj",
        flow_capture_enabled=flow_capture_enabled,
    )
    file_path = repo / relative
    module_qn = "proj." + ".".join(Path(relative).with_suffix("").parts)
    nodes.write(
        file_path,
        Path(relative),
        module_qn,
        language,
        parse(language, source),
        structural_elements or {},
    )
    (module,) = get_nodes(ingestor, cs.NodeLabel.MODULE)
    (contains,) = get_relationships(ingestor, cs.RelationshipType.CONTAINS_MODULE)
    assert contains.to_spec == (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
    return module.properties, contains.from_spec


@pytest.mark.parametrize(
    ("language", "source", "expected"),
    [
        # Python's is a string literal, read as a definition's is.
        (
            cs.SupportedLanguage.PYTHON,
            '"""Tools for x."""\nimport os\n',
            "Tools for x.",
        ),
        (cs.SupportedLanguage.PYTHON, "# a comment\nimport os\n", None),
        (
            cs.SupportedLanguage.GO,
            "// Package svc serves users.\npackage svc\n",
            "Package svc serves users.",
        ),
        (
            cs.SupportedLanguage.JS,
            "/**\n * Helpers for carts.\n */\nexport const a = 1;\n",
            "Helpers for carts.",
        ),
        # An ordinary comment in the doc position is not documentation.
        (cs.SupportedLanguage.JS, "// TODO: tidy\nexport const a = 1;\n", None),
    ],
)
def test_a_files_docstring_is_read_in_its_languages_form(
    language: cs.SupportedLanguage, source: str, expected: str | None
) -> None:
    assert module_docstring(parse(language, source), language) == expected


def test_a_python_module_records_its_identity_doc_and_interface(
    temp_repo: Path,
) -> None:
    props, container = _write(
        temp_repo,
        "pkg/tools.py",
        '"""Tools for x."""\n\ndef run(a):\n    return a\n',
        cs.SupportedLanguage.PYTHON,
        {Path("pkg"): "proj.pkg"},
    )
    fingerprint = props.pop(cs.KEY_INTERFACE_FINGERPRINT)
    assert isinstance(fingerprint, str) and len(fingerprint) == 64
    assert props == {
        cs.KEY_QUALIFIED_NAME: "proj.pkg.tools",
        cs.KEY_NAME: "tools.py",
        cs.KEY_PATH: "pkg/tools.py",
        cs.KEY_ABSOLUTE_PATH: (temp_repo / "pkg/tools.py").resolve().as_posix(),
        cs.KEY_FLOW_COVERED: True,
        cs.KEY_DOCSTRING: "Tools for x.",
    }
    assert container == (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, "proj.pkg")


def test_flow_coverage_needs_both_the_capture_and_a_registered_language(
    temp_repo: Path,
) -> None:
    off, _ = _write(
        temp_repo,
        "m.py",
        "x = 1\n",
        cs.SupportedLanguage.PYTHON,
        flow_capture_enabled=False,
    )
    # Julia has no flow sinks registered, so its module never claims to be
    # covered.
    julia, _ = _write(temp_repo, "m.jl", "module M end\n", cs.SupportedLanguage.JULIA)
    assert off[cs.KEY_FLOW_COVERED] is False
    assert julia[cs.KEY_FLOW_COVERED] is False


def test_the_language_collaborator_adds_its_own_properties(temp_repo: Path) -> None:
    props, _ = _write(
        temp_repo,
        "src/User.php",
        "<?php\nnamespace App\\Models;\nclass User {}\n",
        cs.SupportedLanguage.PHP,
    )
    assert props[cs.KEY_PHP_NAMESPACE] == "App.Models"
    # A language without fingerprint rules records none.
    assert cs.KEY_INTERFACE_FINGERPRINT not in props


def test_a_file_under_a_generated_source_prefix_names_its_generator(
    temp_repo: Path,
) -> None:
    host = _Host(["target/generated-sources/annotations/"])
    generated, _ = _write(
        temp_repo,
        "target/generated-sources/annotations/Foo.java",
        "class Foo {}\n",
        cs.SupportedLanguage.JAVA,
        host=host,
    )
    written, _ = _write(
        temp_repo,
        "src/Foo.java",
        "class Foo {}\n",
        cs.SupportedLanguage.JAVA,
        host=host,
    )
    assert generated[cs.KEY_GENERATED] is True
    assert generated[cs.KEY_GENERATOR] == "annotations"
    assert cs.KEY_GENERATED not in written
    assert cs.KEY_GENERATOR not in written


def test_a_module_outside_a_package_is_held_by_its_folder_or_the_project(
    temp_repo: Path,
) -> None:
    _, in_folder = _write(
        temp_repo, "scripts/run.py", "x = 1\n", cs.SupportedLanguage.PYTHON
    )
    _, at_root = _write(temp_repo, "run.py", "x = 1\n", cs.SupportedLanguage.PYTHON)
    assert in_folder == (
        cs.NodeLabel.FOLDER,
        cs.KEY_ABSOLUTE_PATH,
        (temp_repo / "scripts").resolve().as_posix(),
    )
    assert at_root == (cs.NodeLabel.PROJECT, cs.KEY_NAME, "proj")
