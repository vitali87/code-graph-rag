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
    # Mirror the ingestor's MERGE ... SET n += props semantics: a later
    # partial row (the deferred overrides_external update) merges into the
    # full row ingested during Pass 2.
    props: dict[str, dict] = {}
    for c in mock_ingestor.ensure_node_batch.call_args_list:
        if c.args[0] == cs.NodeLabel.METHOD:
            props.setdefault(c.args[1][cs.KEY_QUALIFIED_NAME], {}).update(c.args[1])
    return props


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
    override = next(v for k, v in props.items() if k.endswith("Circle.area"))
    assert not override.get(cs.KEY_OVERRIDES_EXTERNAL), override


def test_cross_file_internal_base_is_not_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The base lives in a file parsed AFTER the subclass, so it is not yet
    # registered at class-ingest time; the external-or-not decision must wait
    # for deferred inheritance resolution or a first-party override is
    # permanently rooted and its dead call tree hidden.
    root = temp_repo / "dartxfile"
    root.mkdir(parents=True)
    (root / "a_child.dart").write_text(
        "import 'z_base.dart';\n"
        "\n"
        "class Child extends Base {\n"
        "  @override\n"
        "  void tick() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "z_base.dart").write_text(
        "class Base {\n  void tick() {}\n}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="dart")
    props = _method_props(mock_ingestor)
    child_tick = next(v for k, v in props.items() if k.endswith("Child.tick"))
    assert not child_tick.get(cs.KEY_OVERRIDES_EXTERNAL), child_tick


def test_implements_external_interface_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `implements` targets are IMPLEMENTS, not INHERITS; a class whose only
    # external ancestry is an implemented framework interface still has its
    # annotated callbacks invoked by that framework.
    root = temp_repo / "dartimpl"
    root.mkdir(parents=True)
    (root / "handler.dart").write_text(
        "import 'package:events/events.dart';\n"
        "\n"
        "class Handler implements Listener {\n"
        "  @override\n"
        "  void onEvent() {}\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="dart")
    props = _method_props(mock_ingestor)
    on_event = next(v for k, v in props.items() if k.endswith("Handler.onEvent"))
    assert on_event.get(cs.KEY_OVERRIDES_EXTERNAL) is True, on_event


def test_mixed_ancestry_flags_only_names_missing_on_registered_base(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A class with a first-party base AND an external mixin: an @override of
    # the registered base's method resolves via OVERRIDES edges and must not
    # be rooted, while a name the registered ancestry does not define can
    # only override the external mixin.
    root = temp_repo / "dartmixed"
    root.mkdir(parents=True)
    (root / "app.dart").write_text(
        "import 'package:anim/anim.dart';\n"
        "\n"
        "class Base {\n"
        "  void tick() {}\n"
        "}\n"
        "\n"
        "class C extends Base with FrameMixin {\n"
        "  @override\n"
        "  void tick() {}\n"
        "\n"
        "  @override\n"
        "  void onFrame() {}\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="dart")
    props = _method_props(mock_ingestor)
    tick = next(v for k, v in props.items() if k.endswith("C.tick"))
    on_frame = next(v for k, v in props.items() if k.endswith("C.onFrame"))
    assert not tick.get(cs.KEY_OVERRIDES_EXTERNAL), tick
    assert on_frame.get(cs.KEY_OVERRIDES_EXTERNAL) is True, on_frame


def test_registered_implemented_interface_method_is_not_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A class implementing BOTH a registered interface and an external one:
    # `implements` targets never enter class_inheritance, so the ancestry
    # walk must consult the resolved IMPLEMENTS parents too or the registered
    # interface's method is wrongly rooted, hiding it and its call tree from
    # dead-code results. A name only the external interface can declare still
    # roots.
    root = temp_repo / "dartimplmixed"
    root.mkdir(parents=True)
    (root / "app.dart").write_text(
        "import 'package:events/events.dart';\n"
        "\n"
        "abstract class Registered {\n"
        "  void run();\n"
        "}\n"
        "\n"
        "class Worker implements Registered, ExternalListener {\n"
        "  @override\n"
        "  void run() {}\n"
        "\n"
        "  @override\n"
        "  void onEvent() {}\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="dart")
    props = _method_props(mock_ingestor)
    run = next(v for k, v in props.items() if k.endswith("Worker.run"))
    on_event = next(v for k, v in props.items() if k.endswith("Worker.onEvent"))
    assert not run.get(cs.KEY_OVERRIDES_EXTERNAL), run
    assert on_event.get(cs.KEY_OVERRIDES_EXTERNAL) is True, on_event
