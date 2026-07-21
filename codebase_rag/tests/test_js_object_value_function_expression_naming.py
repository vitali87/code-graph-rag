# An anonymous function expression used as an OBJECT VALUE (`var pets = {
# list: function(req, res) {...}, delete: function(req, res) {...} }`,
# express's route-map example) must NOT climb past the pair to the enclosing
# declarator and steal its name -- that registers phantom `pets`/`pets@50`
# Function nodes that nothing references (false dead). The pair-key naming
# path owns object values.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater


def test_object_value_fn_expr_does_not_take_declarator_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "exobj"
    root.mkdir(parents=True)
    (root / "index.js").write_text(
        "var pets = {\n"
        "  list: function(req, res){\n"
        "    return res\n"
        "  },\n"
        "  delete: function(req, res){\n"
        "    return req\n"
        "  }\n"
        "};\n"
        "exports.map = { get: pets.list, del: pets.delete }\n",
        encoding="utf-8",
    )
    updater = create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    registry = updater.factory.function_registry
    phantom = [
        qn
        for qn, label in registry.items()
        if qn.split(".")[-1].split("@")[0] == "pets" and str(label) == "Function"
    ]
    assert not phantom, phantom
