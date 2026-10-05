# Issue #2533: the innerhtml_xss and document_write security rules fired on any
# right-hand side, so clearing an element (`el.innerHTML = ''`) or writing fixed
# markup produced SecurityIssue nodes that cannot inject anything. On fastapi's
# termynal.js four of five hits were constant strings, burying the one real
# interpolated sink. Constant string literals and substitution-free template
# strings must stay clean; anything that can carry data must still be flagged.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.analyzers import FindingAnalyzer
from codebase_rag.capture import resolve_capture

INNERHTML_XSS = "innerhtml_xss"
DOCUMENT_WRITE = "document_write"

# Every extension the JS/TS/TSX security rule files register, so a fix applied
# to one grammar's YAML but forgotten in another fails here.
_EXTENSIONS = [".js", ".jsx", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx"]

# Lines 86/134/139/154/228 of fastapi docs/en/docs/js/termynal.js, condensed.
TERMYNAL_JS = (
    "this.container.innerHTML = '';\n"
    "this.container.innerHTML = '';\n"
    'restart.innerHTML = "restart ↻";\n'
    'finish.innerHTML = "fast →";\n'
    "div.innerHTML = `<span ${this._attributes(line)}>${line.value || ''}</span>`;\n"
)

CONSTANT_INNERHTML = (
    "a.innerHTML = '';\n"
    'b.innerHTML = "<b>fixed</b>";\n'
    "c.innerHTML = `<i>no substitution</i>`;\n"
    "d.innerHTML += '<br>';\n"
    'e.outerHTML = "<hr>";\n'
    "f.outerHTML += ``;\n"
)

DATA_INNERHTML = (
    "a.innerHTML = userInput;\n"
    "b.innerHTML = `<b>${name}</b>`;\n"
    "c.innerHTML = '<b>' + name + '</b>';\n"
    "d.innerHTML += row;\n"
    "e.outerHTML = render(model);\n"
    "f.outerHTML += `${tail}`;\n"
    "g.textContent = userInput;\n"
)

CONSTANT_DOCUMENT_WRITE = (
    'document.write("<p>static</p>");\n'
    "document.write('<p>a</p>', \"<p>b</p>\");\n"
    "document.write(`<p>plain template</p>`);\n"
)

DATA_DOCUMENT_WRITE = (
    "document.write(userInput);\n"
    'document.write("<p>", userInput);\n'
    "document.write(`<p>${userInput}</p>`);\n"
    "document.write(...parts);\n"
    "document.write('<p>' + userInput);\n"
)


def _hit_lines(tmp_path: Path, ext: str, src: str, rule_id: str) -> list[int]:
    f = tmp_path / f"probe{ext}"
    f.write_text(src, encoding="utf-8")
    nodes: list[dict] = []

    class _Ingestor:
        def ensure_node_batch(self, label, props) -> None:
            nodes.append(props)

        def ensure_relationship_batch(self, src, rel, dst) -> None:
            pass

    FindingAnalyzer(_Ingestor(), tmp_path, resolve_capture(["+findings"])).analyze(
        {"probe": f}
    )
    return sorted(p[cs.KEY_START_LINE] for p in nodes if p[cs.KEY_NAME] == rule_id)


@pytest.mark.parametrize("ext", _EXTENSIONS)
def test_termynal_constant_assignments_not_flagged(tmp_path: Path, ext: str) -> None:
    # Only the interpolated template on line 5 can carry data into the DOM.
    assert _hit_lines(tmp_path, ext, TERMYNAL_JS, INNERHTML_XSS) == [5]


@pytest.mark.parametrize("ext", _EXTENSIONS)
def test_innerhtml_constant_literals_not_flagged(tmp_path: Path, ext: str) -> None:
    assert _hit_lines(tmp_path, ext, CONSTANT_INNERHTML, INNERHTML_XSS) == []


@pytest.mark.parametrize("ext", _EXTENSIONS)
def test_document_write_constant_literals_not_flagged(tmp_path: Path, ext: str) -> None:
    assert _hit_lines(tmp_path, ext, CONSTANT_DOCUMENT_WRITE, DOCUMENT_WRITE) == []


# Negative tests: the exclusion must stay narrow. Identifiers, interpolated
# templates, concatenation with a variable, calls and spreads all carry data.


@pytest.mark.parametrize("ext", _EXTENSIONS)
def test_innerhtml_data_sinks_still_flagged(tmp_path: Path, ext: str) -> None:
    # textContent (line 7) never parses markup and stays clean.
    hits = _hit_lines(tmp_path, ext, DATA_INNERHTML, INNERHTML_XSS)
    assert hits == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("ext", _EXTENSIONS)
def test_document_write_data_sinks_still_flagged(tmp_path: Path, ext: str) -> None:
    # One constant argument next to a data argument (line 2) is still a sink.
    hits = _hit_lines(tmp_path, ext, DATA_DOCUMENT_WRITE, DOCUMENT_WRITE)
    assert hits == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("ext", [".js", ".ts", ".tsx"])
def test_eval_of_constant_string_still_flagged(tmp_path: Path, ext: str) -> None:
    # The literal exclusion is scoped to HTML sinks; eval of a constant string
    # is still arbitrary code execution and keeps its finding.
    assert _hit_lines(tmp_path, ext, 'eval("1 + 1");\n', "eval_use") == [1]
