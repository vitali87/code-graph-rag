# A sync's graph against a fresh index's, over generated programs whose
# callbacks are passed, forwarded, returned, wrapped and stored across files
# (issue #3781).
#
# The callable-flow edges (`run_callback -> handler`) are written from records
# several files' walks leave behind: the call site binding the callback, each
# function forwarding it, each factory returning the closure that invokes it.
# A sync re-parses a subset of the files, and the rules picking that subset
# were found wanting one review at a time. Each seed here builds a small
# program from those shapes, indexes it, makes one random edit, syncs it as
# the CLI does (a new updater), as the watcher does (the same updater) and
# as an agent's reingest does, and checks each graph against a clean index
# of the edited tree. The seeds are fixed, so a failure reproduces; hunting
# runs many more through `differential_diff` directly.
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from codebase_rag.tests.test_incremental_flow_derivations import _apply_files
from codebase_rag.tests.test_incremental_interface_skip import (
    PROJECT,
    _clean,
    _diff,
    _snapshot,
    _updater,
)
from evals.cgr_graph import _StatefulIngestor

_MODES = ("new", "reused", "reingest")
_UTIL = "util"

# What each kind needs as its target: a function a callback can be handed to
# (`_RECEIVERS`), one whose call returns such a function (`_FACTORIES`), a
# callback, a class with a callable field, a decorator.
_RECEIVERS = frozenset(
    {"invoker", "nested_invoker", "arrow_invoker", "forwarder", "kw_forwarder"}
)
_FACTORIES = frozenset({"maker", "relay", "getter"})
_PASSERS = (
    "direct",
    "keyword",
    "alias",
    "chained",
    "factory_alias",
    "bound",
    "method_ref",
    "module_level",
    "partial",
)


@dataclass
class _Fn:
    name: str
    module: str
    kind: str
    target: str = ""
    arg: str = ""
    logs: bool = False
    body_extra: bool = False


@dataclass
class _Program:
    lang: str
    modules: list[str]
    fns: list[_Fn]
    # How each module names what it imports: `from m import x`, `import m`
    # then `m.x`, or `from m import x as x_al`.
    styles: dict[str, str]
    comments: dict[str, int] = field(default_factory=dict)
    log_params: int = 1

    def by_name(self, name: str) -> _Fn | None:
        return next((fn for fn in self.fns if fn.name == name), None)


def _kinds(lang: str) -> list[str]:
    base = [
        "callback",
        "callback",
        "invoker",
        "nested_invoker",
        "forwarder",
        "maker",
        "getter",
        "passer",
        "passer",
        "passer",
    ]
    if lang == "py":
        return [*base, "kw_forwarder", "relay", "relay", "field_class", "decorator"]
    return [*base, "arrow_invoker"]


def _targets(program: _Program, fn: _Fn, pool: list[_Fn] | None = None) -> list[str]:
    """The names `fn` may point at among `pool` (every function), by its
    kind."""
    match fn.kind:
        case "forwarder" | "kw_forwarder":
            wanted = _RECEIVERS
        case "relay":
            wanted = frozenset({"maker", "relay"})
        case "getter":
            wanted = _RECEIVERS
        case "passer":
            wanted = _RECEIVERS | _FACTORIES | {"field_class", "decorator"}
        case _:
            return []
    if fn.kind == "kw_forwarder":
        # A keyword reaches only a parameter the receiver names `cb`.
        wanted = wanted - {"arrow_invoker"}
    return [
        other.name
        for other in (program.fns if pool is None else pool)
        if other.kind in wanted and other.name != fn.name
    ]


def _callbacks(program: _Program) -> list[str]:
    return [fn.name for fn in program.fns if fn.kind == "callback"]


# A function that hands a callback on falls back to the shape that runs it
# when nothing before it can be pointed at.
_FALLBACK = {
    "forwarder": "invoker",
    "kw_forwarder": "invoker",
    "relay": "maker",
    "getter": "invoker",
}


def _point(rng: random.Random, program: _Program, fn: _Fn, pool: list[_Fn]) -> None:
    """Give `fn` a target among the functions before it and, for a passer,
    a callback, at random; so every chain ends at a function invoking it."""
    if targets := _targets(program, fn, pool):
        fn.target = rng.choice(targets)
    elif fn.kind in _FALLBACK:
        fn.kind = _FALLBACK[fn.kind]
    if fn.kind == "passer" and (callbacks := _callbacks(program)):
        fn.arg = rng.choice(callbacks)


def generate(seed: int) -> _Program:
    rng = random.Random(seed)
    lang = rng.choice(("py", "py", "js"))
    modules = [f"m{i}" for i in range(rng.randint(2, 5))]
    styles = {
        module: rng.choice(("from", "from", "import", "alias")) for module in modules
    }
    program = _Program(lang, modules, [], styles)
    kinds = _kinds(lang)
    for index in range(rng.randint(5, 11)):
        kind = rng.choice(kinds) if index > 1 else "callback"
        program.fns.append(
            _Fn(
                name=f"{kind[:3]}{index}",
                module=rng.choice(modules),
                kind=kind,
                logs=rng.random() < 0.5,
            )
        )
    # At least one function a callback flows into, so most seeds exercise
    # the closure.
    if not any(fn.kind in _RECEIVERS or fn.kind == "maker" for fn in program.fns):
        program.fns.append(
            _Fn(f"inv{len(program.fns)}", rng.choice(modules), "invoker", logs=True)
        )
    for index, fn in enumerate(program.fns):
        _point(rng, program, fn, program.fns[:index])
    # Passers last too, so most programs hand a callback to the deepest chain.
    for _ in range(rng.randint(1, 3)):
        passer = _Fn(f"pas{len(program.fns)}", rng.choice(modules), "passer")
        _point(rng, program, passer, program.fns)
        program.fns.append(passer)
    for fn in program.fns:
        if fn.kind == "passer":
            fn.kind = f"passer:{_passer_shape(rng, program, fn)}"
    return program


def _passer_shape(rng: random.Random, program: _Program, fn: _Fn) -> str:
    target = program.by_name(fn.target)
    if target is None:
        return "direct"
    if target.kind in _FACTORIES:
        return rng.choice(("chained", "factory_alias"))
    if target.kind == "field_class":
        return rng.choice(("field_site", "field_fire"))
    if target.kind == "decorator":
        return "decorated"
    # A JS call hands no keyword, partial or method reference here, and a
    # Python one binds no `.bind`.
    unwritten = (
        {"keyword", "partial", "method_ref"} if program.lang == "js" else {"bound"}
    )
    return rng.choice(
        [s for s in _PASSERS if s not in {"chained", "factory_alias", *unwritten}]
    )


# --- Rendering ---------------------------------------------------------------


class _Names:
    """How module `module` writes a reference to each function."""

    def __init__(self, program: _Program, module: str) -> None:
        self.program = program
        self.module = module
        self.imports: dict[str, set[str]] = {}

    def ref(self, name: str) -> str:
        fn = self.program.by_name(name)
        if fn is None:
            # Deleted since: the reference dangles, as an edit leaves it.
            return name
        if fn.module == self.module:
            return name
        self.imports.setdefault(fn.module, set()).add(name)
        match self.program.styles[self.module]:
            case "import":
                return f"{fn.module}.{name}"
            case "alias":
                return f"{name}_al"
            case _:
                return name

    def log(self) -> str:
        self.imports.setdefault(_UTIL, set()).add("log")
        return f"{_UTIL}.log" if self.program.styles[self.module] == "import" else "log"


def _render_py(program: _Program, module: str) -> str:
    names = _Names(program, module)
    blocks: list[str] = []
    module_level: list[str] = []
    for fn in program.fns:
        if fn.module != module:
            continue
        log = f'    {names.log()}("{fn.name}")\n' if fn.logs else ""
        extra = "    _x = 1\n" if fn.body_extra else ""
        target = names.ref(fn.target) if fn.target else ""
        arg = names.ref(fn.arg) if fn.arg else ""
        match fn.kind:
            case "callback":
                body = f"def {fn.name}():\n{log}{extra}    return {len(fn.name)}\n"
            case "invoker":
                body = f"def {fn.name}(cb):\n{log}{extra}    return cb()\n"
            case "nested_invoker":
                body = (
                    f"def {fn.name}(cb):\n{log}{extra}"
                    "    def run():\n        return cb()\n\n    return run()\n"
                )
            case "forwarder":
                body = f"def {fn.name}(cb):\n{log}{extra}    return {target}(cb)\n"
            case "kw_forwarder":
                body = f"def {fn.name}(cb):\n{log}{extra}    return {target}(cb=cb)\n"
            case "maker":
                body = (
                    f"def {fn.name}():\n{log}{extra}"
                    f"    def {fn.name}_inner(cb):\n        return cb()\n\n"
                    f"    return {fn.name}_inner\n"
                )
            case "relay":
                body = f"def {fn.name}():\n{log}{extra}    return {target}()\n"
            case "getter":
                body = f"def {fn.name}():\n{log}{extra}    return {target}\n"
            case "field_class":
                body = (
                    f"class {fn.name}:\n"
                    "    def __init__(self, handler):\n"
                    "        self.handler = handler\n"
                )
            case "decorator":
                body = (
                    f"def {fn.name}(fn):\n{log}{extra}"
                    "    def wrapper(*args):\n        return fn(*args)\n\n"
                    "    return wrapper\n"
                )
            case passer if passer.startswith("passer:"):
                shape = passer.partition(":")[2]
                if not target or not arg:
                    body = f"def {fn.name}():\n{log}{extra}    return None\n"
                elif shape == "module_level":
                    module_level.append(f"{target}({arg})\n")
                    continue
                elif shape == "decorated":
                    body = f"@{target}\ndef {fn.name}():\n    return {arg}()\n"
                elif shape == "field_fire":
                    body = (
                        f"def {fn.name}(spec: {target}):\n{log}{extra}"
                        "    return spec.handler()\n"
                    )
                else:
                    call = {
                        "direct": f"return {target}({arg})",
                        "keyword": f"return {target}(cb={arg})",
                        "alias": f"b = {arg}\n    return {target}(b)",
                        "chained": f"return {target}()({arg})",
                        "factory_alias": f"f = {target}()\n    return f({arg})",
                        "method_ref": (
                            f"obj = _Holder{fn.name}()\n    return {target}(obj.meth)"
                        ),
                        "partial": f"return functools.partial({target}, {arg})()",
                        "field_site": f"return {target}(handler={arg})",
                    }[shape]
                    body = f"def {fn.name}():\n{log}{extra}    {call}\n"
                    if shape == "method_ref":
                        body = (
                            f"class _Holder{fn.name}:\n"
                            f"    def meth(self):\n        return {arg}()\n\n\n" + body
                        )
            case _:
                continue
        blocks.append(body)
    header: list[str] = []
    if any(fn.kind == "passer:partial" for fn in program.fns if fn.module == module):
        header.append("import functools\n")
    for imported in sorted(names.imports):
        members = sorted(names.imports[imported])
        match program.styles[module]:
            case "import":
                header.append(f"import {imported}\n")
            case "alias":
                header.append(
                    f"from {imported} import "
                    + ", ".join(
                        member if imported == _UTIL else f"{member} as {member}_al"
                        for member in members
                    )
                    + "\n"
                )
            case _:
                header.append(f"from {imported} import {', '.join(members)}\n")
    text = "".join(header)
    if blocks:
        text += ("\n\n" if text else "") + "\n\n".join(blocks)
    if module_level:
        text += ("\n\n" if text else "") + "".join(module_level)
    return text + "# edited\n" * program.comments.get(module, 0)


def _render_js(program: _Program, module: str) -> str:
    names = _Names(program, module)
    blocks: list[str] = []
    module_level: list[str] = []
    for fn in program.fns:
        if fn.module != module:
            continue
        log = f"  {names.log()}('{fn.name}');\n" if fn.logs else ""
        extra = "  const x = 1;\n" if fn.body_extra else ""
        target = names.ref(fn.target) if fn.target else ""
        arg = names.ref(fn.arg) if fn.arg else ""
        head = f"export function {fn.name}"
        match fn.kind:
            case "callback":
                body = f"{head}() {{\n{log}{extra}  return {len(fn.name)};\n}}\n"
            case "invoker":
                body = f"{head}(cb) {{\n{log}{extra}  return cb();\n}}\n"
            case "arrow_invoker":
                body = f"export const {fn.name} = (cb) => cb();\n"
            case "nested_invoker":
                body = (
                    f"{head}(cb) {{\n{log}{extra}"
                    "  function run() {\n    return cb();\n  }\n  return run();\n}\n"
                )
            case "forwarder":
                body = f"{head}(cb) {{\n{log}{extra}  return {target}(cb);\n}}\n"
            case "maker":
                body = (
                    f"{head}() {{\n{log}{extra}"
                    f"  function {fn.name}Inner(cb) {{\n    return cb();\n  }}\n"
                    f"  return {fn.name}Inner;\n}}\n"
                )
            case "getter":
                body = f"{head}() {{\n{log}{extra}  return {target};\n}}\n"
            case passer if passer.startswith("passer:"):
                shape = passer.partition(":")[2]
                if not target or not arg:
                    body = f"{head}() {{\n{log}{extra}  return null;\n}}\n"
                elif shape == "module_level":
                    module_level.append(f"{target}({arg});\n")
                    continue
                else:
                    call = {
                        "direct": f"return {target}({arg});",
                        "alias": f"const b = {arg};\n  return {target}(b);",
                        "chained": f"return {target}()({arg});",
                        "factory_alias": f"const f = {target}();\n  return f({arg});",
                        "bound": f"return {target}({arg}.bind(null));",
                    }[shape]
                    body = f"{head}() {{\n{log}{extra}  {call}\n}}\n"
            case _:
                continue
        blocks.append(body)
    header: list[str] = []
    for imported in sorted(names.imports):
        members = sorted(names.imports[imported])
        match program.styles[module]:
            case "import":
                header.append(f"import * as {imported} from './{imported}';\n")
            case "alias":
                header.append(
                    "import { "
                    + ", ".join(
                        member if imported == _UTIL else f"{member} as {member}_al"
                        for member in members
                    )
                    + f" }} from './{imported}';\n"
                )
            case _:
                header.append(
                    f"import {{ {', '.join(members)} }} from './{imported}';\n"
                )
    text = "".join(header)
    if blocks:
        text += ("\n" if text else "") + "\n".join(blocks)
    if module_level:
        text += ("\n" if text else "") + "".join(module_level)
    return text + "// edited\n" * program.comments.get(module, 0)


def _util(program: _Program) -> str:
    params = ", ".join(["msg", *(f"p{i}=0" for i in range(1, program.log_params))])
    if program.lang == "py":
        return f"def log({params}):\n    print(msg)\n"
    params = params.replace("=0", "")
    return f"export function log({params}) {{\n  console.log(msg);\n}}\n"


def render(program: _Program) -> dict[str, str]:
    ext = ".py" if program.lang == "py" else ".js"
    renderer = _render_py if program.lang == "py" else _render_js
    files = {f"{_UTIL}{ext}": _util(program)}
    for module in program.modules:
        files[f"{module}{ext}"] = renderer(program, module)
    return files


# --- Edits --------------------------------------------------------------------


def _copy(program: _Program) -> _Program:
    return replace(
        program,
        modules=list(program.modules),
        fns=[replace(fn) for fn in program.fns],
        styles=dict(program.styles),
        comments=dict(program.comments),
    )


def edit(program: _Program, seed: int) -> tuple[_Program, str]:
    """One random edit of `program`, and what it was."""
    rng = random.Random(seed * 7919 + 1)
    edited = _copy(program)
    choices = [
        "comment",
        "util_signature",
        "switch_arg",
        "switch_target",
        "drop_fn",
        "add_passer",
        "body_statement",
        "toggle_invoke",
        "delete_module",
        "move_fn",
        "rename_callback",
    ]
    while True:
        what = rng.choice(choices)
        if _apply(rng, edited, what):
            return edited, what


def _apply(rng: random.Random, program: _Program, what: str) -> bool:
    fns = program.fns
    match what:
        case "comment":
            module = rng.choice(program.modules)
            program.comments[module] = program.comments.get(module, 0) + 1
        case "util_signature":
            program.log_params += 1
        case "switch_arg":
            passers = [fn for fn in fns if fn.kind.startswith("passer:") and fn.arg]
            callbacks = _callbacks(program)
            if not passers or len(callbacks) < 2:
                return False
            fn = rng.choice(passers)
            fn.arg = rng.choice([c for c in callbacks if c != fn.arg])
        case "switch_target":
            movable = [
                fn
                for fn in fns
                if fn.target
                and fn.kind
                in ("forwarder", "kw_forwarder", "relay", "getter", "passer:direct")
                and len(_targets(program, fn)) > 1
            ]
            if not movable:
                return False
            fn = rng.choice(movable)
            fn.target = rng.choice([t for t in _targets(program, fn) if t != fn.target])
        case "drop_fn":
            fns.remove(rng.choice(fns))
        case "add_passer":
            receivers = [fn.name for fn in fns if fn.kind in _RECEIVERS]
            callbacks = _callbacks(program)
            if not receivers or not callbacks:
                return False
            fns.append(
                _Fn(
                    f"new{len(fns)}",
                    rng.choice(program.modules),
                    "passer:direct",
                    target=rng.choice(receivers),
                    arg=rng.choice(callbacks),
                )
            )
        case "body_statement":
            candidates = [fn for fn in fns if fn.kind not in ("field_class",)]
            if not candidates:
                return False
            rng.choice(candidates).body_extra = True
        case "toggle_invoke":
            toggles = {
                "invoker": "callback",
                "callback": "invoker",
                "forwarder": "invoker",
            }
            candidates = [fn for fn in fns if fn.kind in toggles]
            if not candidates:
                return False
            fn = rng.choice(candidates)
            fn.kind = toggles[fn.kind]
            if fn.kind != "forwarder":
                fn.target = ""
        case "delete_module":
            if len(program.modules) < 2:
                return False
            module = rng.choice(program.modules)
            program.modules.remove(module)
            program.fns = [fn for fn in fns if fn.module != module]
        case "move_fn":
            if len(program.modules) < 2:
                return False
            fn = rng.choice(fns)
            fn.module = rng.choice([m for m in program.modules if m != fn.module])
        case "rename_callback":
            callbacks = _callbacks(program)
            if not callbacks:
                return False
            old = rng.choice(callbacks)
            for fn in fns:
                if fn.name == old:
                    fn.name = f"{old}r"
                if fn.arg == old:
                    fn.arg = f"{old}r"
                if fn.target == old:
                    fn.target = f"{old}r"
    return True


# --- The check ----------------------------------------------------------------


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def differential_diff(seed: int, base: Path, modes: tuple[str, ...] = _MODES) -> str:
    """Empty when every mode's sync of seed `seed`'s edit equals a clean
    index of the edited tree; otherwise what differs, mode by mode."""
    program = generate(seed)
    edited, what = edit(program, seed)
    before, after = render(program), render(edited)
    report: list[str] = []
    for mode in modes:
        root = base / mode / PROJECT
        _write(root, before)
        store = _StatefulIngestor()
        indexer = _updater(store, root)
        indexer.run(force=True)
        touched = _apply_files(root, before, after)
        match mode:
            case "reingest":
                _updater(store, root).reingest([root / rel for rel in touched])
            case "reused":
                indexer.run()
            case _:
                _updater(store, root).run()
        if difference := _diff(_snapshot(store), _clean(root)):
            report.append(f"[{mode}] {what}\n{difference}")
    return "\n".join(report)


def describe(seed: int) -> str:
    """The program and edit of `seed`, for a failure to be read."""
    program = generate(seed)
    edited, what = edit(program, seed)
    before, after = render(program), render(edited)
    lines = [f"seed {seed}: {what}"]
    for rel in sorted({*before, *after}):
        lines.append(f"--- {rel}\n{before.get(rel, '<absent>')}")
        if before.get(rel) != after.get(rel):
            lines.append(f"+++ {rel}\n{after.get(rel, '<deleted>')}")
    return "\n".join(lines)


# The seeds CI runs; a hunt runs far more (`differential_diff` over a range).
SUITE_SEEDS = range(30)


@pytest.mark.parametrize("seed", SUITE_SEEDS)
def test_a_sync_equals_a_fresh_index_after_a_random_edit(
    temp_repo: Path, seed: int
) -> None:
    assert not (difference := differential_diff(seed, temp_repo)), (
        f"{describe(seed)}\n{difference}"
    )
