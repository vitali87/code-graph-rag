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
_CLASS = cs.NodeLabel.CLASS.value
_CALLS = cs.RelationshipType.CALLS.value
_DEFINES = cs.RelationshipType.DEFINES.value
_DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_INHERITS = cs.RelationshipType.INHERITS.value
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


def test_dead_code_excludes_generated_paths() -> None:
    # (H) A generated file (openapi-ts client/core, routeTree.gen.ts) has no
    # (H) in-repo caller, so every symbol in it reports as dead -- pure noise the
    # (H) user cannot act on. An exclude glob suppresses those from the report while
    # (H) a real orphan elsewhere is still flagged. Crucially, the excluded file
    # (H) stays a live participant: `used_by_gen` is called only from the generated
    # (H) module and must NOT be flagged dead (excluding before reachability would
    # (H) drop that edge and wrongly report it).
    nodes = dict(
        [
            (
                (_MODULE, "proj.gen"),
                {cs.KEY_QUALIFIED_NAME: "proj.gen", cs.KEY_PATH: "client/core/req.ts"},
            ),
            _fn("proj.gen.helper", path="client/core/req.ts"),
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.ts"},
            ),
            _fn("proj.m.orphan", path="m.ts"),
            _fn("proj.m.used_by_gen", path="m.ts"),
        ]
    )
    rels = [
        (_MODULE, "proj.gen", _CALLS, _FUNCTION, "proj.gen.helper"),
        (_FUNCTION, "proj.gen.helper", _CALLS, _FUNCTION, "proj.m.used_by_gen"),
    ]
    cfg = _CONFIG._replace(exclude_patterns=("*client/core*",))
    dead = dead_code_from_graph(nodes, rels, _PREFIX, cfg)
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


def test_test_file_symbols_are_not_candidates_when_tests_excluded() -> None:
    # (H) A helper defined INSIDE a test file is test infrastructure, not
    # (H) production dead code. With tests excluded its only callers (test
    # (H) functions) can never root it, so reporting it is unconditional noise
    # (H) (the MockHTTPRouter/mock-helper cluster); production code reached only
    # (H) from tests must still be reported.
    nodes = dict(
        [
            (
                (_MODULE, "proj.tests.test_m"),
                {
                    cs.KEY_QUALIFIED_NAME: "proj.tests.test_m",
                    cs.KEY_PATH: "tests/test_m.py",
                },
            ),
            _fn("proj.tests.test_m._helper", path="tests/test_m.py"),
            _fn("proj.m.only_tested"),
        ]
    )
    rels = [
        (_MODULE, "proj.tests.test_m", _CALLS, _FUNCTION, "proj.m.only_tested"),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {"proj.m.only_tested"}


def test_pydantic_computed_field_is_a_root() -> None:
    # (H) Pydantic calls @computed_field methods during serialization through
    # (H) library code outside the first-party graph, so no CALLS edge reaches
    # (H) them; the default decorator set must seed them as roots too.
    config = default_dead_code_config(include_tests=False, include_classes=False)
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.C.full_name", decorators=["@computed_field", "@property"]),
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


def test_abstract_method_is_a_root() -> None:
    # (H) An @abstractmethod is a contract invoked polymorphically through concrete
    # (H) overrides, never by a direct call the graph can trace, so the abstract stub
    # (H) must not be reported dead (SqsBatchJob._raw_batch_operation shape).
    config = default_dead_code_config(include_tests=False, include_classes=False)
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.Base._raw", decorators=["@abstractmethod"]),
            _fn("proj.m.Base._also", decorators=["@abc.abstractmethod"]),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, config)
    assert dead == set()


def test_registration_decorated_nested_function_is_a_root() -> None:
    # (H) A decorated function nested inside another function (prompt_toolkit
    # (H) @bindings.add, MCP @server.list_tools) is handed to a framework when the
    # (H) enclosing function runs, so it is live regardless of the decorator-name
    # (H) whitelist; an undecorated uncalled sibling closure stays dead.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.outer"),
            _fn("proj.m.outer._submit", decorators=["@bindings.add('c-j')"]),
            _fn("proj.m.outer._unused"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _CALLS, _FUNCTION, "proj.m.outer"),
        (_FUNCTION, "proj.m.outer", _DEFINES, _FUNCTION, "proj.m.outer._submit"),
        (_FUNCTION, "proj.m.outer", _DEFINES, _FUNCTION, "proj.m.outer._unused"),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {"proj.m.outer._unused"}


def test_closure_of_dead_function_is_not_a_root() -> None:
    # (H) The registration exemption is tied to a LIVE owner: when the enclosing
    # (H) function is itself unreachable its decorated closure never registers, so
    # (H) the closure and the helper only it calls are dead too, not hidden.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.main"),
            _fn("proj.m.dead_outer"),
            _fn("proj.m.dead_outer._ghost", decorators=["@bindings.add('c-x')"]),
            _fn("proj.m.victim"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _CALLS, _FUNCTION, "proj.m.main"),
        (
            _FUNCTION,
            "proj.m.dead_outer",
            _DEFINES,
            _FUNCTION,
            "proj.m.dead_outer._ghost",
        ),
        (_FUNCTION, "proj.m.dead_outer._ghost", _CALLS, _FUNCTION, "proj.m.victim"),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {
        "proj.m.dead_outer",
        "proj.m.dead_outer._ghost",
        "proj.m.victim",
    }


def test_closure_callee_of_live_function_stays_live() -> None:
    # (H) The registered closure of a LIVE owner runs, so a helper reachable only
    # (H) through the closure's calls is live as well.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.outer"),
            _fn("proj.m.outer._submit", decorators=["@bindings.add('c-j')"]),
            _fn("proj.m._only_from_closure"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _CALLS, _FUNCTION, "proj.m.outer"),
        (_FUNCTION, "proj.m.outer", _DEFINES, _FUNCTION, "proj.m.outer._submit"),
        (
            _FUNCTION,
            "proj.m.outer._submit",
            _CALLS,
            _FUNCTION,
            "proj.m._only_from_closure",
        ),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == set()


def test_typer_callback_decorator_is_a_root() -> None:
    # (H) typer invokes @app.callback() functions by registration, so the default
    # (H) decorator whitelist must seed them as roots (codebase_rag.cli shape).
    config = default_dead_code_config(include_tests=False, include_classes=False)
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m._global_options", decorators=["@app.callback()"]),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, config)
    assert dead == set()


def _class(uid: str, path: str = "m.py") -> tuple:
    return (
        (_CLASS, uid),
        {cs.KEY_QUALIFIED_NAME: uid, cs.KEY_PATH: path},
    )


def test_protocol_stub_method_is_a_root() -> None:
    # (H) A method of a typing.Protocol subclass is an interface stub; callers are
    # (H) traced to the implementations, never to the stub itself, so the stub must
    # (H) not be reported dead while a plain class's uncalled private method is.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _class("proj.m.Loadable"),
            _class("proj.m.Plain"),
            _method("proj.m.Loadable._ensure_loaded"),
            _method("proj.m.Plain._helper"),
        ]
    )
    rels = [
        (_CLASS, "proj.m.Loadable", _INHERITS, _CLASS, "typing.Protocol"),
        (
            _CLASS,
            "proj.m.Loadable",
            _DEFINES_METHOD,
            cs.NodeLabel.METHOD.value,
            "proj.m.Loadable._ensure_loaded",
        ),
        (
            _CLASS,
            "proj.m.Plain",
            _DEFINES_METHOD,
            cs.NodeLabel.METHOD.value,
            "proj.m.Plain._helper",
        ),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {"proj.m.Plain._helper"}


def _make_registration_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "m.py").write_text(
        "from typing import Protocol\n\n\n"
        "class Loadable(Protocol):\n"
        "    def _ensure_loaded(self) -> None: ...\n\n\n"
        "def get_input(bindings):\n"
        "    @bindings.add('c-j')\n"
        "    def _submit(event):\n"
        "        return event\n"
        "    return bindings\n\n\n"
        "get_input(None)\n",
        encoding="utf-8",
    )


def test_cgr_dead_code_keeps_registration_closures_and_protocol_stubs(
    tmp_path: Path,
) -> None:
    # (H) Full parse -> graph -> reachability: the @bindings.add closure and the
    # (H) Protocol stub must not be flagged (the 2026-07-03 false-positive shapes).
    src = tmp_path / "proj"
    _make_registration_repo(src)
    dead = cgr_dead_code(src, "proj", default_dead_code_config(False, False))
    assert "proj.m.get_input._submit" not in dead
    assert "proj.m.Loadable._ensure_loaded" not in dead


def test_score_dead_code_prf() -> None:
    result = score_dead_code({"a", "b"}, {"a", "c"})
    row = result.rows[0]
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)


def test_referenced_function_is_reachable() -> None:
    # (H) A function reachable only through a REFERENCES edge (passed as a callback
    # (H) into first-party plumbing that stores it) is live; a sibling with no
    # (H) inbound edge stays dead.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m._main"),
            _fn("proj.m._main.create_context"),
            _fn("proj.m._orphan"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _CALLS, _FUNCTION, "proj.m._main"),
        (
            _FUNCTION,
            "proj.m._main",
            cs.RelationshipType.REFERENCES.value,
            _FUNCTION,
            "proj.m._main.create_context",
        ),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert "proj.m._main.create_context" not in dead
    assert "proj.m._orphan" in dead


def _make_callback_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "brrrlib.py").write_text(
        "def with_brrr_from_cfg(cfg, handlers, create_context=None):\n"
        "    cfg.ctx_factory = create_context\n"
        "    return cfg\n",
        encoding="utf-8",
    )
    (root / "worker.py").write_text(
        "from proj.brrrlib import with_brrr_from_cfg\n\n\n"
        "def main(cfg, handlers, extra):\n"
        "    def create_context(active_worker, meta):\n"
        "        return (active_worker, extra)\n"
        "    return with_brrr_from_cfg(cfg, handlers, create_context=create_context)\n",
        encoding="utf-8",
    )


def test_cgr_dead_code_keeps_stored_callback_alive(tmp_path: Path) -> None:
    # (H) Full pipeline: a nested callback passed by keyword into first-party
    # (H) plumbing that stores it must not be reported dead (create_context shape).
    src = tmp_path / "proj"
    _make_callback_repo(src)
    dead = cgr_dead_code(src, "proj", default_dead_code_config(False, False))
    assert not any(qn.endswith("create_context") for qn in dead)
