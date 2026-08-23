# The eval harness's in-memory ingestors stand in for the production graph
# service, whose node writes are MERGE (n {qn}) SET n += props - additive.
# Several passes deliberately emit PARTIAL rows for existing nodes (the Rust
# external-trait-override flag is {qualified_name, overrides_external} only),
# so a capture that replaces the whole property dict wipes path/span/
# fingerprint off real definitions and corrupts every eval built on it
# (dead-code loses path rules, duplicates counts the node as skipped).
from codebase_rag import constants as cs
from evals.cgr_graph import _CapturingIngestor, _StatefulIngestor

_METHOD = cs.NodeLabel.METHOD.value
_FULL = {
    cs.KEY_QUALIFIED_NAME: "proj.error.Error.fmt",
    cs.KEY_NAME: "fmt",
    cs.KEY_PATH: "src/error.rs",
    cs.KEY_START_LINE: 700,
    cs.KEY_END_LINE: 710,
    cs.KEY_AST_FINGERPRINT: "ffff",
}
_PARTIAL = {
    cs.KEY_QUALIFIED_NAME: "proj.error.Error.fmt",
    cs.KEY_OVERRIDES_EXTERNAL: True,
}


def test_capturing_ingestor_merges_partial_node_rows() -> None:
    ingestor = _CapturingIngestor()
    ingestor.ensure_node_batch(_METHOD, dict(_FULL))
    ingestor.ensure_node_batch(_METHOD, dict(_PARTIAL))
    props = ingestor.nodes[(_METHOD, "proj.error.Error.fmt")]
    assert props[cs.KEY_PATH] == "src/error.rs"
    assert props[cs.KEY_AST_FINGERPRINT] == "ffff"
    assert props[cs.KEY_OVERRIDES_EXTERNAL] is True


def test_capturing_ingestor_later_full_row_still_updates() -> None:
    # += semantics overwrite keys present in the new row; only absent keys
    # survive from the old one.
    ingestor = _CapturingIngestor()
    ingestor.ensure_node_batch(_METHOD, dict(_FULL))
    updated = dict(_FULL, **{cs.KEY_END_LINE: 720})
    ingestor.ensure_node_batch(_METHOD, updated)
    assert ingestor.nodes[(_METHOD, "proj.error.Error.fmt")][cs.KEY_END_LINE] == 720


def test_stateful_ingestor_merges_partial_node_rows() -> None:
    ingestor = _StatefulIngestor()
    ingestor.ensure_node_batch(_METHOD, dict(_FULL))
    ingestor.ensure_node_batch(_METHOD, dict(_PARTIAL))
    props = ingestor.nodes[(_METHOD, "proj.error.Error.fmt")]
    assert props[cs.KEY_PATH] == "src/error.rs"
    assert props[cs.KEY_OVERRIDES_EXTERNAL] is True
