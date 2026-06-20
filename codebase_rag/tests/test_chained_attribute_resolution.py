# (H) L3 finding from the evals/ harness: GraphUpdater.run calls
# (H) self.factory.definition_processor.process_all_method_overrides(), a three-level
# (H) chain where factory is an instance attribute (ProcessorFactory), definition_processor
# (H) is a @property returning DefinitionProcessor, and the method is inherited from a
# (H) mixin base. A module-level function of the same name makes the bare-name trie
# (H) fallback ambiguous, so the chain types must be walked to land on the mixin method.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    # (H) OverrideMixin is re-exported through the package __init__, so the subclass
    # (H) records its base as the re-export QN (pkg.overrides.OverrideMixin) rather than
    # (H) the real definition (pkg.overrides.mixin.OverrideMixin); inherited-method
    # (H) lookup must follow the re-export. A same-named module-level function competes.
    "pkg/overrides/__init__.py": (
        "from .mixin import OverrideMixin, process_all\n\n"
        "__all__ = ['OverrideMixin', 'process_all']\n"
    ),
    "pkg/overrides/mixin.py": (
        "def process_all():\n    return None\n\n\n"
        "class OverrideMixin:\n"
        "    def process_all(self):\n"
        "        return None\n"
    ),
    "pkg/defproc.py": (
        "from .overrides import OverrideMixin\n\n\n"
        "class DefProc(OverrideMixin):\n"
        "    def other(self):\n"
        "        return None\n"
    ),
    "pkg/factory.py": (
        "from .defproc import DefProc\n\n\n"
        "class Factory:\n"
        "    def __init__(self) -> None:\n"
        "        self._dp = None\n\n"
        "    @property\n"
        "    def definition_processor(self) -> DefProc:\n"
        "        if self._dp is None:\n"
        "            self._dp = DefProc()\n"
        "        return self._dp\n"
    ),
    "pkg/runner.py": (
        "from .factory import Factory\n\n\n"
        "class Runner:\n"
        "    def __init__(self) -> None:\n"
        "        self.factory = Factory()\n\n"
        "    def run(self):\n"
        "        return self.factory.definition_processor.process_all()\n"
    ),
}


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _calls(tmp_path: Path) -> set[tuple[PropertyValue, PropertyValue]]:
    for rel, content in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return {
        (frm, to) for (frm, rel, to) in cap.rels if rel == cs.RelationshipType.CALLS
    }


class TestChainedAttributeResolution:
    def test_three_level_chain_resolves_to_inherited_mixin_method(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.runner.Runner.run",
            "proj.pkg.overrides.mixin.OverrideMixin.process_all",
        ) in calls, calls

    def test_does_not_resolve_to_module_level_function(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.runner.Runner.run",
            "proj.pkg.overrides.mixin.process_all",
        ) not in calls, calls
