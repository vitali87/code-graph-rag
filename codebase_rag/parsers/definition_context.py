"""What every collaborator of the definition pass is built from.

`DefinitionProcessor` builds one `DefinitionContext` and hands it to each
collaborator it builds, which hand it on to theirs: the graph a definition
is written to, the registries and module paths it is registered in, the
import map its names resolve through and the facts the later passes read.
Each is the processor's own object, held by reference, so a write through
any collaborator reaches every reader.

A language's own containers live in that language's package
(`cpp/definition_state.py` and the like). The processor builds one of each,
exposes it by language (`processor.csharp.partial_index`) and hands the
class and function passes all of them as one `LanguageStates`, from which
each language collaborator is given its own."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ..types_defs import FunctionRegistryTrieProtocol, SimpleNameLookup
from .definition_facts import DefinitionFacts

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from .cpp.definition_state import CppDefinitionState
    from .csharp.definition_state import CSharpDefinitionState
    from .dart.definition_state import DartDefinitionState
    from .go.definition_state import GoDefinitionState
    from .import_processor import ImportProcessor
    from .java.definition_state import JavaDefinitionState
    from .rust.definition_state import RustDefinitionState


class DefinitionContext(NamedTuple):
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    import_processor: ImportProcessor
    facts: DefinitionFacts


class LanguageStates(NamedTuple):
    cpp: CppDefinitionState
    csharp: CSharpDefinitionState
    dart: DartDefinitionState
    go: GoDefinitionState
    java: JavaDefinitionState
    rust: RustDefinitionState
