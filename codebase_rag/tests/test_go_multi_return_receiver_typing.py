from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships, run_updater

_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])

_GO_MOD = "module example.com/multiret\n\ngo 1.22\n"

_MULTI_RETURN_SRC = """package main

type zmanager struct{}

func (m zmanager) Get(key string) ([]byte, error) { return nil, nil }

type provider struct{}

func (p provider) Get(path string) ([]byte, error) {
	cm, err := getManager()
	if err != nil {
		return nil, err
	}
	return cm.Get(path)
}

func getManager() (zmanager, error) { return zmanager{}, nil }
"""

_SINGLE_RETURN_SRC = """package main

type worker struct{}

func (w worker) Run() int { return 1 }

type owner struct{}

func (o owner) Run() int { return 2 }

func (o owner) Drive() int {
	h := makeWorker()
	return h.Run()
}

func makeWorker() worker { return worker{} }
"""

_EXTERNAL_RETURN_SRC = """package main

import "example.com/vendorpkg/thirdparty"

type provider struct{}

func (p provider) Do() {}

func (p provider) Fetch() {
	cl, err := connect()
	if err != nil {
		return
	}
	cl.Do()
}

func connect() (thirdparty.Client, error) {
	var c thirdparty.Client
	return c, nil
}
"""


def _calls(ingestor: MagicMock) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(ingestor, "CALLS")}


def _build(temp_repo: Path, name: str, source: str) -> set[tuple[str, str]]:
    root = temp_repo / name
    root.mkdir()
    (root / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    (root / "main.go").write_text(source, encoding="utf-8")
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="go")
    return _calls(ingestor)


def test_multi_return_free_fn_binding_dispatches_real_receiver(
    temp_repo: Path,
) -> None:
    # (H) `cm, err := getManager()` must type cm from getManager's FIRST return
    # (H) (Go's (T, error) idiom), so `cm.Get` binds zmanager.Get instead of
    # (H) trie-falling back to the enclosing provider.Get (a false SELF edge:
    # (H) viper's remoteConfigProvider.Get -> Get). zmanager sorts AFTER
    # (H) provider so an alphabetical trie coincidence cannot fake a pass.
    calls = _build(temp_repo, "multi", _MULTI_RETURN_SRC)

    assert any(
        caller.endswith("provider.Get") and callee.endswith("zmanager.Get")
        for caller, callee in calls
    ), calls
    assert not any(
        caller.endswith("provider.Get") and callee.endswith("provider.Get")
        for caller, callee in calls
    ), calls


def test_single_return_free_fn_binding_dispatches_real_receiver(
    temp_repo: Path,
) -> None:
    calls = _build(temp_repo, "single", _SINGLE_RETURN_SRC)

    assert any(
        caller.endswith("owner.Drive") and callee.endswith("worker.Run")
        for caller, callee in calls
    ), calls
    assert not any(
        caller.endswith("owner.Drive") and callee.endswith("owner.Run")
        for caller, callee in calls
    ), calls


_FLOW_SELF_EDGE_SRC = """package main

import (
	"os"

	"example.com/vendorpkg/thirdparty"
)

type provider struct{}

func (p provider) Get(path string) ([]byte, error) {
	cl, err := connect()
	if err != nil {
		return nil, err
	}
	b, err := cl.Get(path)
	if err != nil {
		return nil, err
	}
	if b == nil {
		raw, _ := os.ReadFile(path)
		return raw, nil
	}
	return b, nil
}

func connect() (thirdparty.Client, error) {
	var c thirdparty.Client
	return c, nil
}
"""


def test_flow_return_taint_honors_receiver_typing(temp_repo: Path) -> None:
    # (H) The flow walk resolves callees for return-taint edges itself; without
    # (H) the caller's local type map, `cl.Get` on an external-typed receiver
    # (H) unique-binds back to the enclosing provider.Get and finalize emits
    # (H) the FLOWS_TO self edge the CALLS pipeline had already correctly
    # (H) dropped (viper's remoteConfigProvider Get -> Get).
    root = temp_repo / "flowmulti"
    root.mkdir()
    (root / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    (root / "main.go").write_text(_FLOW_SELF_EDGE_SRC, encoding="utf-8")
    parsers, queries = load_parsers()
    ingestor = MagicMock()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()

    flows = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(ingestor, "FLOWS_TO")
    }
    assert not any(
        source.endswith("provider.Get") and target.endswith("provider.Get")
        for source, target in flows
    ), flows


def test_external_qualified_return_does_not_bind_local_decoy(
    temp_repo: Path,
) -> None:
    # (H) connect() returns an EXTERNAL package's type: `cl` is typed but the
    # (H) type resolves outside the repo, so `cl.Do()` must not bind the
    # (H) same-module provider.Do decoy.
    calls = _build(temp_repo, "external", _EXTERNAL_RETURN_SRC)

    assert not any(
        caller.endswith("provider.Fetch") and callee.endswith("provider.Do")
        for caller, callee in calls
    ), calls
