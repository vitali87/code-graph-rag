# A Python class followed by a same-named `def` in a conditional block (a
# Sphinx/docs shim, networkx's `_dispatchable`) must follow the documented
# duplicate rule across labels: the first definition keeps the plain qualified
# name, the later one takes `@<start_line>`. Functions used to register before
# classes, so the shim took `m.Tool`, the class became `m.Tool@4`, and every
# construction bound to the shim alone (issue #2621).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import PropertyValue

# The issue's reproduction, verbatim: the class is on line 4, the shim on 12.
ISSUE_SRC = """import os


class Tool:
    def run(self):
        return 1


if os.environ.get("DOCS"):
    _orig = Tool

    def Tool(*a, **k):  # sphinx-friendly shim
        return _orig(*a, **k)


def use():
    return Tool().run()
"""

INIT_SRC = """import os


class Tool:
    def __init__(self):
        self.ready = True

    def run(self):
        return 1


if os.environ.get("DOCS"):

    def Tool(*a, **k):
        return 1


def use():
    tool = Tool()
    return tool.run()
"""

LOCAL_SRC = """import os


def outer():
    class Tool:
        def run(self):
            return 1

    if os.environ.get("DOCS"):

        def Tool(*a, **k):
            return 1

    return Tool
"""

COND_DEF_FIRST_SRC = """import os


if os.environ.get("DOCS"):

    def Tool(*a, **k):
        return 1


class Tool:
    def run(self):
        return 1


def use():
    return Tool().run()
"""

NESTED_SRC = """class Outer:
    class Tool:
        def run(self):
            return 1

    def Tool(self):
        return 1
"""

# networkx's shape: `_dispatchable` is a class, rebound to a docs-only shim,
# and applied as a bare decorator.
DECORATOR_SRC = """import os


class dispatch:
    def __init__(self, func):
        self.func = func


if os.environ.get("DOCS"):

    def dispatch(func):
        return func


@dispatch
def algo():
    return 1
"""

CLASS_DECORATOR_SRC = """class register:
    def __init__(self, func):
        self.func = func


@register
def algo():
    return 1
"""

PLAIN_SRC = """class Tool:
    def run(self):
        return 1


def use():
    return Tool().run()
"""

DEF_FIRST_SRC = """import os


def Tool(*a, **k):
    return 1


if os.environ.get("X"):

    class Tool:
        def run(self):
            return 1


def use():
    return Tool().run()


alias = Tool
"""

IF_ELSE_SRC = """import os


if os.environ.get("FLAG"):

    def impl():
        return "real"

else:

    def impl():
        return "stub"


def caller():
    return impl()
"""

TRY_IMPORT_SRC = """try:
    from _speedups import Tool
except ImportError:

    class Tool:
        def run(self):
            return 1


def use():
    return Tool().run()
"""

TRY_DEF_CLASS_SRC = """try:
    from _speedups import fast

    def Tool(*a):
        return fast(*a)

except ImportError:

    class Tool:
        def run(self):
            return 1


def use():
    return Tool().run()
"""

# The PR #2789 review case: the unconditional def written after the class is
# what every caller runs, and it returns a different class.
FACTORY_SRC = """class Product:
    def product_method(self):
        return 1


class factory:
    def product_method(self):
        return 2


def factory():
    return Product()


def use():
    return factory().product_method()


def use_var():
    v = factory()
    return v.product_method()
"""

# Either binding may be live, and they construct different classes.
DISAGREE_SRC = """import os


class Other:
    def run(self):
        return 2


class Tool:
    def run(self):
        return 1


if os.environ.get("DOCS"):

    def Tool():
        return Other()


def use():
    return Tool().run()


def use_var():
    tool = Tool()
    return tool.run()
"""

_Edge = tuple[str, str, str, PropertyValue]


def _index(repo: Path, mock: MagicMock, src: str) -> str:
    (repo / "m.py").write_text(src)
    create_and_run_updater(repo, mock)
    return f"{repo.name}.m"


def _defs(mock: MagicMock, name: str) -> set[tuple[str, str, PropertyValue]]:
    return {
        (
            str(c.args[0]),
            str(c.args[1][cs.KEY_QUALIFIED_NAME]),
            c.args[1].get(cs.KEY_START_LINE),
        )
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0])
        in (cs.NodeLabel.CLASS, cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        and c.args[1].get(cs.KEY_NAME) == name
    }


def _edges_from(mock: MagicMock, caller_qn: str) -> set[_Edge]:
    out: set[_Edge] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        rel = str(c.args[1])
        if rel not in (cs.RelationshipType.CALLS, cs.RelationshipType.INSTANTIATES):
            continue
        if c.args[0][2] != caller_qn:
            continue
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        out.add(
            (
                rel,
                str(c.args[2][0]),
                str(c.args[2][2]),
                (props or {}).get(cs.KEY_RESOLUTION),
            )
        )
    return out


def _references_from(mock: MagicMock, source_qn: str) -> set[tuple[str, str]]:
    return {
        (str(c.args[2][0]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.REFERENCES
        and c.args[0][2] == source_qn
    }


class TestClassBeforeSameNamedDef:
    def test_class_keeps_plain_name_and_later_def_is_suffixed(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, ISSUE_SRC)
        assert _defs(mock_ingestor, "Tool") == {
            (cs.NodeLabel.CLASS, f"{m}.Tool", 4),
            (cs.NodeLabel.FUNCTION, f"{m}.Tool{cs.DUP_QN_MARKER}12", 12),
        }
        assert _defs(mock_ingestor, "run") == {
            (cs.NodeLabel.METHOD, f"{m}.Tool.run", 5)
        }

    def test_construction_instantiates_class_and_calls_shim(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, ISSUE_SRC)
        edges = _edges_from(mock_ingestor, f"{m}.use")
        overload = cs.EdgeResolution.OVERLOAD
        assert (
            cs.RelationshipType.INSTANTIATES,
            cs.NodeLabel.CLASS,
            f"{m}.Tool",
            overload,
        ) in edges
        assert (
            cs.RelationshipType.CALLS,
            cs.NodeLabel.FUNCTION,
            f"{m}.Tool{cs.DUP_QN_MARKER}12",
            overload,
        ) in edges

    def test_value_reference_passes_on_the_shim(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `_orig = Tool` hands the name on as a value: the __init__-less
        # class has no callable target, the shim does, and nothing may name
        # the class under the shim's label (a dangling edge).
        m = _index(temp_repo, mock_ingestor, ISSUE_SRC)
        assert _references_from(mock_ingestor, m) == {
            (cs.NodeLabel.FUNCTION, f"{m}.Tool{cs.DUP_QN_MARKER}12")
        }

    def test_method_on_constructed_instance_resolves_through_class(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, ISSUE_SRC)
        callees = {
            qn
            for rel, label, qn, _ in _edges_from(mock_ingestor, f"{m}.use")
            if rel == cs.RelationshipType.CALLS and label == cs.NodeLabel.METHOD
        }
        assert callees == {f"{m}.Tool.run"}

    def test_init_and_assigned_instance_bind_to_class(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, INIT_SRC)
        callees = {
            (rel, qn) for rel, _, qn, _ in _edges_from(mock_ingestor, f"{m}.use")
        }
        assert callees == {
            (cs.RelationshipType.INSTANTIATES, f"{m}.Tool"),
            (cs.RelationshipType.CALLS, f"{m}.Tool.__init__"),
            (cs.RelationshipType.CALLS, f"{m}.Tool{cs.DUP_QN_MARKER}14"),
            (cs.RelationshipType.CALLS, f"{m}.Tool.run"),
        }

    def test_function_local_class_keeps_plain_name(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, LOCAL_SRC)
        assert _defs(mock_ingestor, "Tool") == {
            (cs.NodeLabel.CLASS, f"{m}.outer.Tool", 5),
            (cs.NodeLabel.FUNCTION, f"{m}.outer.Tool{cs.DUP_QN_MARKER}11", 11),
        }

    def test_nested_class_keeps_plain_name_over_later_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Methods register with their class, before a nested class written
        # above them.
        m = _index(temp_repo, mock_ingestor, NESTED_SRC)
        assert _defs(mock_ingestor, "Tool") == {
            (cs.NodeLabel.CLASS, f"{m}.Outer.Tool", 2),
            (cs.NodeLabel.METHOD, f"{m}.Outer.Tool{cs.DUP_QN_MARKER}6", 6),
        }

    def test_bare_decorator_instantiates_class_and_calls_shim(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `@dispatch` runs `dispatch(algo)` at import, so the module binds it
        # as it binds that call.
        m = _index(temp_repo, mock_ingestor, DECORATOR_SRC)
        overload = cs.EdgeResolution.OVERLOAD
        assert _edges_from(mock_ingestor, m) == {
            (
                cs.RelationshipType.INSTANTIATES,
                cs.NodeLabel.CLASS,
                f"{m}.dispatch",
                overload,
            ),
            (
                cs.RelationshipType.CALLS,
                cs.NodeLabel.METHOD,
                f"{m}.dispatch.__init__",
                overload,
            ),
            (
                cs.RelationshipType.CALLS,
                cs.NodeLabel.FUNCTION,
                f"{m}.dispatch{cs.DUP_QN_MARKER}11",
                overload,
            ),
        }

    def test_bare_class_decorator_without_twin_does_not_fan_out(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Applying a lone class still constructs it, once and exactly.
        m = _index(temp_repo, mock_ingestor, CLASS_DECORATOR_SRC)
        exact = cs.EdgeResolution.EXACT
        assert _edges_from(mock_ingestor, m) == {
            (
                cs.RelationshipType.INSTANTIATES,
                cs.NodeLabel.CLASS,
                f"{m}.register",
                exact,
            ),
            (
                cs.RelationshipType.CALLS,
                cs.NodeLabel.METHOD,
                f"{m}.register.__init__",
                exact,
            ),
        }


class TestDefBeforeSameNamedClass:
    # The def is written first, so the documented rule already gave it the
    # plain name and that stays; the call gains the class as a candidate.

    def test_def_keeps_plain_name_and_call_fans_out_to_class(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, DEF_FIRST_SRC)
        assert _defs(mock_ingestor, "Tool") == {
            (cs.NodeLabel.FUNCTION, f"{m}.Tool", 4),
            (cs.NodeLabel.CLASS, f"{m}.Tool{cs.DUP_QN_MARKER}10", 10),
        }
        overload = cs.EdgeResolution.OVERLOAD
        assert _edges_from(mock_ingestor, f"{m}.use") == {
            (cs.RelationshipType.CALLS, cs.NodeLabel.FUNCTION, f"{m}.Tool", overload),
            (
                cs.RelationshipType.INSTANTIATES,
                cs.NodeLabel.CLASS,
                f"{m}.Tool{cs.DUP_QN_MARKER}10",
                overload,
            ),
            (
                cs.RelationshipType.CALLS,
                cs.NodeLabel.METHOD,
                f"{m}.Tool{cs.DUP_QN_MARKER}10.run",
                cs.EdgeResolution.EXACT,
            ),
        }
        # `alias = Tool` passes the name on as a value: the def takes the
        # REFERENCES edge, and the __init__-less class twin none. Written
        # under the def's label it named no node (a dangling edge).
        assert _references_from(mock_ingestor, m) == {
            (cs.NodeLabel.FUNCTION, f"{m}.Tool")
        }

    def test_conditional_def_above_class_types_through_class(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The shim is written first, so it keeps the plain name, but the
        # unconditional class is what `Tool` is bound to after import.
        m = _index(temp_repo, mock_ingestor, COND_DEF_FIRST_SRC)
        assert _defs(mock_ingestor, "Tool") == {
            (cs.NodeLabel.FUNCTION, f"{m}.Tool", 6),
            (cs.NodeLabel.CLASS, f"{m}.Tool{cs.DUP_QN_MARKER}10", 10),
        }
        callees = {
            (rel, qn) for rel, _, qn, _ in _edges_from(mock_ingestor, f"{m}.use")
        }
        assert callees == {
            (cs.RelationshipType.CALLS, f"{m}.Tool"),
            (cs.RelationshipType.INSTANTIATES, f"{m}.Tool{cs.DUP_QN_MARKER}10"),
            (cs.RelationshipType.CALLS, f"{m}.Tool{cs.DUP_QN_MARKER}10.run"),
        }

    def test_try_def_then_except_class_fans_out(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, TRY_DEF_CLASS_SRC)
        assert _defs(mock_ingestor, "Tool") == {
            (cs.NodeLabel.FUNCTION, f"{m}.Tool", 4),
            (cs.NodeLabel.CLASS, f"{m}.Tool{cs.DUP_QN_MARKER}9", 9),
        }
        callees = {
            (rel, qn) for rel, _, qn, _ in _edges_from(mock_ingestor, f"{m}.use")
        }
        assert callees == {
            (cs.RelationshipType.CALLS, f"{m}.Tool"),
            (cs.RelationshipType.INSTANTIATES, f"{m}.Tool{cs.DUP_QN_MARKER}9"),
            (cs.RelationshipType.CALLS, f"{m}.Tool{cs.DUP_QN_MARKER}9.run"),
        }


class TestReceiverTypeFollowsTheLiveBinding:
    # A method call on `X()` binds through ONE type. The class is only that
    # type when no def that may run instead returns something else.

    def test_later_unconditional_def_types_by_its_return(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, FACTORY_SRC)
        for caller in ("use", "use_var"):
            methods = {
                (qn, res)
                for rel, label, qn, res in _edges_from(mock_ingestor, f"{m}.{caller}")
                if rel == cs.RelationshipType.CALLS and label == cs.NodeLabel.METHOD
            }
            assert methods == {
                (f"{m}.Product.product_method", cs.EdgeResolution.EXACT)
            }, caller

    def test_disagreeing_live_bindings_type_the_receiver_as_neither(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, DISAGREE_SRC)
        for caller in ("use", "use_var"):
            exact_methods = {
                qn
                for rel, label, qn, res in _edges_from(mock_ingestor, f"{m}.{caller}")
                if rel == cs.RelationshipType.CALLS
                and label == cs.NodeLabel.METHOD
                and res == cs.EdgeResolution.EXACT
            }
            assert exact_methods == set(), caller


class TestNeighboursUnchanged:
    def test_plain_class_without_def_is_exact(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, PLAIN_SRC)
        assert _defs(mock_ingestor, "Tool") == {(cs.NodeLabel.CLASS, f"{m}.Tool", 1)}
        exact = cs.EdgeResolution.EXACT
        assert _edges_from(mock_ingestor, f"{m}.use") == {
            (cs.RelationshipType.INSTANTIATES, cs.NodeLabel.CLASS, f"{m}.Tool", exact),
            (cs.RelationshipType.CALLS, cs.NodeLabel.METHOD, f"{m}.Tool.run", exact),
        }

    def test_if_else_function_twins_are_unchanged(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, IF_ELSE_SRC)
        assert _defs(mock_ingestor, "impl") == {
            (cs.NodeLabel.FUNCTION, f"{m}.impl", 6),
            (cs.NodeLabel.FUNCTION, f"{m}.impl{cs.DUP_QN_MARKER}11", 11),
        }
        overload = cs.EdgeResolution.OVERLOAD
        assert _edges_from(mock_ingestor, f"{m}.caller") == {
            (cs.RelationshipType.CALLS, cs.NodeLabel.FUNCTION, f"{m}.impl", overload),
            (
                cs.RelationshipType.CALLS,
                cs.NodeLabel.FUNCTION,
                f"{m}.impl{cs.DUP_QN_MARKER}11",
                overload,
            ),
        }

    def test_import_error_fallback_class_is_unchanged(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        m = _index(temp_repo, mock_ingestor, TRY_IMPORT_SRC)
        assert _defs(mock_ingestor, "Tool") == {(cs.NodeLabel.CLASS, f"{m}.Tool", 5)}
        exact = cs.EdgeResolution.EXACT
        assert _edges_from(mock_ingestor, f"{m}.use") == {
            (cs.RelationshipType.INSTANTIATES, cs.NodeLabel.CLASS, f"{m}.Tool", exact),
            (cs.RelationshipType.CALLS, cs.NodeLabel.METHOD, f"{m}.Tool.run", exact),
        }
