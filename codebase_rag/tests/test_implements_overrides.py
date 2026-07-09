# (H) An `implements`/trait-impl relationship never reached class_inheritance
# (H) (relationships.py stores only the superclass; interfaces feed
# (H) interface_implementers and the IMPLEMENTS edge), so Pass-4 override
# (H) detection walked past every interface and NO interface/trait
# (H) implementation ever got an OVERRIDES edge -- named Java classes, enum
# (H) constant bodies, and Rust trait impls alike (anonymous classes had their
# (H) own dedicated pass). Dead-code override expansion walks overridden ->
# (H) overriders, so with >1 implementers (no sole-impl CALLS fan-out) a live
# (H) interface/trait method revived nothing and non-public impl methods
# (H) (Rust trait impls) reported dead. The override walk must include the
# (H) implemented interfaces.
from __future__ import annotations

from pathlib import Path

from evals.cgr_graph import _capture
from evals.dead_code import cgr_dead_code, default_dead_code_config


def _java_project(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "impls"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    for name, body in files.items():
        (pkg / name).write_text(f"package com.example;\n{body}", encoding="utf-8")
    return root


def _overrides(root: Path) -> set[tuple[str, str]]:
    ingestor = _capture(root, "proj")
    return {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "OVERRIDES"
    }


NAMER = "public interface Namer { String translateName(String f); }\n"


def test_java_class_implements_interface_gets_overrides_edge(tmp_path: Path) -> None:
    # (H) The plain cross-file shape: the impl method must OVERRIDES the
    # (H) interface method it implements, exactly as an `extends` override would.
    overrides = _overrides(
        _java_project(
            tmp_path,
            {
                "Namer.java": NAMER,
                "Other.java": (
                    "public class OtherNamer implements Namer {\n"
                    "  @Override public String translateName(String f)"
                    " { return f; }\n"
                    "}\n"
                ),
            },
        )
    )
    assert (
        "proj.com.example.Other.OtherNamer.translateName(String)",
        "proj.com.example.Namer.Namer.translateName(String)",
    ) in overrides, sorted(overrides)


def test_java_enum_constant_body_override_gets_overrides_edge(tmp_path: Path) -> None:
    # (H) Enum constant bodies hoist their methods to enum level (`P.m(Sig)` plus
    # (H) `P.m(Sig)@line` duplicates); both hoisted variants must OVERRIDES the
    # (H) interface method (the @line variant via the name+arity fallback).
    overrides = _overrides(
        _java_project(
            tmp_path,
            {
                "Namer.java": NAMER,
                "NamerPolicy.java": (
                    "public enum NamerPolicy implements Namer {\n"
                    "  IDENTITY {\n"
                    "    @Override public String translateName(String f)"
                    " { return f; }\n"
                    "  },\n"
                    "  UPPER {\n"
                    "    @Override public String translateName(String f)"
                    " { return f.toUpperCase(); }\n"
                    "  };\n"
                    "}\n"
                ),
            },
        )
    )
    interface_qn = "proj.com.example.Namer.Namer.translateName(String)"
    enum_prefix = "proj.com.example.NamerPolicy.NamerPolicy.translateName(String)"
    assert (enum_prefix, interface_qn) in overrides, sorted(overrides)
    assert any(
        f.startswith(f"{enum_prefix}@") and t == interface_qn for f, t in overrides
    ), sorted(overrides)


RUST_TWO_IMPLS = (
    "trait Svc { fn run(&self) -> i32; }\n"
    "struct Zed;\n"
    "impl Svc for Zed { fn run(&self) -> i32 { 1 } }\n"
    "struct Zeta;\n"
    "impl Svc for Zeta { fn run(&self) -> i32 { 2 } }\n"
    "pub fn use_it(s: &dyn Svc) -> i32 { s.run() }\n"
)


def test_rust_trait_impl_gets_overrides_edge(tmp_path: Path) -> None:
    # (H) `impl Svc for Zed` feeds the same implementers map, so each impl's
    # (H) method must OVERRIDES the trait method regardless of impl count.
    root = tmp_path / "rimpl"
    root.mkdir()
    (root / "m.rs").write_text(RUST_TWO_IMPLS, encoding="utf-8")
    overrides = _overrides(root)
    assert ("proj.m.Zed.run", "proj.m.Svc.run") in overrides, sorted(overrides)
    assert ("proj.m.Zeta.run", "proj.m.Svc.run") in overrides, sorted(overrides)


def test_rust_multi_impl_trait_methods_not_dead(tmp_path: Path) -> None:
    # (H) The live false-positive class (mini-redis shape): the trait-typed call
    # (H) binds to the trait method, the impls are non-pub (not exported roots)
    # (H) and >1 (no sole-impl CALLS fan-out), so only OVERRIDES expansion from
    # (H) the live trait method can keep them off the dead-code report.
    root = tmp_path / "rdead"
    root.mkdir()
    (root / "m.rs").write_text(RUST_TWO_IMPLS, encoding="utf-8")
    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    impl_dead = [d for d in dead if d.endswith(".run")]
    assert not impl_dead, sorted(dead)
