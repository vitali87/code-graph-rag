"""`PythonResolver` and its helpers: module-level aliases, dispatch through a
Protocol stub, a receiver bound to an external module, an untyped receiver
whose bare-name guess is ambiguous, a typed receiver lacking the method, and
the dunders operator syntax and instance calls run."""

from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.call_site import CallSite
from codebase_rag.parsers.python.resolution import (
    PythonResolver,
    _binds_identifier,
    _module_level_assignments,
    _python_callee_name_node,
    _rebound_alias,
)
from codebase_rag.tests.collaborator_support import index, nodes_of, parse
from evals.recorded_graph import RecordingIngestor

_PY = cs.SupportedLanguage.PYTHON
_FILES = {
    "shapes.py": (
        "class Outer:\n    pass\n\n"
        "Alias = Outer\n"
        "Gone = Outer\nGone = make()\n\n"
        "def make():\n    return 1\n"
    ),
    "bag.py": (
        "class Bag:\n    def __len__(self):\n        return 0\n\n"
        "class Dispatcher:\n    def __call__(self):\n        return 1\n\n"
        "def build() -> Dispatcher:\n    return Dispatcher()\n"
    ),
    "proto.py": (
        "from typing import Protocol\n\n"
        "class Speaker(Protocol):\n    def speak(self) -> str: ...\n\n"
        "class Dog:\n    def speak(self) -> str:\n        return 'woof'\n\n"
        "class ClientProtocol(Protocol):\n    def get(self) -> int: ...\n\n"
        "class Client:\n    def get(self) -> int:\n        return 1\n"
    ),
    "jobs.py": (
        "import subprocess\n\n"
        "class Job:\n    def run(self):\n        return subprocess.run(['x'])\n\n"
        "class Task:\n    def run(self):\n        return 2\n"
    ),
}


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> PythonResolver:
    updater = index(temp_repo / "pp", mock_ingestor, _FILES)
    python = updater.factory.call_processor._resolver.python
    assert isinstance(python, PythonResolver)
    return python


def _site(
    call_name: str, module_qn: str, local_var_types: dict[str, str] | None = None
) -> CallSite:
    return CallSite(call_name, module_qn, local_var_types, None, None, _PY, None, False)


def test_the_last_module_level_binding_decides_an_alias(
    resolver: PythonResolver,
) -> None:
    assert resolver.module_level_alias_target("Alias", "pp.shapes") == "Outer"
    # Rebound to a value before anything runs: it names nothing.
    assert resolver.module_level_alias_target("Gone", "pp.shapes") is None
    assert resolver.module_level_alias_target("Missing", "pp.shapes") is None


def test_a_protocol_stub_dispatches_to_its_conformers(resolver: PythonResolver) -> None:
    assert resolver.protocol_dispatch_targets("pp.proto.Speaker.speak") == {
        (cs.NodeLabel.METHOD, "pp.proto.Dog.speak")
    }
    # An ordinary method is no dispatch.
    assert resolver.protocol_dispatch_targets("pp.proto.Dog.speak") == set()


def test_a_named_protocol_stub_is_replaced_by_its_implementer(
    resolver: PythonResolver,
) -> None:
    stub = (cs.NodeLabel.METHOD, "pp.proto.ClientProtocol.get")
    assert resolver.redirect_protocol_method(stub) == (
        cs.NodeLabel.METHOD,
        "pp.proto.Client.get",
    )
    plain = (cs.NodeLabel.METHOD, "pp.proto.Dog.speak")
    assert resolver.redirect_protocol_method(plain) == plain
    assert resolver.redirect_protocol_method(None) is None


def test_a_call_on_an_external_modules_attribute_has_no_project_target(
    resolver: PythonResolver,
) -> None:
    assert resolver.receiver_is_external_python_module(
        _site("subprocess.run", "pp.jobs")
    )
    # A local of that name shadows the module.
    assert not resolver.receiver_is_external_python_module(
        _site("subprocess.run", "pp.jobs", local_var_types={"subprocess": "Job"})
    )
    assert not resolver.receiver_is_external_python_module(_site("run", "pp.jobs"))


def test_an_untyped_receivers_guess_is_ambiguous_among_several_definers(
    resolver: PythonResolver,
) -> None:
    # `job.run()` on a value nothing typed: Job.run and Task.run both match.
    assert resolver.untyped_receiver_guess_is_ambiguous(_site("job.run", "pp.jobs"))
    # A typed receiver is the typed paths' to answer.
    assert not resolver.untyped_receiver_guess_is_ambiguous(
        _site("job.run", "pp.jobs", local_var_types={"job": "Job"})
    )
    assert not resolver.untyped_receiver_guess_is_ambiguous(_site("run", "pp.jobs"))


def test_a_typed_receiver_without_the_method_binds_nothing(
    resolver: PythonResolver,
) -> None:
    types = {"b": "Bag"}
    assert resolver.typed_receiver_lacks_method("b.missing", "pp.bag", types, _PY)
    assert not resolver.typed_receiver_lacks_method("b.__len__", "pp.bag", types, _PY)
    # Only Python's method set is read off the class qn.
    assert not resolver.typed_receiver_lacks_method(
        "b.missing", "pp.bag", types, cs.SupportedLanguage.GO
    )


def test_operator_and_instance_calls_run_the_typed_dunder(
    resolver: PythonResolver,
) -> None:
    assert resolver.operator_dunder_targets("b", "__len__", "pp.bag", {"b": "Bag"}) == {
        (cs.NodeLabel.METHOD, "pp.bag.Bag.__len__")
    }
    assert resolver.operator_dunder_targets("b", "__len__", "pp.bag", None) == set()
    assert resolver.instance_call_targets("build()", True, "pp.bag", {}) == {
        (cs.NodeLabel.METHOD, "pp.bag.Dispatcher.__call__")
    }


def test_a_chained_assignment_yields_every_link() -> None:
    root = parse(_PY, "A = B = Outer\nx.y = 1\nprint(A)\n")
    assignments = list(_module_level_assignments(root))
    assert [a.child_by_field_name("left").text for a in assignments] == [
        b"A",
        b"B",
        b"x.y",
    ]


def test_an_alias_is_rebound_by_a_value_or_an_unpacking() -> None:
    root = parse(
        _PY,
        "Alias = pkg.Outer\nAlias = 1\nAlias, other = pair()\nholder.Alias, x = p()\n",
    )
    to_dotted, to_value, unpacked, attribute = list(_module_level_assignments(root))
    assert _rebound_alias(to_dotted, "Alias", None) == "pkg.Outer"
    assert _rebound_alias(to_value, "Alias", "pkg.Outer") is None
    assert _rebound_alias(unpacked, "Alias", "pkg.Outer") is None
    # Assigning an attribute binds no module-level name.
    assert _rebound_alias(attribute, "Alias", "pkg.Outer") == "pkg.Outer"
    target = attribute.child_by_field_name("left")
    assert target is not None
    assert not _binds_identifier(target, "Alias")
    assert _binds_identifier(target, "x")


def _callee_token(call: Node) -> bytes | None:
    token = _python_callee_name_node(call)
    return token.text if token is not None else None


def test_the_callee_name_token_of_a_call() -> None:
    root = parse(_PY, "f()\nobj.m()\nitems[0]()\n")
    assert [_callee_token(c) for c in nodes_of(root, cs.TS_PY_CALL)] == [
        b"f",
        b"m",
        None,
    ]
