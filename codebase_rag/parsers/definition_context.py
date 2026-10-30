"""What every collaborator of the definition pass is built from.

`DefinitionProcessor` builds one `DefinitionContext` and hands it to each
collaborator it builds, which hand it on to theirs: the graph a definition
is written to, the registries and module paths it is registered in, the
import map its names resolve through and the facts the later passes read.
Each is the processor's own object, held by reference, so a write through
any collaborator reaches every reader."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ..types_defs import FunctionRegistryTrieProtocol, SimpleNameLookup
from .definition_facts import DefinitionFacts

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from .import_processor import ImportProcessor


class DefinitionContext(NamedTuple):
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    import_processor: ImportProcessor
    facts: DefinitionFacts
