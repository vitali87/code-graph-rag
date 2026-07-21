# Python's ast ends a def at its last STATEMENT; a trailing comment
# inside the body (thrift's THttpServer.shutdown ends with a commented-out
# call) is invisible to it, while tree-sitter's block extends over the
# comment. The comment lexically belongs to the suite, so the oracle
# extends a def's end over trailing comment lines indented deeper than
# the def itself; a comment at the def's own indentation is a sibling
# and must not extend the span.
from __future__ import annotations

from pathlib import Path

from evals.ast_oracle import extract_oracle_graph

SRC = """class Server:
    def serve(self):
        self.count = 1

    def shutdown(self):
        self.close()
        # hangs forever otherwise!

    # sibling comment about the next method
    def restart(self):
        self.shutdown()
"""


def test_trailing_body_comment_extends_span(tmp_path: Path) -> None:
    (tmp_path / "server.py").write_text(SRC)
    graph = extract_oracle_graph(tmp_path, "proj")

    ends = {
        (node.key.kind, node.key.start_line): node.end_line
        for node in graph.nodes.values()
    }
    assert ends[("Method", 5)] == 7, ends
    assert ends[("Method", 10)] == 11, ends
    # The class block includes its last member's trailing extent.
    assert ends[("Class", 1)] == 11, ends
