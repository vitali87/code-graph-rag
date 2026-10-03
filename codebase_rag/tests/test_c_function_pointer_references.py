# C stores functions in function pointers to build callbacks, vtables and ops
# tables (issue #2529). A function used only that way has no call site the
# graph can see, so without a REFERENCES edge from the scope that stores it,
# dead-code reports every callback, allocator hook and dispatch-table entry.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.dead_code import dead_code_from_graph, default_dead_code_config
from codebase_rag.tests.conftest import get_nodes, get_relationships, run_updater
from codebase_rag.types_defs import PropertyDict, PropertyValue

PROJECT = "cfp"
REFERENCES = cs.RelationshipType.REFERENCES
CALLS = cs.RelationshipType.CALLS

HOOKS_C = (
    "#include <stdlib.h>\n"
    "typedef struct { void *(*alloc)(size_t); void (*release)(void *); } hooks_t;\n"
    "static void *my_alloc(size_t n) { return malloc(n); }\n"
    "static void my_release(void *p) { free(p); }\n"
    "static void *my_alloc2(size_t n) { return malloc(n); }\n"
    "static void my_release2(void *p) { free(p); }\n"
    "static hooks_t positional = { my_alloc, my_release };\n"
    "static hooks_t designated = { .alloc = my_alloc2, .release = my_release2 };\n"
)

HOOKS2_C = (
    "typedef struct { int (*op)(int); } ops_t;\n"
    "static int op_inc(int x) { return x + 1; }\n"
    "static int op_dec(int x) { return x - 1; }\n"
    "static int op_neg(int x) { return -x; }\n"
    "static int (*table[])(int) = { op_neg };\n"
    "int apply(ops_t *o, int v) { return o->op(v); }\n"
    "int run(void) {\n"
    "    ops_t a;\n"
    "    a.op = op_inc;\n"
    "    ops_t b = { .op = op_dec };\n"
    "    return apply(&a, 1) + apply(&b, 2) + table[0](3);\n"
    "}\n"
    "int main(void) { return run(); }\n"
)


def _index(temp_repo: Path, mock_ingestor: MagicMock, files: dict[str, str]) -> None:
    project = temp_repo / PROJECT
    project.mkdir()
    for rel, source in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(source, encoding="utf-8")
    run_updater(project, mock_ingestor, skip_if_missing="c")


def _edges(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    }


def _qn(module: str, name: str) -> str:
    return f"{PROJECT}.{module}.{name}"


def _into(edges: set[tuple[str, str]], target: str) -> set[str]:
    return {source for source, callee in edges if callee == target}


def _dead_functions(mock_ingestor: MagicMock) -> set[str]:
    # The dead-code engine run over exactly what the parse emitted, so the
    # assertion covers the whole path from edge to report.
    nodes: dict[tuple[str, PropertyValue], PropertyDict] = {}
    for call in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION):
        props = call[0][1]
        nodes[(cs.NodeLabel.FUNCTION.value, props[cs.KEY_QUALIFIED_NAME])] = {
            cs.KEY_PATH: props[cs.KEY_PATH],
            cs.KEY_NAME: props[cs.KEY_NAME],
            cs.KEY_DECORATORS: [],
            cs.KEY_IS_EXPORTED: props.get(cs.KEY_IS_EXPORTED) is True,
            cs.KEY_OVERRIDES_EXTERNAL: False,
            cs.KEY_START_LINE: props[cs.KEY_START_LINE],
            cs.KEY_END_LINE: props[cs.KEY_END_LINE],
        }
    rels: list[tuple[str, PropertyValue, str, str, PropertyValue]] = [
        (c.args[0][0], c.args[0][2], str(c.args[1]), c.args[2][0], c.args[2][2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
    ]
    config = default_dead_code_config(include_tests=True, include_classes=False)
    return dead_code_from_graph(nodes, rels, f"{PROJECT}.", config)


class TestFunctionPointerValuesAreReferenced:
    def test_file_scope_positional_initializer(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(temp_repo, mock_ingestor, {"hooks.c": HOOKS_C})
        refs = _edges(mock_ingestor, REFERENCES)
        module = f"{PROJECT}.hooks"
        assert (module, _qn("hooks", "my_alloc")) in refs, refs
        assert (module, _qn("hooks", "my_release")) in refs, refs

    def test_file_scope_designated_initializer(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(temp_repo, mock_ingestor, {"hooks.c": HOOKS_C})
        refs = _edges(mock_ingestor, REFERENCES)
        module = f"{PROJECT}.hooks"
        assert (module, _qn("hooks", "my_alloc2")) in refs, refs
        assert (module, _qn("hooks", "my_release2")) in refs, refs

    def test_dispatch_table_initializer(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "table.c": (
                    "static int op_neg(int x) { return -x; }\n"
                    "static int op_abs(int x) { return x < 0 ? -x : x; }\n"
                    "static int op_sq(int x) { return x * x; }\n"
                    "static int (*table[])(int) = { op_neg, &op_abs, (op_sq) };\n"
                )
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        module = f"{PROJECT}.table"
        for name in ("op_neg", "op_abs", "op_sq"):
            assert (module, _qn("table", name)) in refs, (name, refs)

    def test_nested_initializer_entries(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "nested.c": (
                    "typedef struct { const char *name; int (*fn)(int); } cmd_t;\n"
                    "static int cmd_add(int x) { return x + 1; }\n"
                    "static int cmd_sub(int x) { return x - 1; }\n"
                    "static const cmd_t commands[] = {\n"
                    '    { "add", cmd_add },\n'
                    '    { .name = "sub", .fn = cmd_sub },\n'
                    "};\n"
                )
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        module = f"{PROJECT}.nested"
        assert (module, _qn("nested", "cmd_add")) in refs, refs
        assert (module, _qn("nested", "cmd_sub")) in refs, refs

    def test_field_assignment_and_local_initializer(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(temp_repo, mock_ingestor, {"hooks2.c": HOOKS2_C})
        refs = _edges(mock_ingestor, REFERENCES)
        run = _qn("hooks2", "run")
        assert (run, _qn("hooks2", "op_inc")) in refs, refs
        assert (run, _qn("hooks2", "op_dec")) in refs, refs
        assert (f"{PROJECT}.hooks2", _qn("hooks2", "op_neg")) in refs, refs

    def test_arrow_field_assignment_and_address_of(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "wire.c": (
                    "typedef struct { void (*on_read)(int); } ops_t;\n"
                    "static void on_read(int fd) { (void)fd; }\n"
                    "static void on_write(int fd) { (void)fd; }\n"
                    "static int on_close(int fd) { return fd; }\n"
                    "void wire(ops_t *ops) {\n"
                    "    int (*closer)(int) = &on_close;\n"
                    "    ops->on_read = &on_read;\n"
                    "    ops->on_read = (void (*)(int))on_write;\n"
                    "    closer(3);\n"
                    "}\n"
                )
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        wire = _qn("wire", "wire")
        for name in ("on_read", "on_write", "on_close"):
            assert (wire, _qn("wire", name)) in refs, (name, refs)

    def test_function_passed_as_argument(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "sig.c": (
                    "#include <signal.h>\n"
                    "#include <stdlib.h>\n"
                    "static int by_value(const void *a, const void *b) { return 0; }\n"
                    "static void on_sigint(int sig) { (void)sig; }\n"
                    "static void on_exit_hook(void) {}\n"
                    "static void each(void (*cb)(void)) { cb(); }\n"
                    "void setup(int *xs, int n) {\n"
                    "    qsort(xs, n, sizeof *xs, by_value);\n"
                    "    signal(SIGINT, on_sigint);\n"
                    "    each(&on_exit_hook);\n"
                    "}\n"
                )
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        setup = _qn("sig", "setup")
        for name in ("by_value", "on_sigint", "on_exit_hook"):
            assert (setup, _qn("sig", name)) in refs, (name, refs)

    def test_extern_function_in_other_translation_unit(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The usual libuv/driver shape: callbacks live in their own source
        # file, declared in a header, and are wired up from another one.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "callbacks.h": "void on_data(int fd);\n",
                "callbacks.c": "void on_data(int fd) { (void)fd; }\n",
                "loop.c": (
                    '#include "callbacks.h"\n'
                    "typedef struct { void (*cb)(int); } watcher_t;\n"
                    "static watcher_t watcher = { on_data };\n"
                ),
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert (f"{PROJECT}.loop", _qn("callbacks", "on_data")) in refs, refs

    def test_issue_sample_functions_are_not_dead(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "hooks.c": HOOKS_C + "static void never_used(void) {}\n",
                "hooks2.c": HOOKS2_C,
            },
        )
        dead = _dead_functions(mock_ingestor)
        for module, name in (
            ("hooks", "my_alloc"),
            ("hooks", "my_alloc2"),
            ("hooks", "my_release"),
            ("hooks", "my_release2"),
            ("hooks2", "op_inc"),
            ("hooks2", "op_dec"),
            ("hooks2", "op_neg"),
        ):
            assert _qn(module, name) not in dead, (name, sorted(dead))
        assert _qn("hooks", "never_used") in dead, sorted(dead)


class TestNotAFunctionReference:
    def test_parameter_named_like_function(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "param.c": (
                    "#include <signal.h>\n"
                    "typedef struct { void (*fn)(int); } slot_t;\n"
                    "static void handler(int sig) { (void)sig; }\n"
                    "void install(slot_t *s, void (*handler)(int)) {\n"
                    "    signal(SIGINT, handler);\n"
                    "    s->fn = handler;\n"
                    "    slot_t local = { handler };\n"
                    "    (void)local;\n"
                    "}\n"
                )
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("param", "handler")) == set(), refs

    def test_local_variable_named_like_function(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A local shadows only from its declaration to the end of its block:
        # `outside` uses the name after the block closed, so it IS the
        # function there.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "local.c": (
                    "#include <stdlib.h>\n"
                    "static int cmp(const void *a, const void *b) { return 0; }\n"
                    "void shadowed(int *xs, int n) {\n"
                    "    int (*cmp)(const void *, const void *) = 0;\n"
                    "    qsort(xs, n, sizeof *xs, cmp);\n"
                    "}\n"
                    "void inner(int *xs, int n) {\n"
                    "    if (n) {\n"
                    "        int (*cmp)(const void *, const void *) = 0;\n"
                    "        qsort(xs, n, sizeof *xs, cmp);\n"
                    "    }\n"
                    "}\n"
                    "void outside(int *xs, int n) {\n"
                    "    if (n) {\n"
                    "        int (*cmp)(const void *, const void *) = 0;\n"
                    "        (void)cmp;\n"
                    "    }\n"
                    "    qsort(xs, n, sizeof *xs, cmp);\n"
                    "}\n"
                )
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("local", "cmp")) == {_qn("local", "outside")}, refs

    def test_prototype_only_function_stays_dead(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Declaring a function (or a pointer parameter of its type) is not a
        # use of it.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "proto.c": (
                    "static int unused(int x);\n"
                    "static void register_cb(int (*unused)(int));\n"
                    "typedef int (*unused_fn)(int);\n"
                    "static int unused(int x) { return x; }\n"
                    "static void register_cb(int (*cb)(int)) { (void)cb; }\n"
                    "int main(void) { register_cb(0); return 0; }\n"
                )
            },
        )
        assert _into(_edges(mock_ingestor, REFERENCES), _qn("proto", "unused")) == set()
        assert _qn("proto", "unused") in _dead_functions(mock_ingestor)

    def test_direct_call_stays_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "direct.c": (
                    "typedef struct { int value; } box_t;\n"
                    "static int make(int x) { return x; }\n"
                    "static int twice(int x) { return 2 * x; }\n"
                    "static int run(box_t *b) {\n"
                    "    b->value = make(1);\n"
                    "    box_t c = { twice(2) };\n"
                    "    return twice(make(c.value));\n"
                    "}\n"
                    "int main(void) { box_t b; return run(&b); }\n"
                )
            },
        )
        run = _qn("direct", "run")
        calls = _edges(mock_ingestor, CALLS)
        assert (run, _qn("direct", "make")) in calls, calls
        assert (run, _qn("direct", "twice")) in calls, calls
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("direct", "make")) == set(), refs
        assert _into(refs, _qn("direct", "twice")) == set(), refs

    def test_static_function_of_other_translation_unit(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `static` gives a function internal linkage: a same-named function
        # in another source file can never be what this file's name denotes.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "a.c": (
                    "#include <stdlib.h>\n"
                    '#include "util.h"\n'
                    "static int cmp(const void *x, const void *y) { return 0; }\n"
                    "typedef struct { int (*fn)(int); } slot_t;\n"
                    "static slot_t slot = { helper };\n"
                    "void sort(int *xs, int n) { qsort(xs, n, sizeof *xs, cmp); }\n"
                ),
                "b.c": (
                    "static int cmp(const void *x, const void *y) { return 1; }\n"
                    "static int helper(int x) { return -x; }\n"
                ),
                "util.h": "int helper(int x);\n",
                "util.c": "int helper(int x) { return x; }\n",
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("a", "cmp")) == {_qn("a", "sort")}, refs
        assert _into(refs, _qn("b", "cmp")) == set(), refs
        assert _into(refs, _qn("util", "helper")) == {f"{PROJECT}.a"}, refs
        assert _into(refs, _qn("b", "helper")) == set(), refs

    def test_file_scope_variable_named_like_other_file_function(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `compare` in a.c is a.c's own pointer variable, not b.c's function.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "a.c": (
                    "#include <stdlib.h>\n"
                    "static int (*compare)(const void *, const void *);\n"
                    "void sort(int *xs, int n) { qsort(xs, n, sizeof *xs, compare); }\n"
                ),
                "b.c": "int compare(const void *x, const void *y) { return 0; }\n",
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("b", "compare")) == set(), refs

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("counter", id="global-int"),
            pytest.param("&counter", id="address-of-global"),
            pytest.param("RED", id="enum-constant"),
            pytest.param("0", id="literal"),
        ],
    )
    def test_non_function_values_emit_nothing(
        self, temp_repo: Path, mock_ingestor: MagicMock, value: str
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "values.c": (
                    "enum color { RED, GREEN };\n"
                    "static int counter;\n"
                    "static int keep(int x) { return x; }\n"
                    f"static long table[] = {{ (long){value} }};\n"
                    "int main(void) { return keep(0); }\n"
                )
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert refs == set(), refs


# --- #2593 review ---

RETURNS_C = (
    "typedef void (*handler_t)(int);\n"
    "static void handler(int sig) { (void)sig; }\n"
    "static void on_a(int sig) { (void)sig; }\n"
    "static void on_b(int sig) { (void)sig; }\n"
    "static void on_c(int sig) { (void)sig; }\n"
    "static handler_t pick(void) { return handler; }\n"
    "static handler_t pick_addr(void) { return &on_a; }\n"
    "static handler_t pick_either(int f) { return f ? on_b : (handler_t)on_c; }\n"
    "int main(void) {\n"
    "    pick()(1);\n"
    "    pick_addr()(2);\n"
    "    pick_either(0)(3);\n"
    "    return 0;\n"
    "}\n"
)


class TestReturnedFunctionValues:
    def test_returned_callback_is_referenced(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(temp_repo, mock_ingestor, {"ret.c": RETURNS_C})
        refs = _edges(mock_ingestor, REFERENCES)
        assert (_qn("ret", "pick"), _qn("ret", "handler")) in refs, refs
        assert (_qn("ret", "pick_addr"), _qn("ret", "on_a")) in refs, refs
        assert (_qn("ret", "pick_either"), _qn("ret", "on_b")) in refs, refs
        assert (_qn("ret", "pick_either"), _qn("ret", "on_c")) in refs, refs

    def test_returned_callback_is_not_dead(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(temp_repo, mock_ingestor, {"ret.c": RETURNS_C})
        dead = _dead_functions(mock_ingestor)
        for name in ("handler", "on_a", "on_b", "on_c"):
            assert _qn("ret", name) not in dead, (name, sorted(dead))

    def test_returned_call_result_or_local_is_no_reference(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: `return make(x);` returns a call's result (a CALLS edge),
        # and a local named like a function hides it.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "plain.c": (
                    "static int make(int x) { return x; }\n"
                    "static int shadow(int x) { return x; }\n"
                    "static int value(int x) { return make(x); }\n"
                    "static int local(int x) { int shadow = x; return shadow; }\n"
                    "int main(void) { return value(1) + local(2); }\n"
                )
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("plain", "make")) == set(), refs
        assert _into(refs, _qn("plain", "shadow")) == set(), refs
        assert (_qn("plain", "value"), _qn("plain", "make")) in _edges(
            mock_ingestor, CALLS
        )


class TestEnumeratorsAreNotFunctions:
    def test_file_scope_enumerator_is_not_another_files_function(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "state.c": (
                    "enum state { READY, DONE };\n"
                    "typedef enum { IDLE } mode_t;\n"
                    "static int initial[] = { READY, IDLE };\n"
                    "int current(void) { int s = DONE; return s; }\n"
                ),
                "other.c": (
                    "int READY(void) { return 0; }\n"
                    "int DONE(void) { return 1; }\n"
                    "int IDLE(void) { return 2; }\n"
                ),
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        for name in ("READY", "DONE", "IDLE"):
            assert _into(refs, _qn("other", name)) == set(), (name, refs)

    def test_enumerator_of_an_included_header_is_not_a_function(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "state.h": "enum state { READY };\n",
                "use.c": '#include "state.h"\nstatic int initial[] = { READY };\n',
                "other.c": "int READY(void) { return 0; }\n",
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("other", "READY")) == set(), refs

    def test_local_enumerator_is_not_another_files_function(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # An enum written in a function body declares its constants there;
        # after it, in the same body, the name is that constant.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "state.c": (
                    "int current(void) {\n"
                    "    enum { READY, DONE };\n"
                    "    int order[] = { READY, DONE };\n"
                    "    return order[0];\n"
                    "}\n"
                ),
                "other.c": (
                    "int READY(void) { return 0; }\nint DONE(void) { return 1; }\n"
                ),
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("other", "READY")) == set(), refs
        assert _into(refs, _qn("other", "DONE")) == set(), refs

    def test_an_extern_function_still_resolves_beside_an_enum(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: a name that is no enumerator still reaches the other
        # file's function.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "state.c": (
                    "enum state { READY };\n"
                    "int run(void);\n"
                    "typedef struct { int (*fn)(void); } slot_t;\n"
                    "static slot_t slot = { run };\n"
                    "static int initial[] = { READY };\n"
                ),
                "other.c": "int run(void) { return 0; }\n",
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("other", "run")) == {f"{PROJECT}.state"}, refs


class TestHeaderInlineNeedsInclude:
    def test_static_inline_in_a_header_the_file_does_not_include(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "store.c": (
                    "extern int helper(int);\n"
                    "typedef struct { int (*fn)(int); } slot_t;\n"
                    "static slot_t slot = { helper };\n"
                ),
                "real.c": "int helper(int x) { return x; }\n",
                "unrelated.h": "static inline int helper(int x) { return -x; }\n",
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("real", "helper")) == {f"{PROJECT}.store"}, refs
        assert _into(refs, _qn("unrelated", "helper")) == set(), refs

    @pytest.mark.parametrize(
        "includes",
        [
            '#include "a/util.h"\n#include "b/util.h"\n',
            '#include "b/util.h"\n#include "a/util.h"\n',
        ],
        ids=["displaced-first", "displacing-last"],
    )
    def test_static_inline_in_a_header_a_same_named_include_displaced(
        self, temp_repo: Path, mock_ingestor: MagicMock, includes: str
    ) -> None:
        # `a/util.h` and `b/util.h` bind the same local name `util`, so the
        # later include takes over the binding; the earlier header is still
        # compiled into the file, and so is its `static inline` (Greptile, PR
        # #2593). `c/util.h`, also `util`, is not included at all.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "a/util.h": "static inline int twice(int x) { return 2 * x; }\n",
                "b/util.h": "static inline int thrice(int x) { return 3 * x; }\n",
                "c/util.h": "static inline int twice(int x) { return -x; }\n",
                "main.c": (
                    includes + "typedef struct { int (*fn)(int); } slot_t;\n"
                    "static slot_t slots[] = { { twice }, { thrice } };\n"
                ),
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("a.util", "twice")) == {f"{PROJECT}.main"}, refs
        assert _into(refs, _qn("b.util", "thrice")) == {f"{PROJECT}.main"}, refs
        assert _into(refs, _qn("c.util", "twice")) == set(), refs

    def test_static_inline_in_an_included_header_is_referenced(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: a header the file includes, directly or through another
        # header, compiles its `static inline` into this file.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "inline.h": "static inline int twice(int x) { return 2 * x; }\n",
                "outer.h": '#include "inline.h"\n',
                "direct.c": (
                    '#include "inline.h"\n'
                    "typedef struct { int (*fn)(int); } slot_t;\n"
                    "static slot_t slot = { twice };\n"
                ),
                "chained.c": (
                    '#include "outer.h"\n'
                    "typedef struct { int (*fn)(int); } slot_t;\n"
                    "static slot_t slot = { twice };\n"
                ),
            },
        )
        refs = _edges(mock_ingestor, REFERENCES)
        assert _into(refs, _qn("inline", "twice")) == {
            f"{PROJECT}.direct",
            f"{PROJECT}.chained",
        }, refs
