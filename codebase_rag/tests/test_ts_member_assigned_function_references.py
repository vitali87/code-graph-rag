# An inline function assigned to a member expression (`api.setState =
# (state, replace) => {...}`, zustand's middleware store-patching shape) was
# orphaned two ways: (a) the assignment-reference walk stopped at EVERY function
# boundary, so an assignment inside an anonymous curried arrow (which gets no
# caller pass of its own) was scanned by nobody; (b) the def pass used to ALSO
# register a position-named anonymous twin for the assigned arrow. Span-claim
# unification cured (b) at the root (one node per source function), so only
# the property-named node exists and it must be referenced.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _refs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "REFERENCES")
    }


def test_member_assigned_arrow_in_curried_anon_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "zmemb"
    root.mkdir(parents=True)
    (root / "mw.ts").write_text(
        "const persistImpl = (config: any) => (set: any, get: any, api: any) => {\n"
        "  api.setState = (state: any, replace: any) => {\n"
        "    return config(state, replace)\n"
        "  }\n"
        "  return config\n"
        "}\n"
        "export const persist = persistImpl as unknown\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    # the single (property-named) registration of the assigned arrow must
    # be reachable, and the position-named anonymous twin must not exist.
    assert any(t.endswith(".persistImpl.setState") for _, t in refs), sorted(
        t for _, t in refs if "persistImpl" in t
    )
    nodes = {
        c.args[1]["qualified_name"]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == "Function"
    }
    assert not any(".persistImpl.anonymous_1_" in qn for qn in nodes), sorted(nodes)


def test_cast_wrapped_member_assignment_rhs_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # devtools' shape: LHS behind a cast-paren (`;(api as any).dispatch = ...`)
    # and RHS wrapped in a cast (`(... ) as SetState`); both wrappers are
    # transparent for the reference.
    root = temp_repo / "zmemb2"
    root.mkdir(parents=True)
    (root / "mw.ts").write_text(
        "type SetState = unknown\n"
        "const devtoolsImpl = (fn: any) => (set: any, get: any, api: any) => {\n"
        "  ;(api as any).dispatch = ((action: any) => {\n"
        "    return fn(action)\n"
        "  }) as SetState\n"
        "  return fn\n"
        "}\n"
        "export const devtools = devtoolsImpl as unknown\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(
        t.endswith(".devtoolsImpl.dispatch") or ".devtoolsImpl.anonymous_2_" in t
        for _, t in refs
    ), sorted(t for _, t in refs if "devtoolsImpl" in t)


def test_local_reassigned_arrow_in_anon_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # subscribeWithSelector's shape: a LOCAL is reassigned to an arrow inside an
    # anonymous scope (`listener = (state) => {...}`); the position-named node
    # must be referenced so it does not report dead.
    root = temp_repo / "zmemb3"
    root.mkdir(parents=True)
    (root / "mw.ts").write_text(
        "const swsImpl = (fn: any) => (set: any, get: any, api: any) => {\n"
        "  api.subscribe = (selector: any, optListener: any) => {\n"
        "    let listener = optListener\n"
        "    if (selector) {\n"
        "      listener = (state: any) => {\n"
        "        return fn(state)\n"
        "      }\n"
        "    }\n"
        "    return listener\n"
        "  }\n"
        "  return fn\n"
        "}\n"
        "export const subscribeWithSelector = swsImpl as unknown\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(
        ".swsImpl.anonymous_4_" in t or t.endswith(".swsImpl.listener") for _, t in refs
    ), sorted(t for _, t in refs if "swsImpl" in t)
