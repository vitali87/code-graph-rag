"""Module-node emission shared by the non-tree-sitter tiers.

The ast-grep and document tiers both need a file's `Module` node and the
`CONTAINS_MODULE` edge tying it to whatever contains it, with the same flat
qualified name and the same Package/Folder/Project parent resolution. The
tree-sitter path (``definition_processor``) does not use this: it adds
``__init__``/``mod.rs`` handling, stem disambiguation and generated-source
provenance that only apply to code modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs
from ..utils.path_utils import cached_relative_path, cached_resolve_posix

if TYPE_CHECKING:
    from ..services import IngestorProtocol


def emit_flat_module(
    ingestor: IngestorProtocol,
    repo_path: Path,
    project_name: str,
    file_path: Path,
    structural_elements: dict[Path, str | None],
    distinguish_suffix: bool = False,
    extra_properties: dict[str, str] | None = None,
) -> str:
    """Emit a file's Module node and its containment edge; return its qn.

    The qualified name is flat: no ``__init__``/``mod`` special case, and by
    default the suffix is dropped, so ``a/b.sh`` and ``a/b.bash`` share a qn
    and therefore one Module node.

    ``distinguish_suffix`` keeps the extension in the name (``guide_md``) for
    tiers that accept several suffixes for the same language and must not
    merge two such files. It is opt-in because turning it on changes the qn
    of every module a tier emits.
    """
    relative_path = cached_relative_path(file_path, repo_path)
    parts = list(relative_path.with_suffix("").parts)
    if distinguish_suffix and relative_path.suffix and parts:
        # "guide.md" -> "guide_md": the dot would read as another level of
        # hierarchy to every consumer that splits a qn on it.
        parts[-1] = f"{parts[-1]}_{relative_path.suffix.lstrip(cs.SEPARATOR_DOT)}"
    module_qn = cs.SEPARATOR_DOT.join([project_name, *parts])
    module_props: dict[str, object] = {
        cs.KEY_QUALIFIED_NAME: module_qn,
        cs.KEY_NAME: file_path.name,
        cs.KEY_PATH: relative_path.as_posix(),
        cs.KEY_ABSOLUTE_PATH: cached_resolve_posix(file_path),
    }
    if extra_properties:
        # Caller-declared properties, currently Markdown front-matter
        # (issue #1448). Applied AFTER the identity fields and filtered by the
        # caller, so a declared `path:` cannot overwrite the node's real path.
        module_props.update(extra_properties)
    ingestor.ensure_node_batch(cs.NodeLabel.MODULE, module_props)
    ingestor.ensure_relationship_batch(
        _parent_ref(repo_path, project_name, relative_path.parent, structural_elements),
        cs.RelationshipType.CONTAINS_MODULE,
        (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
    )
    return module_qn


def _parent_ref(
    repo_path: Path,
    project_name: str,
    parent_rel_path: Path,
    structural_elements: dict[Path, str | None],
) -> tuple[cs.NodeLabel, str, str]:
    """What contains a module: its Package, else its Folder, else the Project.

    Folder identity is the ABSOLUTE path: the relative one is shared across
    same-layout projects in one graph, and keying on it merges their trees
    (issue #897).
    """
    if package_qn := structural_elements.get(parent_rel_path):
        return (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, package_qn)
    if parent_rel_path != Path(cs.PATH_CURRENT_DIR):
        return (
            cs.NodeLabel.FOLDER,
            cs.KEY_ABSOLUTE_PATH,
            cached_resolve_posix(repo_path / parent_rel_path),
        )
    return (cs.NodeLabel.PROJECT, cs.KEY_NAME, project_name)
