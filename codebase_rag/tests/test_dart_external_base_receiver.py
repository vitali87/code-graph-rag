# Dart undeclared-receiver typing via the external superclass's type
# arguments (issue #875): `widget._btnSize(context)` inside a class
# extending Flutter's `State<GridBtn>` must reach `GridBtn._btnSize`.
# The receiver `widget` is the inherited `State<T>.widget` property of an
# EXTERNAL base, so it is undeclared in first-party scope and receiver
# typing produces nothing; the only first-party types the external base
# can hand back are its type arguments, so the member binds when exactly
# one argument's class defines it. Registry guarded and conservative:
# a first-party base, an ambiguous member, or an own member of the same
# name all keep the edge dropped.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

SKIP = "dart"

APP_DART = """
import 'package:flutter/material.dart';

class GridBtn {
  GridBtn();

  double _btnSize(BuildContext context) {
    return 1.0;
  }
}

class OtherMenu {
  OtherMenu();

  double _btnSize(BuildContext context) {
    return 2.0;
  }
}

class GridBtnState extends State<GridBtn> {
  Widget build(BuildContext context) {
    return SizedBox(width: widget._btnSize(context));
  }
}

class PairState extends Pair<GridBtn, OtherMenu> {
  void poke(BuildContext context) {
    peer._btnSize(context);
  }
}

class Box<T> {
  Box();
}

class BoxSub extends Box<GridBtn> {
  void poke(BuildContext context) {
    mystery._btnSize(context);
  }
}

class OwnState extends State<GridBtn> {
  OtherMenu get helper {
    return OtherMenu();
  }

  void poke(BuildContext context) {
    helper._btnSize(context);
  }
}

class ParamState extends State<GridBtn> {
  void poke(OtherMenu menu, BuildContext context) {
    menu._btnSize(context);
  }
}
"""


@pytest.fixture
def dart_state_project(temp_repo: Path) -> Path:
    root = temp_repo / "dstate"
    root.mkdir()
    (root / "app.dart").write_text(APP_DART, encoding="utf-8")
    return root


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.CALLS.value
    }


def _has(edges: set[tuple[str, str]], src: str, dst: str) -> bool:
    return any(s.endswith(src) and d.endswith(dst) for s, d in edges)


def test_undeclared_receiver_binds_via_extends_type_argument(
    dart_state_project: Path, mock_ingestor: MagicMock
):
    # The issue's repro: two first-party `_btnSize` methods defeat the
    # unique-member gate, so only the `State<GridBtn>` type argument can
    # bind the call.
    run_updater(dart_state_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".GridBtnState.build", ".GridBtn._btnSize"), sorted(calls)
    assert not _has(calls, ".GridBtnState.build", ".OtherMenu._btnSize"), sorted(calls)


def test_ambiguous_type_arguments_drop_the_edge(
    dart_state_project: Path, mock_ingestor: MagicMock
):
    # Both of Pair's type arguments define `_btnSize`: no unique owner, so
    # the call must not bind either.
    run_updater(dart_state_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    assert not _has(calls, ".PairState.poke", ".GridBtn._btnSize"), sorted(calls)
    assert not _has(calls, ".PairState.poke", ".OtherMenu._btnSize"), sorted(calls)


def test_first_party_base_keeps_the_fallback_conservative(
    dart_state_project: Path, mock_ingestor: MagicMock
):
    # Box is first party: ordinary inheritance resolution owns its members,
    # so the type-argument path must not fire for BoxSub's untyped receiver.
    run_updater(dart_state_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    assert not _has(calls, ".BoxSub.poke", ".GridBtn._btnSize"), sorted(calls)


def test_own_member_receiver_is_not_the_external_property(
    dart_state_project: Path, mock_ingestor: MagicMock
):
    # `helper` is OwnState's OWN getter (an OtherMenu), not a member handed
    # back by the external base; binding GridBtn._btnSize would be wrong.
    run_updater(dart_state_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    assert not _has(calls, ".OwnState.poke", ".GridBtn._btnSize"), sorted(calls)


def test_declared_receiver_keeps_typed_resolution(
    dart_state_project: Path, mock_ingestor: MagicMock
):
    # A parameter-typed receiver already resolves through the local-type
    # path; the type-argument fallback must not preempt it.
    run_updater(dart_state_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".ParamState.poke", ".OtherMenu._btnSize"), sorted(calls)
    assert not _has(calls, ".ParamState.poke", ".GridBtn._btnSize"), sorted(calls)
