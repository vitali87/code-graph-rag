# (H) A `#if` directive that splits an if/else chain mid-method makes tree-sitter
# (H) parse-recover the trailing `else if (...)` as a local_function_statement
# (H) whose `name` field is the reserved keyword `if`. That is a parse artifact,
# (H) not a real function; cgr must not emit a Function/Method node for it (it was
# (H) polluting the graph, e.g. 184 bogus "if" nodes in Newtonsoft.Json).
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_node_names, run_updater

SKIP = "c_sharp"


def test_if_split_body_emits_no_keyword_named_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cs_split_body"
    project.mkdir()
    (project / "Converter.cs").write_text(
        "namespace N;\n"
        "public class Converter {\n"
        "    public void Write(object value) {\n"
        "        if (value is int) { Emit(1); }\n"
        "#if HAVE_DATE_TIME_OFFSET\n"
        "        else if (value is DateTimeOffset) { Emit(2); }\n"
        "#endif\n"
        "        else { Emit(3); }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)
    for label in (cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value):
        names = {
            qn.rsplit(".", 1)[-1].split("(", 1)[0]
            for qn in get_node_names(mock_ingestor, label)
        }
        assert "if" not in names, f"{label} node named 'if' leaked from #if-split body"
