from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp import CppTypeInferenceEngine
from codebase_rag.tests.conftest import get_relationships, run_updater


def _first_class_node(source: str):
    parsers, _ = load_parsers()
    tree = parsers["cpp"].parse(source.encode("utf-8"))

    def walk(node):
        if node.type in ("class_specifier", "struct_specifier"):
            return node
        for child in node.children:
            if (found := walk(child)) is not None:
                return found
        return None

    node = walk(tree.root_node)
    assert node is not None
    return node


def _calls_from(mock_ingestor: MagicMock, caller_suffix: str) -> set[str]:
    return {
        str(c.args[2][2])
        for c in get_relationships(mock_ingestor, "CALLS")
        if str(c.args[0][2]).endswith(caller_suffix)
    }


# (H) Member data declarations use `field_identifier`, not `identifier`, and a member
# (H) FUNCTION declaration (`void Lock();`) is also a field_declaration but with a
# (H) function_declarator -- only data members are fields. Pointer/qualified/template
# (H) types reduce to a bare type name the resolver can map to a class.
def test_cpp_build_field_type_map_captures_data_members_only() -> None:
    src = """
class DBImpl {
 public:
  void Lock();
  int Count(int x);
 private:
  port::Mutex mutex_;
  std::string buffer_;
  Foo* ptr_;
  int counter_, extra_;
};
"""
    fields = CppTypeInferenceEngine().build_field_type_map(_first_class_node(src))
    # (H) Only class-typed fields are recorded: primitive-typed members (`int
    # (H) counter_, extra_;`) can never be a method-call receiver, so they are omitted.
    assert fields == {
        "mutex_": "Mutex",
        "buffer_": "string",
        "ptr_": "Foo",
    }, fields
    assert "Lock" not in fields and "Count" not in fields


# (H) A first-party member field receiver: `mutex_.Lock()` must resolve to the field's
# (H) class method, not fall to a name-only guess. `Alpha` also defines `Lock` and sorts
# (H) before `Mutex`, so the name-only trie fallback deterministically picks the WRONG
# (H) `Alpha.Lock` -- only the field's type disambiguates. The method is defined INLINE
# (H) here, so field decls and the call are in the same AST.
_INLINE_SOURCE = """
namespace ns {

class Alpha {
 public:
  void Lock() {}
};

class Mutex {
 public:
  void Lock() {}
};

class DB {
 public:
  void Run() { mutex_.Lock(); }
 private:
  Mutex mutex_;
};

}  // namespace ns
"""


def test_cpp_inline_member_field_call_resolves_to_field_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cpp_field_inline"
    project.mkdir()
    (project / "s.cpp").write_text(_INLINE_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    callees = _calls_from(mock_ingestor, ".DB.Run")
    assert any(c.endswith(".Mutex.Lock") for c in callees), (
        f"mutex_.Lock() should resolve to Mutex.Lock via the field type, got {callees}"
    )
    assert not any(c.endswith(".Alpha.Lock") for c in callees), (
        f"mutex_.Lock() must not resolve to the same-named Alpha.Lock, got {callees}"
    )


# (H) The real leveldb shape: the class (with its fields) is declared in a header and
# (H) the method is defined OUT-OF-LINE in a .cc, so the field declarations live in a
# (H) different translation unit than the method body. Field types must therefore be
# (H) captured at class ingestion and looked up by the enclosing class qn.
# (H) `Alpha.Lock` (sorts before `Mutex.Lock`) and `Buf.clear` are same-named
# (H) first-party competitors: without field-type inference the name-only trie binds
# (H) mutex_.Lock() to Alpha.Lock and buffer_.clear() to Buf.clear.
_HEADER = """
namespace ns {

class Alpha {
 public:
  void Lock();
};

class Mutex {
 public:
  void Lock();
};

class Buf {
 public:
  void clear();
};

class DB {
 public:
  void Run();
 private:
  Mutex mutex_;
  std::string buffer_;
};

}  // namespace ns
"""

_IMPL = """
#include "db.h"

namespace ns {

void Mutex::Lock() {}

void DB::Run() {
  mutex_.Lock();
  buffer_.clear();
}

}  // namespace ns
"""


def test_cpp_out_of_line_member_field_call_resolves_cross_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cpp_field_xfile"
    project.mkdir()
    (project / "db.h").write_text(_HEADER, encoding="utf-8")
    (project / "db.cc").write_text(_IMPL, encoding="utf-8")

    run_updater(project, mock_ingestor)

    callees = _calls_from(mock_ingestor, ".DB.Run")
    # (H) FN fix: first-party Mutex field method resolves cross-file.
    assert any(c.endswith(".Mutex.Lock") for c in callees), (
        f"mutex_.Lock() should resolve to Mutex.Lock across files, got {callees}"
    )
    # (H) FP fix: buffer_ is a std::string (external), so buffer_.clear() must NOT be
    # (H) rebound to any first-party clear method.
    assert not any(c.endswith(".clear") for c in callees), (
        f"buffer_.clear() on a std::string field must not resolve first-party, "
        f"got {callees}"
    )
