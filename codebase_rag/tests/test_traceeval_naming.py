"""cgr definitions renamed into TraceEval's tracer naming scheme.

Each case is a place where cgr's qualified name and the tracer's name for the
same function differ, so a wrong translation would turn a correct cgr edge
into a false positive plus a miss.
"""

from __future__ import annotations

from codebase_rag import constants as cs
from evals.traceeval import constants as tc
from evals.traceeval.naming import NameTranslator, Translation, java_tracer_names
from evals.traceeval.types_defs import CgrDefinition

_MODULE = cs.NodeLabel.MODULE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value

JAVA_SOURCE = """package com.acme.app;

class Box {
    Box(String label) { }
    static class Builder {
        Builder add(final String item, Box other, int n) { return this; }
    }
}

enum Mode {
    ON;
    void flip(Mode other) { }
}
"""


def test_java_tracer_names_reproduce_the_tracers_rule() -> None:
    names = {(m.line, m.method): m.qualified for m in java_tracer_names(JAVA_SOURCE)}

    assert names[(4, "Box")] == "com.acme.app.Box:<init>(java.lang.String)"
    # The innermost class only; `final` is the parameter's first token and
    # the tracer keeps it; a file-local class gets the package prefix.
    assert names[(6, "add")] == ("com.acme.app.Builder:add(final,com.acme.app.Box,int)")
    # Enums are not `class|interface` to the tracer: the enclosing class it
    # finds is the last one declared, and `Mode` is not a local type.
    assert names[(12, "flip")] == "com.acme.app.Builder:flip(Mode)"


def _translator(
    language: tc.Language,
    path: str,
    source: str,
    defs: list[tuple[str, str, int | None, int]],
) -> NameTranslator:
    definitions = {
        qn: CgrDefinition(label, qn, path, start) for qn, label, start, _end in defs
    }
    end_lines = {qn: end for qn, _label, _start, end in defs}

    def read(_path: str) -> str:
        return source

    return NameTranslator(language, definitions, end_lines, read)


def test_java_translation_matches_by_header_line() -> None:
    path = "com/acme/app/Box.java"
    translator = _translator(
        tc.Language.JAVA,
        path,
        JAVA_SOURCE,
        [
            ("p.com.acme.app.Box", _MODULE, None, 0),
            ("p.com.acme.app.Box.Box.Box(String)", _METHOD, 4, 4),
            ("p.com.acme.app.Box.Box.Builder.add(String,Box,int)", _METHOD, 6, 6),
        ],
    )

    ctor = translator.translate("p.com.acme.app.Box.Box.Box(String)")
    assert ctor.name == "com.acme.app.Box:<init>(java.lang.String)"
    add = translator.translate("p.com.acme.app.Box.Box.Builder.add(String,Box,int)")
    assert add.name == "com.acme.app.Builder:add(final,com.acme.app.Box,int)"
    module = translator.translate("p.com.acme.app.Box")
    assert module.name == tc.JAVA_MODULE_CALLER.format(path=path)


PY_SOURCE = """def outer():
    def inner():
        pass
apply = lambda x: x
def dup():
    pass
"""


def test_python_translation() -> None:
    translator = _translator(
        tc.Language.PYTHON,
        "pkg/main.py",
        PY_SOURCE,
        [
            ("p.pkg.main", _MODULE, None, 0),
            ("p.pkg.main.outer.inner", _FUNCTION, 2, 3),
            ("p.pkg.main.outer.anonymous_9_4", _FUNCTION, 9, 9),
            ("p.pkg.main.apply", _FUNCTION, 4, 4),
            ("p.pkg.main.dup@5", _FUNCTION, 5, 6),
        ],
    )

    assert translator.translate("p.pkg.main") == Translation("pkg.main", "pkg.main")
    assert translator.translate("p.pkg.main.outer.inner").name == "pkg.main.outer.inner"
    # Anonymous: no name to be called by, its calls credited to `outer`.
    assert translator.translate("p.pkg.main.outer.anonymous_9_4") == Translation(
        None, "pkg.main.outer"
    )
    # A lambda bound to a name is an ordinal `<lambdaN>` to the tracer.
    assert translator.translate("p.pkg.main.apply").name == "pkg.main.<lambda:apply>"
    # cgr's duplicate-name suffix has no tracer counterpart.
    assert translator.translate("p.pkg.main.dup@5").name == "pkg.main.dup"
    assert translator.translate("p.unknown").name is None


JS_SOURCE = """const editor = {
  hass: {
    format: (s) => s,
  },
  makeDropdown(label) { return label; },
};
const handler = () => run();
function run() {}
class Box { size() { return 1; } }
"""


def test_javascript_translation() -> None:
    translator = _translator(
        tc.Language.JAVASCRIPT,
        "main.js",
        JS_SOURCE,
        [
            ("p.main", _MODULE, None, 0),
            ("p.main.format", _FUNCTION, 3, 3),
            ("p.main.makeDropdown", _FUNCTION, 5, 5),
            ("p.main.handler", _FUNCTION, 7, 7),
            ("p.main.run", _FUNCTION, 8, 8),
            ("p.main.Box.size", _METHOD, 9, 9),
        ],
    )

    # Module-level JS is always `main` to the tracer, whatever the file.
    assert translator.translate("p.main").name == tc.JS_ROOT_CALLER
    # Object-literal members are named through the literal's binding.
    assert (
        translator.translate("p.main.makeDropdown").name == "main.editor.makeDropdown"
    )
    assert translator.translate("p.main.format").name == "main.editor.hass.format"
    assert translator.translate("p.main.handler").name == "main.<arrow:handler>"
    assert translator.translate("p.main.run").name == "main.run"
    assert translator.translate("p.main.Box.size").name == "main.Box.size"
