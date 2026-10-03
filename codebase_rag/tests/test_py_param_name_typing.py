# Untyped Python parameters were typed by fuzzy class-name matching (issue
# #2608): suffix and substring scores against every class in scope typed
# `self` as an imported `F` ("self".endswith("f")) and `cls` as `S`, so every
# `self.m()` / `cls.m()` in the module lost its CALLS edge, and `data` became
# class `A`, binding `data.count()` EXACTLY to `A.count`. The receiver is the
# enclosing class, never a name match; any other parameter is typed only when
# its whole name spells a class of three letters or more, and a call through
# such a guess is `heuristic`.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

_ISSUE_EXPRESSIONS = """\
class A:
    def count(self, x): return 1
class S:
    def run(self): return 2
class Payload:
    def encode(self): return b""
"""

_ISSUE_QUERY = """\
from pkg.expressions import A, S, Payload

class Service:
    def _helper(self):
        return 0

    def handle(self, data, payload):
        payload.encode()
        return self._helper() + data.count(1)

    @classmethod
    def build(cls, args):
        return cls.make() + len(args)

    @classmethod
    def make(cls):
        return 1
"""

Edges = dict[tuple[str, str], set[str]]


def _index(
    repo: Path,
    mock: MagicMock,
    expressions: str,
    query: str,
    rels: tuple[str, ...] = ("CALLS",),
) -> Edges:
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "expressions.py").write_text(expressions)
    (pkg / "query.py").write_text(query)
    create_and_run_updater(repo, mock)
    found: Edges = {}
    for c in mock.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) not in rels:
            continue
        props = c.kwargs.get("properties") or {}
        caller = str(c.args[0][2]).split(".pkg.", 1)[-1]
        callee = str(c.args[2][2]).split(".pkg.", 1)[-1]
        found.setdefault((caller, callee), set()).add(str(props.get(cs.KEY_RESOLUTION)))
    return found


def _one_class(name: str) -> str:
    return f"class {name}:\n    def __init__(self): pass\n    def go(self): return 1\n"


def _receiver_query(imported: str) -> str:
    return (
        f"from pkg.expressions import {imported}\n\n"
        "class QuerySet:\n"
        "    def _chain(self):\n        return 0\n\n"
        "    def filter(self):\n        return self._chain()\n\n"
        "    @classmethod\n"
        "    def create(cls):\n        return cls()\n\n"
        "    @classmethod\n"
        "    def build(cls):\n        return cls.make()\n\n"
        "    @classmethod\n"
        "    def make(cls):\n        return 1\n"
    )


class TestIssueRepro:
    def test_self_and_cls_calls_keep_their_edges(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _index(temp_repo, mock_ingestor, _ISSUE_EXPRESSIONS, _ISSUE_QUERY)
        assert ("query.Service.handle", "query.Service._helper") in calls
        assert ("query.Service.build", "query.Service.make") in calls

    def test_one_letter_class_does_not_type_data(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _index(temp_repo, mock_ingestor, _ISSUE_EXPRESSIONS, _ISSUE_QUERY)
        assert cs.EdgeResolution.EXACT not in calls.get(
            ("query.Service.handle", "expressions.A.count"), set()
        )

    def test_name_guessed_receiver_binds_heuristically(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _index(temp_repo, mock_ingestor, _ISSUE_EXPRESSIONS, _ISSUE_QUERY)
        assert calls[("query.Service.handle", "expressions.Payload.encode")] == {
            cs.EdgeResolution.HEURISTIC
        }


class TestReceiverIsNeverTypedByName:
    # Every class name the old suffix/substring score matched against `self`.
    @pytest.mark.parametrize("imported", ["F", "S", "E", "L", "Elf", "Sel"])
    def test_self_call_survives_a_look_alike_import(
        self, temp_repo: Path, mock_ingestor: MagicMock, imported: str
    ) -> None:
        calls = _index(
            temp_repo, mock_ingestor, _one_class(imported), _receiver_query(imported)
        )
        assert ("query.QuerySet.filter", "query.QuerySet._chain") in calls

    # ...and against `cls`.
    @pytest.mark.parametrize("imported", ["S", "C", "Ls"])
    def test_cls_call_survives_a_look_alike_import(
        self, temp_repo: Path, mock_ingestor: MagicMock, imported: str
    ) -> None:
        calls = _index(
            temp_repo, mock_ingestor, _one_class(imported), _receiver_query(imported)
        )
        assert ("query.QuerySet.build", "query.QuerySet.make") in calls

    @pytest.mark.parametrize("imported", ["S", "C"])
    def test_cls_call_never_constructs_a_look_alike(
        self, temp_repo: Path, mock_ingestor: MagicMock, imported: str
    ) -> None:
        # `cls()` constructs the enclosing class; the name-alike import must
        # never be what it instantiates or calls into.
        edges = _index(
            temp_repo,
            mock_ingestor,
            _one_class(imported),
            _receiver_query(imported),
            ("INSTANTIATES", "CALLS"),
        )
        assert not any(
            callee.startswith(f"expressions.{imported}")
            for caller, callee in edges
            if caller == "query.QuerySet.create"
        )

    def test_self_reaches_own_and_inherited_methods(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        query = (
            "from pkg.expressions import F\n\n"
            "class Base:\n    def inherited(self):\n        return 1\n\n"
            "class Child(Base):\n"
            "    def own(self):\n        return 0\n\n"
            "    def run(self):\n        return self.own() + self.inherited()\n"
        )
        calls = _index(temp_repo, mock_ingestor, _one_class("F"), query)
        assert ("query.Child.run", "query.Child.own") in calls
        assert ("query.Child.run", "query.Base.inherited") in calls


class TestNameGuessIsStrict:
    def test_class_name_suffix_is_not_a_match(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `user` is not `AppUser`: a suffix used to type it exactly.
        expressions = "class AppUser:\n    def greet(self): return 1\n"
        query = (
            "from pkg.expressions import AppUser\n\n"
            "def welcome(user):\n    return user.greet()\n"
        )
        calls = _index(temp_repo, mock_ingestor, expressions, query)
        assert cs.EdgeResolution.EXACT not in calls.get(
            ("query.welcome", "expressions.AppUser.greet"), set()
        )

    def test_short_class_name_never_types_a_parameter(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Even the whole name: `a` spells class `A`, which is too short to
        # tell a class from an ordinary variable.
        query = (
            "from pkg.expressions import A\n\ndef tally(a):\n    return a.count(1)\n"
        )
        calls = _index(temp_repo, mock_ingestor, _ISSUE_EXPRESSIONS, query)
        assert cs.EdgeResolution.EXACT not in calls.get(
            ("query.tally", "expressions.A.count"), set()
        )

    def test_snake_case_name_types_its_camel_case_class(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `user_repo` spells `UserRepo`. A same-module `Cache.load` is the
        # nearer same-named method, which an untyped receiver fell back to.
        expressions = "class UserRepo:\n    def load(self): return 1\n"
        query = (
            "from pkg.expressions import UserRepo\n\n"
            "class Cache:\n    def load(self):\n        return 2\n\n"
            "def fetch(user_repo):\n    return user_repo.load()\n"
        )
        calls = _index(temp_repo, mock_ingestor, expressions, query)
        assert calls.get(("query.fetch", "expressions.UserRepo.load")) == {
            cs.EdgeResolution.HEURISTIC
        }
        assert ("query.fetch", "query.Cache.load") not in calls

    # A parameter the body rebinds to a value of unknown type is no longer
    # what its name spells, so the name guess must not survive as the
    # receiver's type and bind `payload.encode()` exactly (#2788 review).
    @pytest.mark.parametrize(
        "rebinding",
        [
            "    payload = unknown_factory()\n",
            "    for payload in unknown_factory():\n        pass\n",
            "    with unknown_factory() as payload:\n        pass\n",
        ],
        ids=["assignment", "for-target", "with-target"],
    )
    def test_rebinding_to_unknown_value_never_binds_exactly(
        self, temp_repo: Path, mock_ingestor: MagicMock, rebinding: str
    ) -> None:
        expressions = "class Payload:\n    def encode(self): return 1\n"
        query = (
            "from pkg.expressions import Payload\n"
            "from vendor import unknown_factory\n\n"
            f"def send(payload):\n{rebinding}    return payload.encode()\n"
        )
        calls = _index(temp_repo, mock_ingestor, expressions, query)
        assert cs.EdgeResolution.EXACT not in calls.get(
            ("query.send", "expressions.Payload.encode"), set()
        )


class TestTypedReceiversUnchanged:
    def test_annotated_parameter_binds_exactly(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # An annotation is the type, not a guess, whatever the class's length.
        expressions = (
            "class Widget:\n    def render(self): return 1\n"
            "class F:\n    def go(self): return 1\n"
        )
        query = (
            "from pkg.expressions import Widget, F\n\n"
            "def draw(x: Widget, widget: F):\n"
            "    return x.render() + widget.go()\n"
        )
        calls = _index(temp_repo, mock_ingestor, expressions, query)
        assert calls[("query.draw", "expressions.Widget.render")] == {
            cs.EdgeResolution.EXACT
        }
        assert calls[("query.draw", "expressions.F.go")] == {cs.EdgeResolution.EXACT}

    def test_constructed_local_binds_exactly(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        expressions = "class Widget:\n    def render(self): return 1\n"
        query = (
            "from pkg.expressions import Widget\n\n"
            "def draw(widget):\n    widget = Widget()\n    return widget.render()\n"
        )
        calls = _index(temp_repo, mock_ingestor, expressions, query)
        assert calls[("query.draw", "expressions.Widget.render")] == {
            cs.EdgeResolution.EXACT
        }

    def test_default_valued_parameter_keeps_its_target(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        expressions = "class Widget:\n    def render(self): return 1\n"
        query = (
            "from pkg.expressions import Widget\n\n"
            "def draw(w=Widget()):\n    return w.render()\n"
        )
        calls = _index(temp_repo, mock_ingestor, expressions, query)
        assert ("query.draw", "expressions.Widget.render") in calls
