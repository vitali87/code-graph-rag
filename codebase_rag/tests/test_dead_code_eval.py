from pathlib import Path

from codebase_rag import constants as cs
from evals.dead_code import (
    DeadCodeConfig,
    cgr_dead_code,
    dead_code_from_graph,
    default_dead_code_config,
    score_dead_code,
)

_MODULE = cs.NodeLabel.MODULE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_CALLS = cs.RelationshipType.CALLS.value
_PREFIX = "proj."
_CONFIG = DeadCodeConfig(
    include_tests=False,
    include_classes=False,
    root_decorators=frozenset(),
    entry_points=(),
    test_patterns=cs.TEST_PATH_PATTERNS,
)


def _fn(uid: str, path: str = "m.py", decorators: list[str] | None = None) -> tuple:
    return (
        (_FUNCTION, uid),
        {
            cs.KEY_QUALIFIED_NAME: uid,
            cs.KEY_PATH: path,
            cs.KEY_DECORATORS: decorators or [],
            cs.KEY_IS_EXPORTED: False,
        },
    )


def test_dead_code_flags_uncalled_function() -> None:
    # (H) Module calls main(); main() calls helper(); orphan() is never called.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.main"),
            _fn("proj.m.helper"),
            _fn("proj.m.orphan"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _CALLS, _FUNCTION, "proj.m.main"),
        (_FUNCTION, "proj.m.main", _CALLS, _FUNCTION, "proj.m.helper"),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {"proj.m.orphan"}


def test_dead_code_flags_orphan_chain() -> None:
    # (H) orphan() calls buried(), but orphan() itself is never reached, so both
    # (H) are dead (a callee kept alive only by dead code is dead too).
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.main"),
            _fn("proj.m.orphan"),
            _fn("proj.m.buried"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _CALLS, _FUNCTION, "proj.m.main"),
        (_FUNCTION, "proj.m.orphan", _CALLS, _FUNCTION, "proj.m.buried"),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {"proj.m.orphan", "proj.m.buried"}


def test_decorated_function_is_a_root() -> None:
    # (H) A function with a recognised entry-point decorator (e.g. @app.route) is
    # (H) live even if nothing calls it.
    config = _CONFIG._replace(root_decorators=frozenset({"route"}))
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.handler", decorators=["@app.route('/x')"]),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, config)
    assert dead == set()


def test_pydantic_validator_is_a_root() -> None:
    # (H) Pydantic invokes @field_validator/@model_validator methods by registration
    # (H) through library code that is not in the first-party graph, so reachability
    # (H) cannot trace the call; the default decorator set must seed them as roots.
    config = default_dead_code_config(include_tests=False, include_classes=False)
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn(
                "proj.m.C._check",
                decorators=["@pydantic.field_validator('x')"],
            ),
            _fn("proj.m.C._verify", decorators=["@model_validator(mode='after')"]),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, config)
    assert dead == set()


def test_non_test_module_does_not_keep_code_alive_when_tests_excluded() -> None:
    # (H) With tests excluded, a call from a test module must not root project code.
    nodes = dict(
        [
            (
                (_MODULE, "proj.tests.test_m"),
                {
                    cs.KEY_QUALIFIED_NAME: "proj.tests.test_m",
                    cs.KEY_PATH: "tests/test_m.py",
                },
            ),
            _fn("proj.m.only_tested"),
        ]
    )
    rels = [(_MODULE, "proj.tests.test_m", _CALLS, _FUNCTION, "proj.m.only_tested")]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {"proj.m.only_tested"}


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "m.py").write_text(
        "def helper():\n    return 1\n\n\n"
        "def main():\n    return helper()\n\n\n"
        "def orphan():\n    return 2\n\n\n"
        "def _orphan():\n    return 3\n\n\n"
        "main()\n",
        encoding="utf-8",
    )


def test_cgr_dead_code_matches_known_dead_set(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    dead = cgr_dead_code(src, "proj", default_dead_code_config(False, False))
    # (H) A private, uncalled function is genuinely dead. A public one is part of
    # (H) the module's API surface (a potential external entry point), so it is a
    # (H) reachability root and must not be flagged.
    assert "proj.m._orphan" in dead
    assert "proj.m.orphan" not in dead
    assert "proj.m.main" not in dead
    assert "proj.m.helper" not in dead


def _method(uid: str, path: str = "m.py") -> tuple:
    return (
        (cs.NodeLabel.METHOD.value, uid),
        {
            cs.KEY_QUALIFIED_NAME: uid,
            cs.KEY_NAME: uid.rsplit(cs.SEPARATOR_DOT, 1)[-1],
            cs.KEY_PATH: path,
            cs.KEY_DECORATORS: [],
            cs.KEY_IS_EXPORTED: False,
        },
    )


def test_dunder_root_is_limited_to_methods() -> None:
    # (H) Implicit dunder dispatch applies to special METHODS on objects, not to a
    # (H) module-level function that merely has a dunder-shaped name, so an uncalled
    # (H) function named __unused__ must stay a dead-code candidate.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            (
                (_FUNCTION, "proj.m.__unused__"),
                {
                    cs.KEY_QUALIFIED_NAME: "proj.m.__unused__",
                    cs.KEY_NAME: "__unused__",
                    cs.KEY_PATH: "m.py",
                    cs.KEY_DECORATORS: [],
                    cs.KEY_IS_EXPORTED: False,
                },
            ),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.m.__unused__" in dead


def test_dunder_method_is_a_root() -> None:
    # (H) __aenter__/__aexit__ are invoked by the `async with` protocol, never by an
    # (H) explicit call, so cgr cannot see an inbound edge. They are runtime protocol
    # (H) hooks and must be treated as reachable roots, while a plain private method
    # (H) with no caller stays dead.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _method("proj.m.Inner.__aenter__"),
            _method("proj.m.Inner.__aexit__"),
            _method("proj.m.Inner._helper"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.m.Inner.__aenter__" not in dead
    assert "proj.m.Inner.__aexit__" not in dead
    assert "proj.m.Inner._helper" in dead


def _make_async_cm_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "m.py").write_text(
        "class Counter:\n"
        "    def __call__(self):\n"
        "        class Inner:\n"
        "            async def __aenter__(self):\n"
        "                return None\n"
        "            async def __aexit__(self, *a):\n"
        "                return None\n"
        "        return Inner()\n\n\n"
        "async def use():\n"
        "    c = Counter()\n"
        "    async with c():\n"
        "        pass\n\n\n"
        "use()\n",
        encoding="utf-8",
    )


def test_cgr_dead_code_does_not_flag_context_manager_dunders(tmp_path: Path) -> None:
    # (H) __aenter__/__aexit__ on the Inner class returned by Counter.__call__ are
    # (H) driven by `async with` and can never show an inbound call edge; they must
    # (H) not be reported dead (full parse -> graph -> reachability path).
    src = tmp_path / "proj"
    _make_async_cm_repo(src)
    dead = cgr_dead_code(src, "proj", default_dead_code_config(False, False))
    assert not any(qn.endswith("__aenter__") for qn in dead)
    assert not any(qn.endswith("__aexit__") for qn in dead)


def test_dunder_root_is_scoped_to_python() -> None:
    # (H) Dunder methods are a Python runtime protocol. A function named __unused__ in
    # (H) another language (a .ts file) is not implicitly invoked, so it must stay a
    # (H) dead-code candidate; only Python (.py) dunders are rooted.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.ts"},
            ),
            (
                (_FUNCTION, "proj.m.__unused__"),
                {
                    cs.KEY_QUALIFIED_NAME: "proj.m.__unused__",
                    cs.KEY_NAME: "__unused__",
                    cs.KEY_PATH: "m.ts",
                    cs.KEY_DECORATORS: [],
                    cs.KEY_IS_EXPORTED: False,
                },
            ),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.m.__unused__" in dead


def test_score_dead_code_prf() -> None:
    result = score_dead_code({"a", "b"}, {"a", "c"})
    row = result.rows[0]
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
