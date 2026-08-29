"""An explicit C++ template specialization is unreachable from calls (#1188).

Measured on `main` rather than argued. Given a primary template and an explicit
specialization, the graph contains BOTH classes and both methods, and the
specialization gets its own `INHERITS` edge -- but every call resolves to the
primary:

    CLASSES : Proc, Proc<int>
    INHERITS: Proc -> Base, Proc<int> -> Base
    CALLS   : use -> Proc.f            <- on a `Proc<int>` receiver

So `Proc<int>.f` is reachable structurally and never by a call. The cause is on
the CALL side, not in the node name: `CppTypeInference` maps a receiver to its
"bare C++ type name", stripping template arguments, because `_resolve_class_name`
takes bare names -- a contract documented in three places with 15 call sites
through it. `Proc<int>` and `Proc` therefore reduce to the same key.

A note for anyone tempted by the node name, since I tried it and was wrong.
`extract_cpp_class_name` returns `Proc<int>` verbatim because tree-sitter names
a specialization with a `template_type` node rather than a `type_identifier`,
and `_scope_segment_name` states that template arguments are text "no registry
class QN holds". Unwrapping it does NOT fix the orphan: the specialization then
collides with the primary's bare name and is disambiguated to `Proc@3`, which is
the same orphan with a less informative name, and it breaks
`test_cpp_inheritance_edge_cases`, which asserts the bracketed form. The
bracketed qn is at least self-describing.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

_SOURCE = (
    "template<typename T>\n"
    "struct Box { void put(T v) {} };\n"
    "template<>\n"
    "struct Box<char> { void put(char v) {} };\n"
    "void use() {\n"
    "    Box<double> bd; bd.put(1.0);\n"
    "    Box<char>   bc; bc.put('x');\n"
    "}\n"
)


def _write(project: Path) -> None:
    (project / "box.cpp").write_text(_SOURCE, encoding="utf-8")


def _put_targets(mock_ingestor: MagicMock) -> set[str]:
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    return {target for _caller, target in calls if target.endswith(".put")}


def test_a_call_on_a_specialized_receiver_is_attributed_to_the_primary(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Pins TODAY's behaviour so the limitation is visible rather than folklore.

    Deliberately asserts what the graph does, not what it should do. The
    companion xfail below asserts the intended behaviour, so the pair fails
    from one side or the other the moment dispatch changes.
    """
    _write(temp_repo)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    assert _put_targets(mock_ingestor) == {f"{temp_repo.name}.box.Box.put"}


@pytest.mark.xfail(
    reason="specialization dispatch needs the receiver type to keep its "
    "template arguments; the C++ type map is bare-name by contract and 15 "
    "call sites resolve through _resolve_class_name (issue #1188)",
    strict=True,
)
def test_a_call_on_a_specialized_receiver_should_reach_the_specialization(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two receivers of DIFFERENT types each call `put`; one definition cannot
    # be the right answer for both. strict=True so this fails loudly the day
    # dispatch works, rather than rotting into a silent pass.
    _write(temp_repo)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    assert len(_put_targets(mock_ingestor)) == 2
