from pathlib import Path

from evals.cgr_graph import _capture


def _make(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.js").write_text(
        "function helper() { return 1; }\n"
        "function register(name, cb) { return cb; }\n"
        "class Plugin {\n"
        "  apply(compiler) {\n"
        "    register('x', (compilation) => {\n"
        "      return helper();\n"
        "    });\n"
        "  }\n"
        "}\n"
        "module.exports = Plugin;\n",
        encoding="utf-8",
    )


def test_call_inside_anonymous_callback_attributes_to_enclosing_method(
    tmp_path: Path,
) -> None:
    # (H) A call inside an anonymous arrow passed as an argument
    # (H) (`register('x', (c) => { helper() })`) has no name and no binding, so the
    # (H) call loop skipped the arrow AND _calls_owned_by excluded its calls from
    # (H) the enclosing method, dropping the call entirely. It must instead bubble
    # (H) up to the nearest named scope (Plugin.apply). This callback pattern is
    # (H) pervasive in real JS (e.g. webpack's hooks.tap(name, (x) => {...})).
    _make(tmp_path)
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    assert ("proj.plugin.Plugin.apply", "proj.plugin.helper") in calls
