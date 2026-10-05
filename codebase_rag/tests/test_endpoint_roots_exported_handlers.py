"""Issue #2664: with `--no-endpoint-roots` the endpoint alone decides a handler.

The Python ingest marks every public module-level function `is_exported`, and
route handlers are exactly that. The "exported symbols are roots" rule then
kept an uncalled `create_user` alive, so only `_underscore` handlers were ever
reported. The #1603 tests built handlers without `is_exported` and never met a
node shaped like the ones the parser writes.
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.dead_code import (
    DeadCodeConfig,
    dead_code_from_graph,
    default_dead_code_config,
)
from codebase_rag.types_defs import PropertyDict

P = "user_service"
_FUNCTION = cs.NodeLabel.FUNCTION.value
GET_USER = f"{P}.app.main.get_user"
CREATE_USER = f"{P}.app.main.create_user"
DELETE_USER = f"{P}.app.main._delete_user"

NodeMap = dict[tuple[str, str], PropertyDict]


def _handler(
    qn: str,
    route: str,
    *,
    path: str = "app/main.py",
    extra_decorators: tuple[str, ...] = (),
) -> tuple[tuple[str, str], PropertyDict]:
    name = qn.rsplit(".", 1)[-1]
    return (
        (_FUNCTION, qn),
        {
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_NAME: name,
            cs.KEY_PATH: path,
            cs.KEY_DECORATORS: [route, *extra_decorators],
            # As function_ingest writes it: public module-level functions are
            # exported, `_private` ones are not.
            cs.KEY_IS_EXPORTED: not name.startswith("_"),
        },
    )


def _service() -> NodeMap:
    return dict(
        [
            _handler(GET_USER, '@app.get("/users/{user_id}")'),
            _handler(CREATE_USER, '@app.post("/users")'),
            _handler(DELETE_USER, '@app.delete("/users/{user_id}")'),
        ]
    )


# The issue's graph: another project calls GET /users/42 only.
LINKS = {GET_USER: 1, CREATE_USER: 0, DELETE_USER: 0}


def _off(**changes: object) -> DeadCodeConfig:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    return config._replace(endpoint_roots=False, **changes)


def test_an_exported_handler_nobody_calls_is_reported() -> None:
    assert dead_code_from_graph(_service(), [], f"{P}.", _off(), LINKS) == {
        CREATE_USER,
        DELETE_USER,
    }


@pytest.mark.parametrize(
    "route",
    ['@app.post("/users")', '@router.post("/users")', '@bp.route("/users")'],
)
def test_the_verdict_holds_for_each_route_decorator(route: str) -> None:
    nodes = dict([_handler(CREATE_USER, route)])

    assert dead_code_from_graph(nodes, [], f"{P}.", _off(), {CREATE_USER: 0}) == {
        CREATE_USER
    }


# Negative: what must not change.


def test_a_called_exported_handler_stays_live() -> None:
    assert GET_USER not in dead_code_from_graph(_service(), [], f"{P}.", _off(), LINKS)


def test_endpoint_roots_on_still_roots_every_handler() -> None:
    on = default_dead_code_config(include_tests=True, include_classes=False)

    assert dead_code_from_graph(_service(), [], f"{P}.", on, LINKS) == set()


def test_missing_endpoint_evidence_reports_nothing() -> None:
    assert dead_code_from_graph(_service(), [], f"{P}.", _off(), None) == set()


def test_an_exported_function_that_is_no_handler_stays_a_root() -> None:
    nodes = _service()
    helper = f"{P}.app.main.helper"
    nodes[(_FUNCTION, helper)] = {
        cs.KEY_QUALIFIED_NAME: helper,
        cs.KEY_NAME: "helper",
        cs.KEY_PATH: "app/main.py",
        cs.KEY_IS_EXPORTED: True,
    }

    assert helper not in dead_code_from_graph(nodes, [], f"{P}.", _off(), LINKS)


def test_a_second_root_decorator_still_roots_the_handler() -> None:
    nodes = dict(
        [
            _handler(
                CREATE_USER, '@app.post("/users")', extra_decorators=("@app.command()",)
            )
        ]
    )

    assert dead_code_from_graph(nodes, [], f"{P}.", _off(), {CREATE_USER: 0}) == set()


def test_a_named_entry_point_still_roots_the_handler() -> None:
    config = _off(entry_points=("create_user",))

    assert CREATE_USER not in dead_code_from_graph(
        _service(), [], f"{P}.", config, LINKS
    )


def test_a_handler_in_test_code_stays_a_root_while_tests_are_roots() -> None:
    qn = f"{P}.tests.test_api.create_user"
    nodes = dict([_handler(qn, '@app.post("/users")', path="tests/test_api.py")])

    assert dead_code_from_graph(nodes, [], f"{P}.", _off(), {qn: 0}) == set()
