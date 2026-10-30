"""`rename_spec`, the types a rename reports in (issue #1532): what a refusal
carries when the caller names no unplanned sites, what a report built
without its later fields reads as, the JSON a site serialises to, the
markers a site's resolution can hold, and the word pattern the doc scan
uses."""

from __future__ import annotations

import json
import re

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename_spec import (
    CHAIN,
    IDENTIFIER_RE,
    RECEIVER_UNKNOWN,
    SITELESS,
    STRUCTURAL,
    TS_LANGUAGES,
    TYPE_SPACE_LABELS,
    UNPLANNED,
    RenameRefused,
    RenameReport,
    RenameSite,
)
from codebase_rag.language_spec import get_language_for_extension

CALL_SITE = RenameSite(
    cs.RenameSiteKind.CALL,
    "pkg/app.py",
    7,
    30,
    "proj.pkg.app.run",
    cs.EdgeResolution.HEURISTIC.value,
)


def test_a_refusal_is_a_value_error_holding_the_sites_it_names() -> None:
    refused = RenameRefused("cannot rename", [CALL_SITE], ["proj.pkg.app.run (?)"])

    assert isinstance(refused, ValueError)
    assert str(refused) == "cannot rename"
    assert refused.ambiguous == [CALL_SITE]
    assert refused.unlocatable == ["proj.pkg.app.run (?)"]
    assert refused.unplanned == []


def test_refusals_without_unplanned_sites_do_not_share_one_list() -> None:
    # The MCP and CLI payloads serialise `unplanned` per refusal; a default
    # shared between instances would leak one refusal's sites into the next.
    first = RenameRefused("a", [], [])
    second = RenameRefused("b", [], [])

    first.unplanned.append(CALL_SITE)

    assert second.unplanned == []


def test_a_report_built_without_its_later_fields_reads_as_whole() -> None:
    # Callers that predate the contract, the rollback and the unplanned scan
    # build reports without those fields: such a report must read as a graph
    # that is complete, a rename that was never undone, and nothing guessed.
    report = RenameReport(
        qualified_name="proj.pkg.util.helper",
        old_name="helper",
        new_name="assist",
        applied=True,
        transaction_id="tx",
        files=("pkg/util.py",),
        sites=(CALL_SITE,),
        ambiguous=(),
        unlocatable=(),
        doc_mentions=(),
        hierarchy=("proj.pkg.util.helper",),
        diff="",
        message="ok",
    )

    assert report.verdict is None
    assert report.graph_incomplete is False
    assert report.undone is None
    assert report.unplanned == ()
    assert report.companions == ()


def test_a_site_serialises_to_plain_json_values() -> None:
    # A site reaches MCP and CLI clients as `_asdict()`; its kind is a
    # StrEnum, which must land as the bare string.
    payload = json.loads(json.dumps(CALL_SITE._asdict()))

    assert payload == {
        "kind": "call",
        "path": "pkg/app.py",
        "line": 7,
        "col": 30,
        "owner": "proj.pkg.app.run",
        "resolution": "heuristic",
    }
    assert RenameSite(**payload) == CALL_SITE


def test_the_rename_markers_never_read_as_a_graph_resolution() -> None:
    # The plan sorts sites by resolution: a marker equal to an edge's own
    # resolution (`exact`, say) would be planned as that edge.
    markers = [STRUCTURAL, SITELESS, CHAIN, UNPLANNED, RECEIVER_UNKNOWN]

    assert len(set(markers)) == len(markers)
    assert not set(markers) & {resolution.value for resolution in cs.EdgeResolution}


@pytest.mark.parametrize(
    "line",
    [
        "Call helper to do the thing.",
        "helper",
        "See `helper`.",
        "pkg.helper(1, 2)",
        "(helper)",
    ],
)
def test_the_identifier_pattern_finds_the_whole_name(line: str) -> None:
    assert re.search(IDENTIFIER_RE % re.escape("helper"), line)


@pytest.mark.parametrize(
    "line",
    ["helperish stays", "my_helper()", "helper2 = 1", "_helper", "helpers"],
)
def test_the_identifier_pattern_skips_a_longer_word(line: str) -> None:
    assert re.search(IDENTIFIER_RE % re.escape("helper"), line) is None


def test_the_identifier_pattern_takes_an_escaped_name_literally() -> None:
    pattern = IDENTIFIER_RE % re.escape("$scope")

    assert re.search(pattern, "return $scope.value;")
    assert re.search(pattern, "return $scopes;") is None
    assert re.search(pattern, "return xscope;") is None


def test_only_typescript_type_declarations_live_in_the_type_space() -> None:
    assert TYPE_SPACE_LABELS == {cs.NodeLabel.TYPE, cs.NodeLabel.INTERFACE}
    assert cs.NodeLabel.CLASS not in TYPE_SPACE_LABELS
    assert {get_language_for_extension(ext) for ext in (".ts", ".tsx")} == set(
        TS_LANGUAGES
    )
    assert get_language_for_extension(".js") not in TS_LANGUAGES
