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


def _method(uid: str, path: str = "m.py", decorators: list[str] | None = None) -> tuple:
    return (
        (cs.NodeLabel.METHOD.value, uid),
        {
            cs.KEY_QUALIFIED_NAME: uid,
            cs.KEY_NAME: uid.rsplit(cs.SEPARATOR_DOT, 1)[-1],
            cs.KEY_PATH: path,
            cs.KEY_DECORATORS: decorators or [],
            cs.KEY_IS_EXPORTED: False,
        },
    )


def test_dead_code_flags_uncalled_function() -> None:
    # Module calls main(); main() calls helper(); orphan() is never called.
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


def test_go_init_and_main_are_roots() -> None:
    # Go `func init()` is auto-run by the runtime at package load and `func
    # main()` is the program entry; neither is ever called explicitly, so both
    # are reachability roots (like Python dunders). A same-file helper with no
    # caller is still dead -- the exemption is name-scoped to init/main.
    nodes = dict(
        [
            (
                (_MODULE, "proj.mode"),
                {cs.KEY_QUALIFIED_NAME: "proj.mode", cs.KEY_PATH: "mode.go"},
            ),
            _fn("proj.mode.init", path="mode.go"),
            _fn("proj.main.main", path="main.go"),
            _fn("proj.util.helper", path="util.go"),
            _method("proj.mode.Type.init", path="mode.go"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.mode.init" not in dead
    assert "proj.main.main" not in dead
    assert "proj.util.helper" in dead
    # A receiver method named init/main is NOT the package init/entry, so the
    # exemption is Function-scoped and this stays dead.
    assert "proj.mode.Type.init" in dead


def test_rust_trait_methods_and_main_are_roots() -> None:
    # Rust trait-impl methods (Display::fmt, PartialEq::eq, Iterator::next) are
    # dispatched by the language (format!, ==, for), never called explicitly, and
    # `fn main()` is the program entry -- all reachability roots (like Python
    # dunders), gated by the .rs extension. A custom method (push_int) is not a
    # trait name, so it stays dead.
    nodes = dict(
        [
            (
                (_MODULE, "proj.frame"),
                {cs.KEY_QUALIFIED_NAME: "proj.frame", cs.KEY_PATH: "frame.rs"},
            ),
            _method("proj.frame.Frame.fmt", "frame.rs"),
            _method("proj.frame.Frame.eq", "frame.rs"),
            _method("proj.iter.It.next", "iter.rs"),
            _method("proj.frame.Frame.push_int", "frame.rs"),
            _fn("proj.main.main", path="main.rs"),
            _method("proj.frame.Frame.main", "frame.rs"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.frame.Frame.fmt" not in dead
    assert "proj.frame.Frame.eq" not in dead
    assert "proj.iter.It.next" not in dead
    assert "proj.main.main" not in dead
    assert "proj.frame.Frame.push_int" in dead
    # A method named main is not the binary entry, so it stays dead (main is
    # Function-scoped; trait-method rooting is the reverse, Method-scoped).
    assert "proj.frame.Frame.main" in dead


def test_cpp_operator_overloads_are_roots() -> None:
    # A C++ operator overload / user-defined literal (member `operator==`, free
    # `operator<<`, UDL `operator""_json`) is invoked by operator/literal SYNTAX
    # (`a == b`, `os << x`, `1_json`), never by a named call the graph can see, so
    # it is a reachability root (like Python dunders / Rust trait methods), gated
    # by a C++ file extension. A regular uncalled method stays dead, and a non-C++
    # symbol whose name merely starts with `operator` is NOT rooted.
    nodes = dict(
        [
            (
                (_MODULE, "proj.json"),
                {cs.KEY_QUALIFIED_NAME: "proj.json", cs.KEY_PATH: "json.hpp"},
            ),
            _method("proj.json.Json.operator_equal", "json.hpp"),
            _method("proj.json.Json.operator_subscript", "json.hpp"),
            _fn("proj.json.operator_left_shift", path="json.hpp"),
            _fn('proj.json.operator_""_json', path="json.hpp"),
            _method("proj.json.Json.push_back", "json.hpp"),
            _fn("proj.mod.operator_equal", path="mod.py"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.json.Json.operator_equal" not in dead
    assert "proj.json.Json.operator_subscript" not in dead
    assert "proj.json.operator_left_shift" not in dead
    assert 'proj.json.operator_""_json' not in dead
    # A regular method with no caller is still dead -- rooting is prefix-scoped
    # to `operator`, not a blanket C++ exemption.
    assert "proj.json.Json.push_back" in dead
    # The `operator` prefix only roots on a C++ file; a .py symbol stays dead.
    assert "proj.mod.operator_equal" in dead


def test_java_serialization_hooks_are_roots() -> None:
    # Java serialization hooks (readObject/writeObject/writeReplace/readResolve/
    # readObjectNoData) are invoked reflectively by the java.io runtime, never by a
    # call the graph can see, so they are reachability roots (like Python dunders),
    # gated by the .java extension. The real Java QN carries a signature
    # (`readObject(ObjectInputStream)`), which must be stripped to the bare name. A
    # regular uncalled method stays dead, and a same-named symbol on a non-Java file
    # is NOT rooted.
    nodes = dict(
        [
            (
                (_MODULE, "proj.S"),
                {cs.KEY_QUALIFIED_NAME: "proj.S", cs.KEY_PATH: "S.java"},
            ),
            _method("proj.S.S.readObject(ObjectInputStream)", "S.java"),
            _method("proj.S.S.writeReplace()", "S.java"),
            _method("proj.S.S.readResolve()", "S.java"),
            _method("proj.S.S.helper()", "S.java"),
            _method("proj.mod.C.readObject(ObjectInputStream)", "mod.py"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.S.S.readObject(ObjectInputStream)" not in dead
    assert "proj.S.S.writeReplace()" not in dead
    assert "proj.S.S.readResolve()" not in dead
    # A regular uncalled method is still dead -- rooting is name-scoped to the
    # reserved serialization hooks, not a blanket Java exemption.
    assert "proj.S.S.helper()" in dead
    # The hook names only root on a .java file; a .py symbol stays dead.
    assert "proj.mod.C.readObject(ObjectInputStream)" in dead


def test_override_of_reachable_method_is_reachable() -> None:
    # A call to a base/interface method dispatches at runtime to any override, so
    # an override of a REACHABLE method is itself reachable (sound virtual dispatch).
    # The graph records OVERRIDES (overrider -> overridden) but the reachability walk
    # follows CALLS/REFERENCES only, so overrides reached solely by dispatch looked
    # dead (gson's RecordHelper strategy subclasses). A call reaches the base; the
    # override must be revived; an override of a DEAD base stays dead.
    _OVERRIDES = cs.RelationshipType.OVERRIDES.value
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.java"},
            ),
            _fn("proj.m.entry", path="m.java"),
            _method("proj.m.Base.run(int)", "m.java"),
            _method("proj.m.Sub.run(int)", "m.java"),
            _method("proj.m.SubSub.run(int)", "m.java"),
            _method("proj.m.DeadBase.gone()", "m.java"),
            _method("proj.m.DeadSub.gone()", "m.java"),
        ]
    )
    _MTD = cs.NodeLabel.METHOD.value
    nodes[(_FUNCTION, "proj.m.entry")][cs.KEY_IS_EXPORTED] = True
    rels = [
        (_FUNCTION, "proj.m.entry", _CALLS, _MTD, "proj.m.Base.run(int)"),
        (_MTD, "proj.m.Sub.run(int)", _OVERRIDES, _MTD, "proj.m.Base.run(int)"),
        # multi-level: SubSub overrides Sub overrides Base -- all must revive.
        (_MTD, "proj.m.SubSub.run(int)", _OVERRIDES, _MTD, "proj.m.Sub.run(int)"),
        (_MTD, "proj.m.DeadSub.gone()", _OVERRIDES, _MTD, "proj.m.DeadBase.gone()"),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    # Base is called (via exported entry) -> live; its overrides (direct and
    # transitive) are dispatch targets -> revived.
    assert "proj.m.Base.run(int)" not in dead
    assert "proj.m.Sub.run(int)" not in dead
    assert "proj.m.SubSub.run(int)" not in dead
    # DeadBase is never called, so neither it nor its override is reachable.
    assert "proj.m.DeadBase.gone()" in dead
    assert "proj.m.DeadSub.gone()" in dead


def test_root_level_tests_dir_is_excluded() -> None:
    # A top-level `tests/` dir (Rust integration tests `tests/client.rs`, a JS
    # `tests/` folder) is test infrastructure. The `/tests/` pattern needs a
    # leading slash, so a root tests/ dir was missed and every test fn leaked as
    # a candidate. Path matching must normalize a leading slash so root tests/ is
    # recognized; a `contests/` dir must NOT be mistaken for a tests dir.
    nodes = dict(
        [
            (
                (_MODULE, "proj.tests.client"),
                {
                    cs.KEY_QUALIFIED_NAME: "proj.tests.client",
                    cs.KEY_PATH: "tests/client.rs",
                },
            ),
            _fn("proj.tests.client.smoke", path="tests/client.rs"),
            _fn("proj.contests.entry", path="contests/entry.rs"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.tests.client.smoke" not in dead
    # `contests/` is not a tests dir, so a genuinely-uncalled symbol there is
    # still reported (no false leading-slash match).
    assert "proj.contests.entry" in dead


def test_dead_code_excludes_generated_paths() -> None:
    # A generated file (openapi-ts client/core, routeTree.gen.ts) has no
    # in-repo caller, so every symbol in it reports as dead -- pure noise the
    # user cannot act on. An exclude glob suppresses those from the report while
    # a real orphan elsewhere is still flagged. Crucially, the excluded file
    # stays a live participant: `used_by_gen` is called only from the generated
    # module and must NOT be flagged dead (excluding before reachability would
    # drop that edge and wrongly report it).
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
    # orphan() calls buried(), but orphan() itself is never reached, so both
    # are dead (a callee kept alive only by dead code is dead too).
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
    # A function with a recognised entry-point decorator (e.g. @app.route) is
    # live even if nothing calls it.
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
    # Pydantic invokes @field_validator/@model_validator methods by registration
    # through library code that is not in the first-party graph, so reachability
    # cannot trace the call; the default decorator set must seed them as roots.
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
    # A helper defined INSIDE a test file is test infrastructure, not
    # production dead code. With tests excluded its only callers (test
    # functions) can never root it, so reporting it is unconditional noise
    # (the MockHTTPRouter/mock-helper cluster); production code reached only
    # from tests must still be reported.
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
    # Pydantic calls @computed_field methods during serialization through
    # library code outside the first-party graph, so no CALLS edge reaches
    # them; the default decorator set must seed them as roots too.
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
    # With tests excluded, a call from a test module must not root project code.
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
    # A private, uncalled function is genuinely dead. A public one is part of
    # the module's API surface (a potential external entry point), so it is a
    # reachability root and must not be flagged.
    assert "proj.m._orphan" in dead
    assert "proj.m.orphan" not in dead
    assert "proj.m.main" not in dead
    assert "proj.m.helper" not in dead


def test_dunder_root_is_limited_to_methods() -> None:
    # Implicit dunder dispatch applies to special METHODS on objects, not to a
    # module-level function that merely has a dunder-shaped name, so an uncalled
    # function named __unused__ must stay a dead-code candidate.
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
    # __aenter__/__aexit__ are invoked by the `async with` protocol, never by an
    # explicit call, so cgr cannot see an inbound edge. They are runtime protocol
    # hooks and must be treated as reachable roots, while a plain private method
    # with no caller stays dead.
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
    # __aenter__/__aexit__ on the Inner class returned by Counter.__call__ are
    # driven by `async with` and can never show an inbound call edge; they must
    # not be reported dead (full parse -> graph -> reachability path).
    src = tmp_path / "proj"
    _make_async_cm_repo(src)
    dead = cgr_dead_code(src, "proj", default_dead_code_config(False, False))
    assert not any(qn.endswith("__aenter__") for qn in dead)
    assert not any(qn.endswith("__aexit__") for qn in dead)


def test_dunder_root_is_scoped_to_python() -> None:
    # Dunder methods are a Python runtime protocol. A function named __unused__ in
    # another language (a .ts file) is not implicitly invoked, so it must stay a
    # dead-code candidate; only Python (.py) dunders are rooted.
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
    # An @abstractmethod is a contract invoked polymorphically through concrete
    # overrides, never by a direct call the graph can trace, so the abstract stub
    # must not be reported dead (SqsBatchJob._raw_batch_operation shape).
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
    # A decorated function nested inside another function (prompt_toolkit
    # @bindings.add, MCP @server.list_tools) is handed to a framework when the
    # enclosing function runs, so it is live regardless of the decorator-name
    # whitelist; an undecorated uncalled sibling closure stays dead.
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
    # The registration exemption is tied to a LIVE owner: when the enclosing
    # function is itself unreachable its decorated closure never registers, so
    # the closure and the helper only it calls are dead too, not hidden.
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
    # The registered closure of a LIVE owner runs, so a helper reachable only
    # through the closure's calls is live as well.
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
    # typer invokes @app.callback() functions by registration, so the default
    # decorator whitelist must seed them as roots (codebase_rag.cli shape).
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
    # A method of a typing.Protocol subclass is an interface stub; callers are
    # traced to the implementations, never to the stub itself, so the stub must
    # not be reported dead while a plain class's uncalled private method is.
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
    # Full parse -> graph -> reachability: the @bindings.add closure and the
    # Protocol stub must not be flagged (the 2026-07-03 false-positive shapes).
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
    # A function reachable only through a REFERENCES edge (passed as a callback
    # into first-party plumbing that stores it) is live; a sibling with no
    # inbound edge stays dead.
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
    # Full pipeline: a nested callback passed by keyword into first-party
    # plumbing that stores it must not be reported dead (create_context shape).
    src = tmp_path / "proj"
    _make_callback_repo(src)
    dead = cgr_dead_code(src, "proj", default_dead_code_config(False, False))
    assert not any(qn.endswith("create_context") for qn in dead)


def test_external_override_property_is_root() -> None:
    # A method flagged `overrides_external` (subclass of a stdlib class
    # overriding one of its methods, click's textwrap.TextWrapper subclass) is
    # invoked by the external base's machinery -- a reachability root. An
    # unflagged sibling with no callers is still dead.
    flagged = _method("proj.tw.TextWrapper._wrap_chunks", path="tw.py")
    flagged[1][cs.KEY_OVERRIDES_EXTERNAL] = True
    nodes = dict(
        [
            (
                (_MODULE, "proj.tw"),
                {cs.KEY_QUALIFIED_NAME: "proj.tw", cs.KEY_PATH: "tw.py"},
            ),
            flagged,
            _method("proj.tw.TextWrapper.unused", path="tw.py"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.tw.TextWrapper._wrap_chunks" not in dead
    assert "proj.tw.TextWrapper.unused" in dead


def test_singular_test_dir_is_excluded() -> None:
    # The Node.js/mocha convention keeps tests under a singular `test/` dir
    # (express: 34 of 49 dead-code reports were test helpers). With tests
    # excluded, such symbols are unconditional noise. `contest/` and
    # `latest/` must NOT match (the pattern is segment-anchored by the
    # leading-slash normalization).
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.test.helper", path="test/helper.js"),
            _fn("proj.contest.helper", path="contest/helper.js"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.test.helper" not in dead
    assert "proj.contest.helper" in dead


def test_missing_decorators_property_is_not_a_root() -> None:
    # A node whose decorators property is absent (NULL in the graph) must not
    # crash root selection and must stay a dead candidate.
    nodes = {
        (_FUNCTION, "proj.m.bare"): {
            cs.KEY_QUALIFIED_NAME: "proj.m.bare",
            cs.KEY_PATH: "m.py",
        },
    }
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert dead == {"proj.m.bare"}


def test_module_edge_to_non_candidate_is_ignored() -> None:
    # A module-load edge whose target is outside the candidate set (a class
    # with classes excluded) must not root anything or leak into the report.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            (
                (_CLASS, "proj.m.Widget"),
                {cs.KEY_QUALIFIED_NAME: "proj.m.Widget", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.orphan"),
        ]
    )
    rels = [(_MODULE, "proj.m", _CALLS, _CLASS, "proj.m.Widget")]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {"proj.m.orphan"}


def test_factory_class_methods_are_dispatch_roots() -> None:
    # django-style class factory: create_manager() defines RelatedManager
    # inside itself and hands it out (return value / argument), so instances
    # surface behind dynamic receivers and no call edge ever lands on the
    # methods. Once the factory is LIVE its class's methods are dispatch
    # surface, and their callee closure revives with them.
    method = cs.NodeLabel.METHOD.value
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.create_manager"),
            _class("proj.m.create_manager.RelatedManager"),
            _method("proj.m.create_manager.RelatedManager.add"),
            _method("proj.m.create_manager.RelatedManager._apply_filters"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _CALLS, _FUNCTION, "proj.m.create_manager"),
        (
            _FUNCTION,
            "proj.m.create_manager",
            _DEFINES,
            _CLASS,
            "proj.m.create_manager.RelatedManager",
        ),
        (
            _CLASS,
            "proj.m.create_manager.RelatedManager",
            _DEFINES_METHOD,
            method,
            "proj.m.create_manager.RelatedManager.add",
        ),
        (
            _CLASS,
            "proj.m.create_manager.RelatedManager",
            _DEFINES_METHOD,
            method,
            "proj.m.create_manager.RelatedManager._apply_filters",
        ),
        (
            method,
            "proj.m.create_manager.RelatedManager.add",
            _CALLS,
            method,
            "proj.m.create_manager.RelatedManager._apply_filters",
        ),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == set()


def test_dead_factory_class_methods_stay_dead() -> None:
    # The factory itself is never called: neither it nor its nested class's
    # methods may be revived (the dispatch-surface rule applies only to LIVE
    # factories).
    method = cs.NodeLabel.METHOD.value
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _fn("proj.m.create_manager"),
            _class("proj.m.create_manager.RelatedManager"),
            _method("proj.m.create_manager.RelatedManager.add"),
        ]
    )
    rels = [
        (
            _FUNCTION,
            "proj.m.create_manager",
            _DEFINES,
            _CLASS,
            "proj.m.create_manager.RelatedManager",
        ),
        (
            _CLASS,
            "proj.m.create_manager.RelatedManager",
            _DEFINES_METHOD,
            method,
            "proj.m.create_manager.RelatedManager.add",
        ),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {
        "proj.m.create_manager",
        "proj.m.create_manager.RelatedManager.add",
    }


def test_module_level_class_methods_are_not_rooted_by_defines() -> None:
    # The dispatch-surface rule is scoped to classes nested in functions or
    # methods: a module-level class's uncalled method must stay dead.
    method = cs.NodeLabel.METHOD.value
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _class("proj.m.Plain"),
            _method("proj.m.Plain.helper"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _DEFINES, _CLASS, "proj.m.Plain"),
        (_MODULE, "proj.m", _CALLS, _CLASS, "proj.m.Plain"),
        (
            _CLASS,
            "proj.m.Plain",
            _DEFINES_METHOD,
            method,
            "proj.m.Plain.helper",
        ),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == {"proj.m.Plain.helper"}


def test_factory_revived_by_override_expansion_roots_its_class() -> None:
    # Interleaving: Sub.make only goes live as an OVERRIDE of the called
    # Base.make, and Sub.make is itself a class factory. The factory and
    # override expansions feed each other, so the nested class's methods
    # must still be revived (fixed point, not a fixed pass order).
    method = cs.NodeLabel.METHOD.value
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _method("proj.m.Base.make"),
            _method("proj.m.Sub.make"),
            _method("proj.m.Sub.make.Manager.run"),
        ]
    )
    rels = [
        (_MODULE, "proj.m", _CALLS, method, "proj.m.Base.make"),
        (
            method,
            "proj.m.Sub.make",
            cs.RelationshipType.OVERRIDES.value,
            method,
            "proj.m.Base.make",
        ),
        (method, "proj.m.Sub.make", _DEFINES, _CLASS, "proj.m.Sub.make.Manager"),
        (
            _CLASS,
            "proj.m.Sub.make.Manager",
            _DEFINES_METHOD,
            method,
            "proj.m.Sub.make.Manager.run",
        ),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, _CONFIG)
    assert dead == set()


def test_enum_protocol_hooks_are_roots() -> None:
    # Python's Enum machinery invokes _generate_next_value_ (on auto())
    # and _missing_ (on failed lookup) by NAME, never through a call the
    # graph can see -- runtime hooks exactly like dunders (django's
    # TextChoices._generate_next_value_). An arbitrary sunder-named
    # method is NOT in the protocol and must stay a dead candidate.
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _method("proj.m.TextChoices._generate_next_value_"),
            _method("proj.m.Color._missing_"),
            _method("proj.m.Color._custom_sunder_"),
            # _order_ / _ignore_ are Enum class ATTRIBUTES consumed at
            # class creation, never methods the machinery invokes; a
            # user-defined method with that name is ordinary dead code.
            _method("proj.m.Color._order_"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert dead == {"proj.m.Color._custom_sunder_", "proj.m.Color._order_"}


def test_property_family_decorated_methods_are_roots() -> None:
    # A @property/@cached_property/@classproperty method (and @x.setter /
    # @x.deleter) is invoked by ATTRIBUTE syntax -- a bare read like
    # `self.app_config._is_default_auto_field_overridden` produces no call
    # node, so no CALLS edge can ever land on it (django's
    # WhereNode._output_field_or_none, Expression._constructor_signature).
    # Same invisible-invocation situation as dunders: roots, not dead code.
    # Its callees revive through the normal walk.
    config = default_dead_code_config(include_tests=False, include_classes=False)
    nodes = dict(
        [
            (
                (_MODULE, "proj.m"),
                {cs.KEY_QUALIFIED_NAME: "proj.m", cs.KEY_PATH: "m.py"},
            ),
            _method("proj.m.Options.plain_prop", decorators=["@property"]),
            _method(
                "proj.m.Options.cached",
                decorators=["@functools.cached_property"],
            ),
            _method("proj.m.Expr.sig", decorators=["@classproperty"]),
            _method("proj.m.Model.hybrid", decorators=["@hybrid_property"]),
            _method("proj.m.Options.value", decorators=["@value.setter"]),
            _method("proj.m.Options.gone", decorators=["@gone.deleter"]),
            _method("proj.m.Options._helper"),
            _method("proj.m.Options.undecorated"),
            _method("proj.m.Options.custom", decorators=["@deprecated"]),
        ]
    )
    rels = [
        (
            cs.NodeLabel.METHOD.value,
            "proj.m.Options.plain_prop",
            _CALLS,
            cs.NodeLabel.METHOD.value,
            "proj.m.Options._helper",
        ),
    ]
    dead = dead_code_from_graph(nodes, rels, _PREFIX, config)
    assert dead == {"proj.m.Options.undecorated", "proj.m.Options.custom"}


def test_duplicate_variant_leaf_still_matches_name_roots() -> None:
    # Go allows several init() in ONE file; the duplicate-qn machinery
    # renames the second to `init@51`, whose leaf failed the name-based
    # root checks and reported the runtime-invoked initializer dead
    # (kubernetes pkg.apis.abac register.init@51). The marker suffix is a
    # registration artifact, never part of the written name -- strip it
    # before every name-scoped root rule.
    nodes = dict(
        [
            (
                (_MODULE, "proj.register"),
                {cs.KEY_QUALIFIED_NAME: "proj.register", cs.KEY_PATH: "register.go"},
            ),
            _fn("proj.register.init", path="register.go"),
            _fn("proj.register.init@51", path="register.go"),
            _fn("proj.register.helper@60", path="register.go"),
            _method("proj.frame.Frame.fmt@12", "frame.rs"),
        ]
    )
    dead = dead_code_from_graph(nodes, [], _PREFIX, _CONFIG)
    assert "proj.register.init" not in dead
    assert "proj.register.init@51" not in dead
    assert "proj.frame.Frame.fmt@12" not in dead
    # a non-root name keeps its variant dead -- stripping the marker must
    # not accidentally widen any rule
    assert "proj.register.helper@60" in dead
