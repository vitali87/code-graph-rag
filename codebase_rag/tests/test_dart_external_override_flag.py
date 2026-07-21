# A Dart method overriding an EXTERNAL base class method (a Flutter widget's
# `build`/`initState`/`createState`) is invoked by the framework, never by
# first-party code, so it reported dead: 440 of 538 wonderous-app candidates
# were framework overrides. Unlike Python, the external base's method set is
# not introspectable, but Dart marks every override explicitly with
# `@override`; trust the annotation whenever the class has an external parent.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater


def _method_props(mock_ingestor: MagicMock) -> dict[str, dict]:
    return {
        c.args[1][cs.KEY_QUALIFIED_NAME]: c.args[1]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.METHOD
    }


def test_external_base_override_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "dartext"
    root.mkdir(parents=True)
    (root / "widget.dart").write_text(
        "import 'package:flutter/material.dart';\n"
        "\n"
        "class MyLabel extends StatelessWidget {\n"
        "  @override\n"
        "  Widget build(BuildContext context) {\n"
        "    return helper();\n"
        "  }\n"
        "\n"
        "  Widget helper() {\n"
        "    return Container();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="dart")
    props = _method_props(mock_ingestor)
    build = next(v for k, v in props.items() if k.endswith(".build"))
    helper = next(v for k, v in props.items() if k.endswith(".helper"))
    assert build.get(cs.KEY_OVERRIDES_EXTERNAL) is True, build
    assert not helper.get(cs.KEY_OVERRIDES_EXTERNAL), helper


def test_internal_base_override_is_not_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A first-party base resolves via OVERRIDES edges; the external-root
    # property must stay off so virtual dispatch keeps doing the work.
    root = temp_repo / "dartint"
    root.mkdir(parents=True)
    (root / "shapes.dart").write_text(
        "class Shape {\n"
        "  double area() {\n"
        "    return 0;\n"
        "  }\n"
        "}\n"
        "\n"
        "class Circle extends Shape {\n"
        "  @override\n"
        "  double area() {\n"
        "    return 3.14;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="dart")
    props = _method_props(mock_ingestor)
    override = next(
        v for k, v in props.items() if k.endswith("Circle.area")
    )
    assert not override.get(cs.KEY_OVERRIDES_EXTERNAL), override
