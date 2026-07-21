# A `#if` directive that splits an if/else chain mid-method makes tree-sitter
# parse-recover the trailing `else if (...)` as a local_function_statement
# whose `name` field is the reserved keyword `if`. That is a parse artifact,
# not a real function; cgr must not emit a Function/Method node for it (it was
# polluting the graph, e.g. 184 bogus "if" nodes in Newtonsoft.Json).
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    get_node_names,
    get_relationships,
    run_updater,
)

SKIP = "c_sharp"


def _leaf_names(mock_ingestor: MagicMock, label: str) -> set[str]:
    return {
        qn.rsplit(".", 1)[-1].split("(", 1)[0]
        for qn in get_node_names(mock_ingestor, label)
    }


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
        names = _leaf_names(mock_ingestor, label)
        assert "if" not in names, f"{label} node named 'if' leaked from #if-split body"


# When a `#if` splits a statement deep inside a method, tree-sitter's error
# recovery can TRUNCATE the enclosing class_declaration node early; every
# member after the truncation point detaches into the namespace's
# declaration_list with no class ancestor. A C# `method_declaration` /
# `property_declaration` is grammatically only ever a type member (a real
# top-level function is a local_function_statement), so such a detached node
# is still a class method and must be emitted as a Method, never mislabelled a
# module-level Function (this was ~133 phantom Function FPs on Newtonsoft.Json,
# e.g. JsonTextReader.BlockCopyChars, a private static member). The body below
# is a reduced slice of the real JsonTextReader.ParseReadString derailment.
_ORPHAN_MEMBER_SRC = """\
namespace N
{
    public class Reader
    {
        private void ParseReadString(int readType)
        {
            switch (readType)
            {
                default:
                    if (_dateParseHandling != DateParseHandling.None)
                    {
                        DateParseHandling dateParseHandling;
                        if (readType == ReadType.ReadAsDateTime)
                        {
                            dateParseHandling = DateParseHandling.DateTime;
                        }
#if HAVE_DATE_TIME_OFFSET
                        else if (readType == ReadType.ReadAsDateTimeOffset)
                        {
                            dateParseHandling = DateParseHandling.DateTimeOffset;
                        }
#endif
                        else
                        {
                            dateParseHandling = _dateParseHandling;
                        }

                        if (dateParseHandling == DateParseHandling.DateTime)
                        {
                            if (TryParse(out DateTime dt))
                            {
                                SetToken(dt);
                                return;
                            }
                        }
#if HAVE_DATE_TIME_OFFSET
                        else
                        {
                            if (TryParseOffset(out DateTimeOffset dt))
                            {
                                SetToken(dt);
                                return;
                            }
                        }
#endif
                    }

                    SetToken(_stringReference.ToString());
                    break;
            }
        }

        private static void BlockCopyChars(char[] src, int srcOffset)
        {
            const int charByteCount = 2;
            Shift();
            Buffer.BlockCopy(src, srcOffset, charByteCount);
        }

        private static void Shift() { }

        public int Prop { get; set; }
    }
}
"""


def test_if_truncated_class_body_orphan_members_are_methods(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cs_orphan_members"
    project.mkdir()
    (project / "Reader.cs").write_text(_ORPHAN_MEMBER_SRC, encoding="utf-8")
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    func_leaves = _leaf_names(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    method_leaves = _leaf_names(mock_ingestor, cs.NodeLabel.METHOD.value)

    # The method and property that the truncation detached from the class node
    # must be Methods, not module Functions.
    for name in ("BlockCopyChars", "Prop"):
        assert name not in func_leaves, f"{name} leaked as a module Function"
        assert name in method_leaves, f"{name} missing as a Method"

    # They must attach to the recovered enclosing class, not orphan or vanish.
    method_qns = get_node_names(mock_ingestor, cs.NodeLabel.METHOD.value)
    assert any(".Reader.BlockCopyChars" in qn for qn in method_qns), method_qns
    defines_method_targets = {
        c.args[2][2] for c in get_relationships(mock_ingestor, "DEFINES_METHOD")
    }
    assert any(".Reader.BlockCopyChars" in qn for qn in defines_method_targets), (
        defines_method_targets
    )

    # A call inside the recovered method must be attributed to its Method node,
    # not a re-derived module-Function qn (which would source a dropped edge).
    # The recovery records function_locations so Pass 3 reuses the Method
    # identity; the graph audit already rejects any dangling CALLS endpoint.
    shift_calls = [
        c
        for c in get_relationships(mock_ingestor, "CALLS")
        if ".Reader.Shift" in c.args[2][2]
    ]
    assert shift_calls, "expected a CALLS edge into Reader.Shift"
    for c in shift_calls:
        caller_label, _, caller_qn = c.args[0]
        assert caller_label == cs.NodeLabel.METHOD.value, c.args[0]
        assert ".Reader.BlockCopyChars" in caller_qn, caller_qn


def test_directive_wrapped_default_interface_bodies_parse_clean(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An interface member with an `#if`-wrapped default body
    # (`void M() #if X => Impl() #endif ;`, Serilog's ILogger) shatters the
    # whole interface into an ERROR node: every member registers as a
    # module-level Function and the directive CONDITION itself becomes a
    # phantom node. Blanking conditional-directive lines on a broken parse
    # recovers the real structure.
    project = temp_repo / "cs_default_iface"
    project.mkdir()
    members = []
    for i in range(6):
        members.append(
            f'    [Obsolete("m{i}")]\n'
            f"    void Verbose{i}(string messageTemplate, params object?[]? args)\n"
            "#if FEATURE_DEFAULT_INTERFACE\n"
            f"        => Write((System.Exception?)null, messageTemplate)\n"
            "#endif\n"
            "    ;\n"
        )
    (project / "ILogger.cs").write_text(
        "using System;\n"
        "namespace N;\n"
        "public interface ILogger {\n"
        "    ILogger ForContext(string name);\n"
        + "".join(members)
        + "    void Write(Exception? ex, string messageTemplate);\n"
        "}\n",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    method_names = _leaf_names(mock_ingestor, "Method")
    function_names = _leaf_names(mock_ingestor, "Function")
    assert "Verbose0" in method_names, (method_names, function_names)
    assert "FEATURE_DEFAULT_INTERFACE" not in function_names, function_names
    assert "FEATURE_DEFAULT_INTERFACE" not in method_names, method_names
    assert "if" not in function_names, function_names
