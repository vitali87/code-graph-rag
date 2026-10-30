"""A file's Module node: the properties it is written with, and the package,
folder or project that contains it.

`DefinitionProcessor` writes each file's module through the `ModuleNodes`
it owns, once the file's module qn is settled. It calls back through
`ModuleNodeHost` for the file collaborator of a language, which adds the
properties its language records, and for the annotation-processor output
prefixes the graph updater sets on the pass for each run."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .. import constants as cs
from ..types_defs import ASTNode, PropertyDict
from ..utils.path_utils import cached_resolve_posix
from .interface_fingerprint import interface_fingerprint
from .io_access import FLOW_REGISTERED_LANGUAGES
from .java.generated import generator_hint
from .language_definitions import LanguageFileIngest
from .module_docstring import extract_module_docstring
from .python.docstring import python_docstring

if TYPE_CHECKING:
    from ..services import IngestorProtocol


def module_docstring(root_node: ASTNode, language: cs.SupportedLanguage) -> str | None:
    """The documentation for a whole file, in whatever form its language uses.

    Python's is a string literal, read as a definition's is; every other
    language marks it with a comment convention, which needs the marker
    prefixes in `module_docstring` because the grammars report a doc
    comment and an ordinary one as the same node type.
    """
    if language == cs.SupportedLanguage.PYTHON:
        return python_docstring(root_node)
    return extract_module_docstring(root_node, language)


class ModuleNodeHost(Protocol):
    """What writing a Module node reads from the definition pass that owns
    it: the file collaborator of a language, and the run's
    annotation-processor output prefixes."""

    generated_source_prefixes: list[str]

    def _for(self, language: cs.SupportedLanguage) -> LanguageFileIngest: ...


class ModuleNodes:
    """Writes a file's Module node and the CONTAINS_MODULE edge to it.

    Built and owned by `DefinitionProcessor`."""

    __slots__ = (
        "_host",
        "ingestor",
        "repo_path",
        "project_name",
        "flow_capture_enabled",
    )

    def __init__(
        self,
        host: ModuleNodeHost,
        *,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        flow_capture_enabled: bool,
    ) -> None:
        self._host = host
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.flow_capture_enabled = flow_capture_enabled

    def write(
        self,
        file_path: Path,
        relative_path: Path,
        module_qn: str,
        language: cs.SupportedLanguage,
        root_node: ASTNode,
        structural_elements: dict[Path, str | None],
    ) -> None:
        module_props = self._module_props(
            file_path, relative_path.as_posix(), module_qn, language, root_node
        )
        self.ingestor.ensure_node_batch(cs.NodeLabel.MODULE, module_props)
        self.ingestor.ensure_relationship_batch(
            self._container_of(relative_path, structural_elements),
            cs.RelationshipType.CONTAINS_MODULE,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
        )

    def _module_props(
        self,
        file_path: Path,
        relative_path_str: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        root_node: ASTNode,
    ) -> PropertyDict:
        module_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: module_qn,
            cs.KEY_NAME: file_path.name,
            cs.KEY_PATH: relative_path_str,
            cs.KEY_ABSOLUTE_PATH: cached_resolve_posix(file_path),
            cs.KEY_FLOW_COVERED: (
                self.flow_capture_enabled and language in FLOW_REGISTERED_LANGUAGES
            ),
        }
        if docstring := module_docstring(root_node, language):
            module_props[cs.KEY_DOCSTRING] = docstring
        if fingerprint := interface_fingerprint(root_node, language):
            module_props[cs.KEY_INTERFACE_FINGERPRINT] = fingerprint
        self._host._for(language).record_module_props(root_node, module_props)
        prefixes = self._host.generated_source_prefixes
        if prefixes and (hint := generator_hint(relative_path_str, prefixes)):
            module_props[cs.KEY_GENERATED] = True
            module_props[cs.KEY_GENERATOR] = hint
        return module_props

    def _container_of(
        self, relative_path: Path, structural_elements: dict[Path, str | None]
    ) -> tuple[str, str, str]:
        # The package the file's directory is, else that directory's Folder,
        # else the project for a file at the repository root.
        parent_rel_path = relative_path.parent
        if parent_container_qn := structural_elements.get(parent_rel_path):
            return cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, parent_container_qn
        if parent_rel_path != Path("."):
            return (
                cs.NodeLabel.FOLDER,
                cs.KEY_ABSOLUTE_PATH,
                cached_resolve_posix(self.repo_path / parent_rel_path),
            )
        return cs.NodeLabel.PROJECT, cs.KEY_NAME, self.project_name
