# (H) #498: external modules get a dedicated ExternalModule label (mirroring
# (H) Package/ExternalPackage) instead of Module plus an is_external boolean the
# (H) LLM prompts and schemas never surfaced.
from pathlib import Path

from codebase_rag import constants as cs
from evals.cgr_graph import _capture

_IMPORTS = cs.RelationshipType.IMPORTS.value


def test_external_import_creates_external_module_node(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "import requests\n\n\ndef fetch() -> None:\n    requests.get\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")

    node_labels = {
        (str(label), str(props.get(cs.KEY_QUALIFIED_NAME)))
        for (label, _uid), props in ingestor.nodes.items()
    }
    assert (cs.NodeLabel.EXTERNAL_MODULE.value, "requests") in node_labels
    assert (cs.NodeLabel.MODULE.value, "requests") not in node_labels

    # (H) the IMPORTS edge must target the ExternalModule label, or the edge
    # (H) would dangle (no Module node with that qualified name exists).
    imports = {
        (str(fl), str(f), str(tl), str(t))
        for fl, f, rel, tl, t in ingestor.rels
        if rel == _IMPORTS
    }
    assert (
        cs.NodeLabel.MODULE.value,
        "proj.app",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "requests",
    ) in imports


def test_first_party_import_keeps_module_label(tmp_path: Path) -> None:
    (tmp_path / "util.py").write_text("def helper() -> None: ...\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from util import helper\n\n\ndef run() -> None:\n    helper()\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")

    imports = {
        (str(fl), str(f), str(tl), str(t))
        for fl, f, rel, tl, t in ingestor.rels
        if rel == _IMPORTS
    }
    assert (
        cs.NodeLabel.MODULE.value,
        "proj.app",
        cs.NodeLabel.MODULE.value,
        "proj.util",
    ) in imports
