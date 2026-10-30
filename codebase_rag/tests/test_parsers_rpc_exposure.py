"""`GoRpcExposureProcessor`: a connect-go `New<Stem>Handler(impl)` wiring
call makes each contract method the impl serves EXPOSE the unscoped
`resource::RPC::<Stem>.<Method>` node, resolving the impl through a
literal, a typed parameter, a constructor's recorded return type or an
embedded type, and only from bindings in scope at the call (issue #912)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.ast_cache import BoundedASTCache
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.rpc_exposure import GoRpcExposureProcessor
from codebase_rag.tests.collaborator_support import nodes_of, parse
from codebase_rag.tests.conftest import get_nodes, get_relationships
from evals.recorded_graph import RecordingIngestor

_REPO = Path("/repo")

_CONTRACT = """package userv1connect

type UserServiceHandler interface {
\tGetUser(id string) error
\tPutUser(id string) error
}

type UnimplementedUserServiceHandler struct{}

func (UnimplementedUserServiceHandler) PutUser(id string) error { return nil }
"""

_MAIN = """package main

import (
\t"example.com/app/gen/userv1connect"
\t"example.com/app/svc"
)

type Impl struct{}

type Base struct{}

type Embedding struct {
\tBase
\tuserv1connect.UnimplementedUserServiceHandler
}

func literal() {
\timpl := &Impl{}
\tuserv1connect.NewUserServiceHandler(impl)
}

func constructed() {
\tsrv := server.New()
\tuserv1connect.NewUserServiceHandler(srv)
}

func inline() {
\tuserv1connect.NewUserServiceHandler(server.New())
}

func parameter(impl *Impl) {
\tuserv1connect.NewUserServiceHandler(impl)
}

func promoted() {
\te := &Embedding{}
\tuserv1connect.NewUserServiceHandler(e)
}

func closedBlock(ok bool) {
\tif ok {
\t\timpl := &Impl{}
\t\t_ = impl
\t}
\tuserv1connect.NewUserServiceHandler(impl)
}

func shadowedByParameter(userv1connect *Factory) {
\timpl := &Impl{}
\tuserv1connect.NewUserServiceHandler(impl)
}

func shadowedByLocal() {
\tuserv1connect := NewFactory()
\timpl := &Impl{}
\tuserv1connect.NewUserServiceHandler(impl)
}

func notConnect() {
\timpl := &Impl{}
\tweb.NewUserServiceHandler(impl)
}
"""

_CONNECT_MODULE = "proj.gen.userv1connect.user"
_MODULE_PATHS = {
    "proj.main": _REPO / "main.go",
    _CONNECT_MODULE: _REPO / "gen/userv1connect/user.go",
    "proj.svc.server": _REPO / "svc/server.go",
}
_PACKAGE_NAMES = {
    "proj.main": "main",
    _CONNECT_MODULE: "userv1connect",
    "proj.svc.server": "server",
}
_TYPES = {
    "proj.main.Impl": ["proj.main.Impl.GetUser"],
    "proj.main.Base": ["proj.main.Base.GetUser"],
    "proj.main.Embedding": [],
    "proj.svc.server.Server": [
        "proj.svc.server.Server.GetUser",
        "proj.svc.server.Server.PutUser",
    ],
    f"{_CONNECT_MODULE}.UnimplementedUserServiceHandler": [
        f"{_CONNECT_MODULE}.UnimplementedUserServiceHandler.PutUser"
    ],
}


def _processor(
    ingestor: RecordingIngestor, selection: CaptureSelection | None = None
) -> tuple[GoRpcExposureProcessor, Node]:
    registry = FunctionRegistryTrie()
    lookup: dict[str, set[str]] = {}
    for type_qn, methods in _TYPES.items():
        registry[type_qn] = cs.NodeLabel.CLASS
        lookup.setdefault(type_qn.rsplit(".", 1)[-1], set()).add(type_qn)
        for method_qn in methods:
            registry[method_qn] = cs.NodeLabel.METHOD
    main_root = parse(cs.SupportedLanguage.GO, _MAIN)
    cache = BoundedASTCache()
    cache[_MODULE_PATHS["proj.main"]] = (main_root, cs.SupportedLanguage.GO)
    cache[_MODULE_PATHS[_CONNECT_MODULE]] = (
        parse(cs.SupportedLanguage.GO, _CONTRACT),
        cs.SupportedLanguage.GO,
    )
    imports = ImportProcessor(_REPO, "proj")
    # `svc` is the import path's segment; the code calls it by its `package
    # server` clause, which the processor matches across the imports.
    imports.import_mapping["proj.main"] = {
        "userv1connect": "proj.gen.userv1connect",
        "svc": "proj.svc",
        "web": "proj.web",
    }
    processor = GoRpcExposureProcessor(
        ingestor,
        imports,
        selection or resolve_capture([cs.CaptureGroup.IO.value]),
        registry,
        lookup,
        _MODULE_PATHS,
        _PACKAGE_NAMES,
        {"proj.svc.server.New": "Server"},
        cache,
    )
    return processor, main_root


def _exposed_from(
    caller: str,
    selection: CaptureSelection | None = None,
    language: cs.SupportedLanguage = cs.SupportedLanguage.GO,
) -> tuple[RecordingIngestor, set[tuple[str, str]]]:
    ingestor = RecordingIngestor()
    processor, root = _processor(ingestor, selection)
    (func,) = [
        node
        for node in nodes_of(root, cs.TS_GO_FUNCTION_DECLARATION)
        if (name := node.child_by_field_name(cs.FIELD_NAME)) is not None
        and name.text == caller.encode()
    ]
    processor.process_caller(func, "proj.main", language)
    edges = {
        (str(rel.source), str(rel.target))
        for rel in get_relationships(ingestor, cs.RelationshipType.EXPOSES)
    }
    return ingestor, edges


def test_a_literal_bound_impl_exposes_each_contract_method_it_defines() -> None:
    ingestor, edges = _exposed_from("literal")
    # PutUser is in the contract, but Impl does not define it.
    assert edges == {("proj.main.Impl.GetUser", "resource::RPC::UserService.GetUser")}
    (resource,) = get_nodes(ingestor, cs.NodeLabel.RESOURCE)
    assert resource.properties == {
        cs.KEY_QUALIFIED_NAME: "resource::RPC::UserService.GetUser",
        cs.KEY_NAME: "UserService.GetUser",
        cs.KEY_KIND: "RPC",
    }


@pytest.mark.parametrize("caller", ["constructed", "inline"])
def test_a_constructed_impl_resolves_through_the_recorded_return_type(
    caller: str,
) -> None:
    _, edges = _exposed_from(caller)
    assert edges == {
        ("proj.svc.server.Server.GetUser", "resource::RPC::UserService.GetUser"),
        ("proj.svc.server.Server.PutUser", "resource::RPC::UserService.PutUser"),
    }


def test_a_typed_parameter_names_the_impl() -> None:
    _, edges = _exposed_from("parameter")
    assert edges == {("proj.main.Impl.GetUser", "resource::RPC::UserService.GetUser")}


def test_a_promoted_method_exposes_but_a_generated_stub_does_not() -> None:
    # Embedding serves GetUser through Base; its PutUser is the codegen's
    # Unimplemented stub, which is no served RPC.
    _, edges = _exposed_from("promoted")
    assert edges == {("proj.main.Base.GetUser", "resource::RPC::UserService.GetUser")}


@pytest.mark.parametrize(
    "caller",
    [
        # The binding's block closed before the call.
        "closedBlock",
        # A local of the package's name makes the call a method on it.
        "shadowedByParameter",
        "shadowedByLocal",
        # A `New<Stem>Handler` from a package not generated by connect-go.
        "notConnect",
    ],
)
def test_a_call_that_is_not_in_scope_wiring_exposes_nothing(caller: str) -> None:
    ingestor, edges = _exposed_from(caller)
    assert edges == set()
    assert get_nodes(ingestor, cs.NodeLabel.RESOURCE) == []


def test_nothing_is_exposed_without_the_capture_or_outside_go() -> None:
    _, uncaptured = _exposed_from(
        "literal", selection=resolve_capture([cs.CAPTURE_TOKEN_NONE])
    )
    _, not_go = _exposed_from("literal", language=cs.SupportedLanguage.PYTHON)
    assert uncaptured == set()
    assert not_go == set()
