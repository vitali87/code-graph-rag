# An out-of-line C++ member definition (`int Writer::AddRecord() {}` inside
# `namespace ns { namespace log {`) bound to the first registered class whose
# qn merely ENDED with `.Writer`: a class nested in another class
# (`ns::Impl::Writer`, private to db/impl.cc) took all of log_writer.cc's
# bodies, and renaming db/impl.* to db/zimpl.* moved them back, because
# log_writer.cc is parsed before its header and impl.cc before it (issue
# #3137). The class is now looked up as C++ does, from the definition's
# namespace outward, and anything less certain waits for the full registry.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp import out_of_line
from codebase_rag.parsers.cpp import utils as cpp_utils
from codebase_rag.tests.conftest import get_relationships, run_updater
from codebase_rag.types_defs import CppClassLookup

IMPL_H = """#pragma once

namespace ns {

class Impl {
 public:
  int Run();

 private:
  struct Writer;  // defined in impl.cc
};

}  // namespace ns
"""

IMPL_CC = """#include "db/{stem}.h"

#include "db/log_writer.h"

namespace ns {{

struct Impl::Writer {{
  explicit Writer(int n) : n(n) {{}}
  int n;
}};

int Impl::Run() {{
  Writer w(1);
  log::Writer lw(nullptr);
  return lw.AddRecord("x") + w.n;
}}

}}  // namespace ns
"""

LOG_WRITER_H = """#pragma once

namespace ns {
namespace log {

class Writer {
 public:
  explicit Writer(void* dest);
  int AddRecord(const char* s);

 private:
  int Emit();
};

}  // namespace log
}  // namespace ns
"""

LOG_WRITER_CC = """#include "db/log_writer.h"

namespace ns {
namespace log {

Writer::Writer(void* dest) {}

int Writer::AddRecord(const char* s) { return Emit(); }

int Writer::Emit() { return 0; }

}  // namespace log
}  // namespace ns
"""

MAIN_CC = """#include "db/{stem}.h"

int main() {{ return ns::Impl().Run(); }}
"""

SKIPLIST_H = """#pragma once

namespace leveldb {

template <typename Key, class Comparator>
class SkipList {
 public:
  class Iterator {
   public:
    explicit Iterator(const SkipList* list);
    bool Valid() const;

   private:
    const SkipList* list_;
  };
};

template <typename Key, class Comparator>
inline SkipList<Key, Comparator>::Iterator::Iterator(const SkipList* list) {
  list_ = list;
}

template <typename Key, class Comparator>
inline bool SkipList<Key, Comparator>::Iterator::Valid() const {
  return list_ != nullptr;
}

}  // namespace leveldb
"""

ITERATOR_CC = """namespace leveldb {

Iterator::Iterator() { cleanup_ = 0; }

Iterator::~Iterator() {}

}  // namespace leveldb
"""


class _Graph:
    def __init__(self, mock_ingestor: MagicMock) -> None:
        self.methods: dict[tuple[str, int], str] = {}
        self.class_paths: dict[str, str] = {}
        for call in mock_ingestor.ensure_node_batch.call_args_list:
            label, props = str(call.args[0]), call.args[1]
            qn, path = props.get(cs.KEY_QUALIFIED_NAME), props.get(cs.KEY_PATH)
            if qn is None or path is None:
                continue
            if label == cs.NodeLabel.METHOD.value:
                self.methods[(path, props[cs.KEY_START_LINE])] = qn
            elif label == cs.NodeLabel.CLASS.value:
                self.class_paths[qn] = path
        self.owners = {
            str(call.args[2][2]): str(call.args[0][2])
            for call in get_relationships(
                mock_ingestor, cs.RelationshipType.DEFINES_METHOD.value
            )
        }
        self.calls = {
            (str(call.args[0][2]), str(call.args[2][2]))
            for call in get_relationships(
                mock_ingestor, cs.RelationshipType.CALLS.value
            )
        }

    def owner_path(self, path: str, line: int) -> str | None:
        owner = self.owners.get(self.methods[(path, line)])
        return None if owner is None else self.class_paths.get(owner)


def _write_repro(repo: Path, stem: str) -> None:
    (repo / "db").mkdir()
    (repo / "db" / f"{stem}.h").write_text(IMPL_H)
    (repo / "db" / f"{stem}.cc").write_text(IMPL_CC.format(stem=stem))
    (repo / "db" / "log_writer.h").write_text(LOG_WRITER_H)
    (repo / "db" / "log_writer.cc").write_text(LOG_WRITER_CC)
    (repo / "main.cc").write_text(MAIN_CC.format(stem=stem))


# `impl` sorts before `log_writer`, so Impl::Writer is registered by the time
# log_writer.cc is parsed; `zimpl` sorts after it. Both must give one graph.
@pytest.mark.parametrize("stem", ["impl", "zimpl"])
def test_log_writer_bodies_join_log_writer_whatever_the_file_names(
    temp_repo: Path, mock_ingestor: MagicMock, stem: str
) -> None:
    _write_repro(temp_repo, stem)
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    for line in (6, 8, 10):
        assert graph.owner_path("db/log_writer.cc", line) == "db/log_writer.h", (
            line,
            graph.owners,
        )
    # Impl::Writer's own inline constructor keeps a node of its own instead
    # of sharing a qn with log::Writer's out-of-line one.
    assert graph.owner_path(f"db/{stem}.cc", 8) == f"db/{stem}.cc"
    assert graph.methods[(f"db/{stem}.cc", 8)] != graph.methods[("db/log_writer.cc", 6)]


@pytest.mark.parametrize("stem", ["impl", "zimpl"])
def test_a_call_on_a_log_writer_reaches_log_writer(
    temp_repo: Path, mock_ingestor: MagicMock, stem: str
) -> None:
    _write_repro(temp_repo, stem)
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    run_qn = graph.methods[(f"db/{stem}.cc", 12)]
    add_record_qn = graph.methods[("db/log_writer.cc", 8)]
    emit_qn = graph.methods[("db/log_writer.cc", 10)]
    assert graph.owners[add_record_qn].endswith(".ns.log.Writer"), graph.owners
    assert (run_qn, add_record_qn) in graph.calls, graph.calls
    assert (add_record_qn, emit_qn) in graph.calls, graph.calls
    assert not any("Impl::Writer.AddRecord" in callee for _, callee in graph.calls)


def test_a_bare_qualifier_never_joins_a_nested_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # No top-level `Iterator` exists, only SkipList's nested one: the
    # namespace-scope `Iterator::Iterator()` names a class this repo does not
    # declare (leveldb's is an export-macro class, #2840), so it stays
    # unattached rather than joining SkipList::Iterator.
    (temp_repo / "db").mkdir()
    (temp_repo / "table").mkdir()
    (temp_repo / "db" / "skiplist.h").write_text(SKIPLIST_H)
    (temp_repo / "table" / "iterator.cc").write_text(ITERATOR_CC)
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    for line in (3, 5):
        qn = graph.methods[("table/iterator.cc", line)]
        assert qn not in graph.owners, (qn, graph.owners)
    module_children = {
        str(call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.DEFINES.value)
        if str(call.args[0][0]) == cs.NodeLabel.MODULE.value
    }
    assert graph.methods[("table/iterator.cc", 3)] in module_children


def test_a_qualifier_through_a_template_still_reaches_its_nested_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `SkipList<Key, Comparator>::Iterator::Valid` names the nested class
    # through its template; reading it as a bare `Iterator::` would orphan it.
    (temp_repo / "db").mkdir()
    (temp_repo / "db" / "skiplist.h").write_text(SKIPLIST_H)
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    for line in (18, 19, 23, 24):
        qn = graph.methods[("db/skiplist.h", line)]
        assert graph.owners.get(qn, "").endswith(".SkipList.Iterator"), (
            qn,
            graph.owners,
        )


def test_a_class_private_to_another_source_file_is_no_candidate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # leveldb's util/comparator.cc defines leveldb::Comparator's destructor;
    # db/skiplist_test.cc declares a test-only `leveldb::Comparator` of its
    # own, at the same path but in another translation unit, where no other
    # file can define its members.
    (temp_repo / "include").mkdir()
    (temp_repo / "util").mkdir()
    (temp_repo / "db").mkdir()
    (temp_repo / "include" / "comparator.h").write_text(
        "#pragma once\nnamespace leveldb {\nclass Comparator {\n public:\n"
        "  virtual ~Comparator();\n  virtual int Compare(int a, int b) const = 0;\n"
        "};\n}  // namespace leveldb\n"
    )
    (temp_repo / "util" / "comparator.cc").write_text(
        '#include "include/comparator.h"\nnamespace leveldb {\n'
        "Comparator::~Comparator() = default;\n}  // namespace leveldb\n"
    )
    (temp_repo / "db" / "skiplist_test.cc").write_text(
        "namespace leveldb {\nstruct Comparator {\n"
        "  int operator()(int a, int b) const { return a - b; }\n"
        "};\n}  // namespace leveldb\n"
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    assert graph.owner_path("util/comparator.cc", 3) == "include/comparator.h"


def test_an_alias_in_scope_reaches_the_nested_class_it_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The header sorts first, so the class is registered when the definition
    # is parsed and binds at once; `Inner` is the alias, not a bare lookup.
    (temp_repo / "a").mkdir()
    (temp_repo / "b").mkdir()
    (temp_repo / "a" / "outer.h").write_text(
        "#pragma once\nnamespace lib {\nclass Outer {\n public:\n  class Inner;\n};\n"
        "class Outer::Inner {\n public:\n  bool run();\n};\n}  // namespace lib\n"
    )
    (temp_repo / "b" / "inner.cc").write_text(
        '#include "a/outer.h"\nnamespace lib {\nusing Inner = Outer::Inner;\n'
        "bool Inner::run() { return true; }\n}  // namespace lib\n"
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    qn = graph.methods[("b/inner.cc", 4)]
    assert graph.owners.get(qn, "").endswith("Inner"), (qn, graph.owners)
    assert graph.owner_path("b/inner.cc", 4) == "a/outer.h"


def test_a_using_directive_still_reaches_a_namespace_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "a").mkdir()
    (temp_repo / "b").mkdir()
    (temp_repo / "a" / "widget.h").write_text(
        "#pragma once\nnamespace lib {\nclass Widget {\n public:\n  void draw();\n"
        "};\n}  // namespace lib\n"
    )
    (temp_repo / "b" / "widget.cc").write_text(
        '#include "a/widget.h"\nusing namespace lib;\nvoid Widget::draw() {}\n'
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    assert graph.owner_path("b/widget.cc", 3) == "a/widget.h"


def test_a_written_template_qualifier_beats_a_top_level_same_leaf_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # leveldb declares both `leveldb::Iterator` and `SkipList<K, C>::Iterator`;
    # the out-of-line `SkipList<Key, Comparator>::Iterator::Valid` belongs to
    # the nested one whichever is registered first.
    (temp_repo / "db").mkdir()
    (temp_repo / "include").mkdir()
    (temp_repo / "db" / "skiplist.h").write_text(SKIPLIST_H)
    (temp_repo / "include" / "iterator.h").write_text(
        "#pragma once\nnamespace leveldb {\nclass Iterator {\n public:\n"
        "  Iterator();\n  bool Valid() const;\n};\n}  // namespace leveldb\n"
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    for line in (18, 19, 23, 24):
        qn = graph.methods[("db/skiplist.h", line)]
        assert graph.owners.get(qn, "").endswith(".SkipList.Iterator"), (
            qn,
            graph.owners,
        )


def test_a_destructor_of_a_nested_class_joins_that_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "reader.h").write_text(
        "#pragma once\nnamespace log {\nclass Reader {\n public:\n"
        "  class Reporter {\n   public:\n    virtual ~Reporter();\n  };\n};\n"
        "}  // namespace log\n"
    )
    (temp_repo / "reader.cc").write_text(
        '#include "reader.h"\nnamespace log {\n'
        "Reader::Reporter::~Reporter() = default;\n}  // namespace log\n"
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    qn = graph.methods[("reader.cc", 3)]
    assert graph.owners.get(qn, "").endswith(".log.Reader.Reporter"), (
        qn,
        graph.owners,
    )


def test_a_namespace_prefixed_fallback_never_joins_a_nested_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # No `log::Writer` is declared anywhere. `Outer::log::Writer` ends with
    # the `log.Writer` the fallback also tries, but `Writer::f` inside
    # `namespace log` does not name a class nested in `Outer`.
    (temp_repo / "outer.h").write_text(
        "#pragma once\nclass Outer {\n public:\n  class log {\n   public:\n"
        "    class Writer {\n     public:\n      void f();\n    };\n  };\n};\n"
    )
    (temp_repo / "writer.cc").write_text(
        "namespace log {\nvoid Writer::f() {}\n}  // namespace log\n"
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    qn = graph.methods[("writer.cc", 2)]
    assert qn not in graph.owners, (qn, graph.owners)


def test_an_alias_in_an_earlier_block_of_the_namespace_still_applies(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "a").mkdir()
    (temp_repo / "b").mkdir()
    (temp_repo / "a" / "outer.h").write_text(
        "#pragma once\nnamespace lib {\nclass Outer {\n public:\n"
        "  class Inner {\n   public:\n    bool run();\n  };\n};\n"
        "}  // namespace lib\n"
    )
    (temp_repo / "b" / "inner.cc").write_text(
        '#include "a/outer.h"\nnamespace lib {\nusing In = Outer::Inner;\n}\n'
        "namespace lib {\nbool In::run() { return true; }\n}  // namespace lib\n"
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    assert graph.owner_path("b/inner.cc", 6) == "a/outer.h", graph.owners


# `a_inner.h` sorts before b.cc, so lib::Writer is registered when the body
# is parsed; `z_inner.h` sorts after it, while the global ::Writer in
# a_outer.h is registered either way. Both must give lib::Writer.
@pytest.mark.parametrize("inner", ["a_inner", "z_inner"])
def test_an_outer_namespace_class_never_takes_a_body_early(
    temp_repo: Path, mock_ingestor: MagicMock, inner: str
) -> None:
    (temp_repo / "a_outer.h").write_text(
        "#pragma once\nclass Writer {\n public:\n  void run();\n};\n"
    )
    (temp_repo / f"{inner}.h").write_text(
        "#pragma once\nnamespace lib {\nclass Writer {\n public:\n  void run();\n"
        "};\n}  // namespace lib\n"
    )
    (temp_repo / "b.cc").write_text(
        f'#include "a_outer.h"\n#include "{inner}.h"\nnamespace lib {{\n'
        "void Writer::run() {}\n}  // namespace lib\n"
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    assert graph.owner_path("b.cc", 4) == f"{inner}.h", graph.owners


def test_a_nearer_class_hides_an_outer_alias(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `X` inside a::b is the class declared there, not `a`'s alias of lib::X.
    (temp_repo / "include").mkdir()
    (temp_repo / "src").mkdir()
    (temp_repo / "include" / "lib.h").write_text(
        "#pragma once\nnamespace lib {\nclass X {\n public:\n  void f();\n};\n"
        "}  // namespace lib\n"
    )
    (temp_repo / "src" / "shadow.cc").write_text(
        '#include "include/lib.h"\nnamespace a {\nusing X = lib::X;\n'
        "namespace b {\nclass X {\n  void f();\n};\nvoid X::f() {}\n"
        "}  // namespace b\n}  // namespace a\n"
    )
    run_updater(temp_repo, mock_ingestor)
    graph = _Graph(mock_ingestor)

    qn = graph.methods[("src/shadow.cc", 8)]
    assert graph.owners.get(qn, "").endswith(".a.b.X"), (qn, graph.owners)
    assert graph.owner_path("src/shadow.cc", 8) == "src/shadow.cc"


@pytest.fixture(scope="module")
def cpp_parser():
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    return parsers[cs.SupportedLanguage.CPP]


def _lookup_of_last_definition(cpp_parser, code: str) -> CppClassLookup:
    pending = [cpp_parser.parse(code.encode()).root_node]
    found = []
    while pending:
        node = pending.pop()
        if cpp_utils.is_out_of_class_method_definition(node):
            found.append(node)
            continue
        pending.extend(node.children)
    definition = max(found, key=lambda node: node.start_byte)
    class_name = cpp_utils.extract_class_name_from_out_of_class_method(definition)
    assert class_name is not None
    return out_of_line.class_lookup(definition, class_name)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(
            "namespace ns { namespace log { int Writer::Add() { return 0; } } }",
            CppClassLookup("Writer", "ns.log"),
            id="plain",
        ),
        pytest.param(
            "namespace lib { using In = Outer::Inner; bool In::run() { return 1; } }",
            CppClassLookup("Outer.Inner", "lib"),
            id="alias-declaration",
        ),
        pytest.param(
            "namespace lib { typedef Outer::Inner In; bool In::run() { return 1; } }",
            CppClassLookup("Outer.Inner", "lib"),
            id="typedef",
        ),
        pytest.param(
            "using other::Widget;\nvoid Widget::draw() {}",
            CppClassLookup("other.Widget", ""),
            id="using-declaration",
        ),
        pytest.param(
            "using namespace other;\nvoid Widget::draw() {}",
            CppClassLookup("Widget", ""),
            id="using-directive-declares-no-name",
        ),
        pytest.param(
            "namespace lib { bool In::run() { return 1; } using In = Outer::Inner; }",
            CppClassLookup("In", "lib"),
            id="alias-declared-after",
        ),
        pytest.param(
            "namespace b { using D = x::C; }\nnamespace a { void D::m() {} }",
            CppClassLookup("D", "a"),
            id="alias-in-another-namespace-is-out-of-scope",
        ),
        pytest.param(
            "namespace lib { using In = Outer::Inner; }\n"
            "namespace lib { bool In::run() { return 1; } }",
            CppClassLookup("Outer.Inner", "lib"),
            id="alias-in-an-earlier-block-of-the-namespace",
        ),
        pytest.param(
            "namespace a { namespace lib { using In = O::I; } }\n"
            "namespace a::lib { bool In::run() { return 1; } }",
            CppClassLookup("O.I", "a.lib"),
            id="alias-in-an-earlier-nested-block",
        ),
        pytest.param(
            "void g() { using In = O::I; }\nbool In::run() { return 1; }",
            CppClassLookup("In", ""),
            id="alias-local-to-a-function-is-out-of-scope",
        ),
        pytest.param(
            "namespace a { using B = x::C; using D = B; void D::m() {} }",
            CppClassLookup("x.C", "a"),
            id="alias-of-alias",
        ),
        pytest.param(
            "namespace a { using X = lib::X;\n"
            "namespace b { class X { void f(); }; void X::f() {} } }",
            CppClassLookup("X", "a.b"),
            id="nearer-class-hides-alias",
        ),
        pytest.param(
            "using W = other::W;\nnamespace n { struct W; void W::f() {} }",
            CppClassLookup("W", "n"),
            id="nearer-forward-declaration-hides-alias",
        ),
        pytest.param(
            "namespace a { using X = lib::X;\n"
            "namespace b { namespace X { struct Y { void f(); }; }\n"
            "void X::Y::f() {} } }",
            CppClassLookup("X.Y", "a.b"),
            id="nearer-namespace-hides-alias",
        ),
        pytest.param(
            "namespace a { using X = lib::X;\n"
            "namespace b { template <class T> struct X; void X::f() {} } }",
            CppClassLookup("X", "a.b"),
            id="nearer-template-hides-alias",
        ),
        pytest.param(
            "namespace x { void ::lib::Widget::draw() {} }",
            CppClassLookup("lib.Widget", ""),
            id="anchored",
        ),
        pytest.param(
            "namespace leveldb {\ntemplate <typename K, class C>\n"
            "bool SkipList<K, C>::Iterator::Valid() const { return true; }\n}",
            CppClassLookup("SkipList.Iterator", "leveldb"),
            id="template-scope-kept",
        ),
        pytest.param(
            "namespace leveldb { namespace log {\n"
            "Reader::Reporter::~Reporter() = default;\n} }",
            CppClassLookup("Reader.Reporter", "leveldb.log"),
            id="nested-destructor-keeps-its-class",
        ),
        pytest.param(
            "namespace re2 { bool PCRE::FullMatchFunctor::operator()(int a) const "
            "{ return a; } }",
            CppClassLookup("PCRE.FullMatchFunctor", "re2"),
            id="nested-operator-keeps-its-class",
        ),
    ],
)
def test_class_lookup_reads_scope_and_aliases(
    cpp_parser, code: str, expected: CppClassLookup
) -> None:
    assert _lookup_of_last_definition(cpp_parser, code) == expected


def test_scope_candidates_walk_out_to_the_global_namespace() -> None:
    assert out_of_line.scope_candidates(CppClassLookup("W", "ns.log")) == [
        "ns.log.W",
        "ns.W",
        "W",
    ]
