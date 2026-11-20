"""Issue #2858: IPython/Jupyter rich-display methods are dead-code roots.

IPython calls `_repr_html_`, `_repr_mimebundle_`, `_ipython_display_` and
their siblings by name (`getattr(obj, "_repr_html_")`), never through a call
the graph can see, exactly as the runtime calls a `__dunder__`. Spelled with
one underscore each side, they were no roots, so they were reported dead
(rich's two `_repr_mimebundle_` were its only candidates) and took every
helper only they reach with them.
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.dead_code import _NodeId, _RelTuple
from codebase_rag.tests.test_dead_code_eval import _CONFIG, _MODULE, _PREFIX, _method
from codebase_rag.types_defs import PropertyDict
from evals.dead_code import dead_code_from_graph

_METHOD = cs.NodeLabel.METHOD.value
_CALLS = cs.RelationshipType.CALLS.value

PROTOCOL = (
    "_repr_html_",
    "_repr_markdown_",
    "_repr_svg_",
    "_repr_png_",
    "_repr_jpeg_",
    "_repr_latex_",
    "_repr_json_",
    "_repr_javascript_",
    "_repr_pdf_",
    "_repr_mimebundle_",
    "_repr_pretty_",
    "_ipython_display_",
    "_ipython_key_completions_",
)


def _module() -> tuple:
    return ((_MODULE, "proj.m"), {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"})


@pytest.mark.parametrize("name", PROTOCOL)
def test_a_display_protocol_method_is_a_root(name: str) -> None:
    nodes = dict([_module(), _method(f"proj.m.Widget.{name}")])

    assert dead_code_from_graph(nodes, [], _PREFIX, _CONFIG) == set()


def test_what_only_the_protocol_reaches_is_live() -> None:
    # The issue's Widget: `_render` is called from the protocol methods alone.
    nodes = dict(
        [
            _module(),
            _method("proj.m.Widget._repr_html_"),
            _method("proj.m.Widget._repr_mimebundle_"),
            _method("proj.m.Widget._ipython_display_"),
            _method("proj.m.Widget._render"),
            _method("proj.m.Widget._genuinely_dead"),
        ]
    )
    rels: list[_RelTuple] = [
        (_METHOD, f"proj.m.Widget.{caller}", _CALLS, _METHOD, "proj.m.Widget._render")
        for caller in ("_repr_html_", "_repr_mimebundle_", "_ipython_display_")
    ]

    assert dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG) == {
        "proj.m.Widget._genuinely_dead"
    }


# Negative: what must not change.


@pytest.mark.parametrize(
    ("uid", "path"),
    [
        ("proj.m.Widget._repr_custom", "m.py"),
        ("proj.m.Widget._repr_", "m.py"),
        ("proj.m.Widget._ipython_helper_", "m.py"),
        ("proj.m.Widget._repr_html_", "m.js"),
    ],
    ids=["no-trailing-underscore", "bare-prefix", "unknown-ipython-hook", "not-python"],
)
def test_a_lookalike_is_still_a_candidate(uid: str, path: str) -> None:
    nodes = dict([_module(), _method(uid, path=path)])

    assert dead_code_from_graph(nodes, [], _PREFIX, _CONFIG) == {uid}


def test_a_module_level_function_of_that_name_is_still_a_candidate() -> None:
    # Only a method is looked up on the displayed object.
    uid = "proj.m._repr_html_"
    nodes: dict[_NodeId, PropertyDict] = dict(
        [
            _module(),
            (
                (cs.NodeLabel.FUNCTION.value, uid),
                {
                    cs.KEY_QUALIFIED_NAME: uid,
                    cs.KEY_PATH: "m.py",
                    cs.KEY_DECORATORS: [],
                    cs.KEY_IS_EXPORTED: False,
                },
            ),
        ]
    )

    assert dead_code_from_graph(nodes, [], _PREFIX, _CONFIG) == {uid}
