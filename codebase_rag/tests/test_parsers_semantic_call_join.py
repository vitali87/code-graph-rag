"""The declaration join the semantic frontends share: a call's site key from
its callee name token, and a compiler fact's target position bound to the
exact node the definition pass registered there (issue #1179)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.frontends.protocol import CallSiteKey, ResolvedCallSite
from codebase_rag.parsers.semantic_call_join import (
    call_site_key,
    declared_location,
    module_qn_for_rel_file,
    resolve_call_site,
)
from codebase_rag.tests.collaborator_support import nodes_of, parse
from codebase_rag.types_defs import FunctionLocation, FunctionSpanKey

_GO = cs.SupportedLanguage.GO
# The "é" before the call is two bytes in one column: the key's column is a
# byte offset, as tree-sitter and the compiler facts both count.
_CALLER = 'package main\n\nfunc main() {\n\tx := "é"; svc.Run(x)\n\tlocal()\n}\n'
_EXTERNAL = ("ExternalFunction", "go.external")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path / "repo"


@pytest.fixture
def module_paths(repo: Path) -> dict[str, Path]:
    return {
        "repo.main": repo / "main.go",
        "repo.pkg.svc": repo / "pkg" / "svc.go",
    }


@pytest.fixture
def locations() -> dict[FunctionSpanKey, FunctionLocation]:
    return {
        ("repo.pkg.svc", 7, 0): FunctionLocation(
            cs.NodeLabel.METHOD, "repo.pkg.svc.Svc.Run", "repo.pkg.svc.Svc"
        )
    }


def _calls() -> tuple[Node, Node]:
    remote, local = nodes_of(parse(_GO, _CALLER), "call_expression")
    return remote, local


def _name_token(call: Node) -> Node:
    function = call.child_by_field_name(cs.FIELD_FUNCTION)
    assert function is not None
    return function.child_by_field_name("field") or function


def _site_key(
    module_paths: dict[str, Path], repo: Path, seen: list[Node] | None = None
) -> Callable[[Node, str], CallSiteKey | None]:
    # The key the Go engine derives: the callee's name token, its simple name
    # passed through unchanged.
    def key(call: Node, module_qn: str) -> CallSiteKey | None:
        if seen is not None:
            seen.append(call)
        token = _name_token(call)
        assert token.text is not None
        return call_site_key(token, token.text.decode(), module_qn, module_paths, repo)

    return key


def test_a_site_key_is_the_name_tokens_file_line_byte_column_and_name(
    module_paths: dict[str, Path], repo: Path
) -> None:
    remote, local = _calls()
    token = _name_token(remote)
    assert call_site_key(token, "Run", "repo.main", module_paths, repo) == (
        "main.go",
        4,
        16,
        "Run",
    )
    # A nested module keys by its repo-relative POSIX path.
    assert call_site_key(
        _name_token(local), "local", "repo.pkg.svc", module_paths, repo
    ) == ("pkg/svc.go", 5, 1, "local")


def test_a_module_with_no_file_has_no_site_key(
    module_paths: dict[str, Path], repo: Path
) -> None:
    remote, _local = _calls()
    assert (
        call_site_key(_name_token(remote), "Run", "repo.gone", module_paths, repo)
        is None
    )


def test_the_file_to_module_index_is_built_lazily_and_in_place(
    module_paths: dict[str, Path], repo: Path
) -> None:
    index: dict[str, str] = {}
    assert module_qn_for_rel_file("pkg/svc.go", module_paths, repo, index) == (
        "repo.pkg.svc"
    )
    assert index == {"main.go": "repo.main", "pkg/svc.go": "repo.pkg.svc"}
    assert module_qn_for_rel_file("pkg/none.go", module_paths, repo, index) is None

    # A module registered after the first lookup rebuilds the caller's own
    # index rather than a copy, so the reference it holds stays current.
    module_paths["repo.pkg.late"] = repo / "pkg" / "late.go"
    held = index
    assert module_qn_for_rel_file("pkg/late.go", module_paths, repo, index) == (
        "repo.pkg.late"
    )
    assert held is index
    assert held["pkg/late.go"] == "repo.pkg.late"


def test_a_declared_position_binds_the_registered_node(
    module_paths: dict[str, Path],
    repo: Path,
    locations: dict[FunctionSpanKey, FunctionLocation],
) -> None:
    index: dict[str, str] = {}
    assert declared_location(
        "pkg/svc.go", 7, 0, locations, module_paths, repo, index
    ) == (cs.NodeLabel.METHOD, "repo.pkg.svc.Svc.Run")
    # A position nothing was registered at, and a file outside the index,
    # bind nothing: the caller falls back to its heuristics.
    assert (
        declared_location("pkg/svc.go", 7, 1, locations, module_paths, repo, index)
        is None
    )
    assert (
        declared_location("vendor/x.go", 7, 0, locations, module_paths, repo, index)
        is None
    )


def test_a_call_with_a_fact_binds_its_declaration(
    module_paths: dict[str, Path],
    repo: Path,
    locations: dict[FunctionSpanKey, FunctionLocation],
) -> None:
    remote, local = _calls()
    site_key = _site_key(module_paths, repo)
    remote_key = site_key(remote, "repo.main")
    assert remote_key is not None
    facts = {remote_key: ResolvedCallSite("Run", "pkg/svc.go", 7, 0)}

    def resolve(call: Node) -> tuple[str, str] | None:
        return resolve_call_site(
            call,
            "repo.main",
            site_key,
            facts,
            set(),
            _EXTERNAL,
            locations,
            module_paths,
            repo,
            {},
        )

    assert resolve(remote) == (cs.NodeLabel.METHOD, "repo.pkg.svc.Svc.Run")
    # Another call of the same caller keys by its own name token: no fact.
    assert resolve(local) is None


def test_a_fact_whose_target_was_never_registered_falls_back(
    module_paths: dict[str, Path], repo: Path
) -> None:
    remote, _local = _calls()
    site_key = _site_key(module_paths, repo)
    remote_key = site_key(remote, "repo.main")
    assert remote_key is not None
    assert (
        resolve_call_site(
            remote,
            "repo.main",
            site_key,
            {remote_key: ResolvedCallSite("Run", "pkg/svc.go", 7, 0)},
            set(),
            _EXTERNAL,
            {},
            module_paths,
            repo,
            {},
        )
        is None
    )


def test_a_call_the_compiler_proved_external_takes_the_sentinel(
    module_paths: dict[str, Path],
    repo: Path,
    locations: dict[FunctionSpanKey, FunctionLocation],
) -> None:
    remote, _local = _calls()
    site_key = _site_key(module_paths, repo)
    remote_key = site_key(remote, "repo.main")
    assert remote_key is not None
    assert (
        resolve_call_site(
            remote,
            "repo.main",
            site_key,
            {},
            {remote_key},
            _EXTERNAL,
            locations,
            module_paths,
            repo,
            {},
        )
        == _EXTERNAL
    )


def test_no_facts_skips_the_site_key_and_an_unkeyed_call_binds_nothing(
    module_paths: dict[str, Path],
    repo: Path,
    locations: dict[FunctionSpanKey, FunctionLocation],
) -> None:
    remote, _local = _calls()
    seen: list[Node] = []
    site_key = _site_key(module_paths, repo, seen)
    # A build without the frontend has no facts and never pays for a key.
    assert (
        resolve_call_site(
            remote,
            "repo.main",
            site_key,
            {},
            set(),
            _EXTERNAL,
            locations,
            module_paths,
            repo,
            {},
        )
        is None
    )
    assert seen == []

    # A caller in a module with no file has no key, whatever the facts hold.
    remote_key = site_key(remote, "repo.main")
    assert remote_key is not None
    assert (
        resolve_call_site(
            remote,
            "repo.gone",
            site_key,
            {remote_key: ResolvedCallSite("Run", "pkg/svc.go", 7, 0)},
            {remote_key},
            _EXTERNAL,
            locations,
            module_paths,
            repo,
            {},
        )
        is None
    )
