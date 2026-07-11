# (H) nlohmann's binary_reader.hpp (3,125 lines) collapsed into ONE whole-file
# (H) ERROR node: a preprocessor branch opens a brace that a LATER branch
# (H) closes (`#ifdef __cpp_lib_byteswap ... else { #endif` ... `#ifdef
# (H) __cpp_lib_byteswap } #endif`), and tree-sitter -- which keeps EVERY
# (H) branch's tokens -- sees unbalanced braces. Query recovery inside the
# (H) ERROR still surfaced most method NAMES but lost the class structure
# (H) (methods registered as free functions, five definitions dropped
# (H) entirely), orphaning the 41-node binary_reader dead-code cluster.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import (
    get_nodes,
    get_qualified_names,
    run_updater,
)

_HEAD = """\
namespace detail
{

template<typename BasicJsonType>
class binary_reader
{
  public:
    bool parse_bson_internal()
    {
        std::int32_t document_size{};
        get_number<std::int32_t, true>(input_format_t::bson, document_size);
        return read_one();
    }

    template<class NumberType>
    static void byte_swap(NumberType& number)
    {
        constexpr std::size_t sz = sizeof(number);
#ifdef __cpp_lib_byteswap
        if constexpr (sz == 1)
        {
            return;
        }
        else
        {
#endif
            auto* ptr = reinterpret_cast<std::uint8_t*>(&number);
            for (std::size_t i = 0; i < sz / 2; ++i)
            {
                std::swap(ptr[i], ptr[sz - i - 1]);
            }
#ifdef __cpp_lib_byteswap
        }
#endif
    }
"""

# (H) the collapse needs parse mass after the unbalanced branch; a handful of
# (H) switch-bearing template methods reproduces the real file's behaviour
_BULK_METHOD = """
    template<typename NumberType>
    bool helper_{i}(const input_format_t format, NumberType& result)
    {{
        if (format == input_format_t::bson)
        {{
            return read_one();
        }}
        switch (result)
        {{
            case 0x00:
                return helper_call(static_cast<int>(result), "x");
            default:
                return false;
        }}
    }}
"""

_TAIL = """
    bool read_one()
    {
        return true;
    }

  private:
    const decltype(JSON_MAKE_MARKERS_) type_markers = JSON_MAKE_MARKERS_;
};

}  // namespace detail
"""


def _write(root: Path) -> None:
    root.mkdir()
    bulk = "".join(_BULK_METHOD.format(i=i) for i in range(10))
    (root / "reader.hpp").write_text(_HEAD + bulk + _TAIL, encoding="utf-8")


def test_conditional_brace_file_keeps_class_structure(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "brproj"
    _write(root)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    methods = get_qualified_names(get_nodes(mock_ingestor, "Method"))
    # (H) every method after the unbalanced branch must stay a MEMBER of
    # (H) binary_reader, not degrade to a module-level free function
    assert "brproj.reader.detail.binary_reader.helper_0" in methods, sorted(methods)
    assert "brproj.reader.detail.binary_reader.helper_9" in methods, sorted(methods)
    assert "brproj.reader.detail.binary_reader.read_one" in methods, sorted(methods)

    functions = get_qualified_names(get_nodes(mock_ingestor, "Function"))
    assert "brproj.reader.helper_0" not in functions, sorted(functions)

    # (H) `const decltype(SOME_MACRO_) field = ...` is a FIELD whose type uses
    # (H) an unexpandable macro; the declarator must not register a phantom
    # (H) method named after the reserved keyword
    assert not any(q.endswith(".decltype") for q in methods), sorted(methods)


def test_conditional_brace_file_keeps_call_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "brcalls"
    _write(root)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    calls = {
        (c.args[0][2], c.args[2][2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "CALLS"
    }
    assert (
        "brcalls.reader.detail.binary_reader.parse_bson_internal",
        "brcalls.reader.detail.binary_reader.read_one",
    ) in calls, sorted(calls)
    assert (
        "brcalls.reader.detail.binary_reader.helper_3",
        "brcalls.reader.detail.binary_reader.read_one",
    ) in calls, sorted(calls)
