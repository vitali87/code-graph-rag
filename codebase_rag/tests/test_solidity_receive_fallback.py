"""Solidity `receive()` and `fallback()` are Methods of their contract.

They are the code that runs when a contract is paid or called with unknown
calldata, but the tier had no rule for `fallback_receive_definition`, so
neither became a node: `resolve receive` was empty and a line inside one
resolved to the contract (issue #3205; OpenZeppelin: 0 of 11, among them
`Proxy.fallback`, which every OZ proxy delegates through).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.ast_grep_tier import AstGrepTier

_WALLET = """\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract Wallet {
    event Received(address from, uint256 amount);
    constructor() {}
    modifier onlyOwner() { _; }
    function withdraw(uint256 amount) external onlyOwner {
        payable(msg.sender).transfer(amount);
    }
    receive() external payable {
        emit Received(msg.sender, msg.value);
    }
    fallback() external payable {
        emit Received(msg.sender, msg.value);
    }
}
contract Proxy {
    fallback() external payable {}
    fallback(bytes calldata input) external returns (bytes memory) {
        return input;
    }
}
contract Plain {
    function ping() external pure returns (uint256) { return 1; }
}
"""

_Nodes = dict[str, tuple[str, int, int]]


@pytest.fixture(scope="module")
def nodes(tmp_path_factory: pytest.TempPathFactory) -> _Nodes:
    root = tmp_path_factory.mktemp("sol3205")
    path = root / "Wallet.sol"
    path.write_text(_WALLET, encoding="utf-8")
    mock = MagicMock()
    AstGrepTier(mock, root, "proj").process_file(path, {})
    out: _Nodes = {}
    for call in mock.ensure_node_batch.call_args_list:
        label, props = str(call.args[0]), call.args[1]
        if label in (cs.NodeLabel.METHOD.value, cs.NodeLabel.FUNCTION.value):
            qn = str(props[cs.KEY_QUALIFIED_NAME]).removeprefix("proj.Wallet_sol.")
            out[qn] = (label, props[cs.KEY_START_LINE], props[cs.KEY_END_LINE])
    return out


@pytest.mark.parametrize(
    ("qn", "span"),
    [("Wallet.receive", (10, 12)), ("Wallet.fallback", (13, 15))],
    ids=["receive", "fallback"],
)
def test_receive_and_fallback_are_methods_of_their_contract(
    nodes: _Nodes, qn: str, span: tuple[int, int]
) -> None:
    assert nodes.get(qn) == (cs.NodeLabel.METHOD.value, *span), nodes


def test_an_overloaded_fallback_keeps_both_definitions(nodes: _Nodes) -> None:
    fallbacks = sorted(
        (start, end)
        for qn, (_l, start, end) in nodes.items()
        if qn.startswith("Proxy.fallback")
    )
    assert fallbacks == [(18, 18), (19, 21)], nodes


@pytest.mark.parametrize(
    "qn",
    ["Wallet.constructor", "Wallet.onlyOwner", "Wallet.withdraw", "Plain.ping"],
)
def test_the_other_definitions_are_unchanged(nodes: _Nodes, qn: str) -> None:
    # Negatives: constructors, modifiers and functions were indexed before
    # and still are, and a contract with neither special function gains none.
    assert nodes[qn][0] == cs.NodeLabel.METHOD.value, nodes
    assert not [q for q in nodes if q.startswith("Plain.") and q != "Plain.ping"]
