# (H) A src-root distribution (setup.py maps src/ to the package named after
# (H) the project) writes absolute imports against the DISTRIBUTION name
# (H) (`from thrift.Thrift import TProcessor`) while the oracle indexes
# (H) modules by PATH (`thrift.src.Thrift`), so every such import was called
# (H) external and cgr's correctly resolved edges graded as false positives
# (H) (thrift: IMPORTS precision 0.15). When an absolute import claims the
# (H) project's own top-level name but misses the index, it must be internal;
# (H) a UNIQUE whole-segment suffix match recovers the file.
from __future__ import annotations

from pathlib import Path

from evals.ast_oracle import extract_oracle_graph

PROJECT = "thrift"


def _write_src_layout(root: Path) -> None:
    src = root / "src"
    (src / "protocol").mkdir(parents=True)
    (src / "Thrift.py").write_text("class TProcessor(object):\n    pass\n")
    (src / "protocol" / "__init__.py").write_text("")
    (src / "protocol" / "TProtocol.py").write_text(
        "class TProtocolBase(object):\n    pass\n"
    )
    (src / "TMultiplexedProcessor.py").write_text(
        "from thrift.Thrift import TProcessor\n"
        "from thrift.protocol.TProtocol import TProtocolBase\n"
        "import thrift.protocol.fastbinary\n"
        "import struct\n\n\n"
        "class TMultiplexedProcessor(TProcessor):\n    pass\n"
    )


def test_distribution_name_imports_resolve_via_unique_suffix(tmp_path: Path) -> None:
    _write_src_layout(tmp_path)
    graph = extract_oracle_graph(tmp_path, PROJECT)

    import_targets = {
        edge.target_name
        for edge in graph.name_edges
        if edge.rel_type == "IMPORTS" and "TMultiplexedProcessor" in edge.source.file
    }
    assert "src/Thrift.py" in import_targets, import_targets
    assert "src/protocol/TProtocol.py" in import_targets, import_targets
    # (H) `import a.b.c` of a C extension still imports the deepest importable
    # (H) parent package.
    assert "src/protocol/__init__.py" in import_targets, import_targets
    # (H) `import struct` has a foreign top level; it must stay external.
    assert len(import_targets) == 3, import_targets
