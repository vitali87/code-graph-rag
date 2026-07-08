# (H) A Java anonymous class (`new Base(){ @Override m(){} }`) is not modelled as a
# (H) subclass, so its override methods register under the enclosing class with no
# (H) OVERRIDES edge and look dead even though the base method is called and dispatch
# (H) can land on them (gson's JavaTimeTypeAdapters `create`/`integerValues`). Recording
# (H) the anon override and emitting OVERRIDES to the base, plus override-reachability,
# (H) keeps them live when the base is reachable.
from __future__ import annotations

from pathlib import Path

from evals.dead_code import cgr_dead_code, default_dead_code_config


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "janon"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    for name, body in files.items():
        (pkg / name).write_text(f"package com.example;\n{body}\n", encoding="utf-8")
    return root


def test_anonymous_override_of_called_base_is_not_dead(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "Base.java": (
                "public abstract class Base {\n"
                "  public abstract int make(int[] v);\n"
                "  public int run(int[] v) { return make(v); }\n"
                "}\n"
            ),
            "Holder.java": (
                "public class Holder {\n"
                "  public static final Base B = new Base() {\n"
                "    @Override public int make(int[] v) { return v[0]; }\n"
                "  };\n"
                "}\n"
            ),
        },
    )
    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    # (H) run() is public (root) and calls make() -> Base.make live; the anonymous
    # (H) override in Holder overrides Base.make, so it is a live dispatch target.
    anon_override = [d for d in dead if d.endswith(".make(int[])") and ".Holder" in d]
    assert not anon_override, f"anon override reported dead: {sorted(dead)}"
