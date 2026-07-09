from pathlib import Path

from evals.cgr_graph import _capture


def _calls(tmp_path: Path, name: str, body: str) -> set[tuple[str, str]]:
    (tmp_path / name).write_text(body, encoding="utf-8")
    ingestor = _capture(tmp_path, "proj")
    return {(str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"}


def test_single_implementer_interface_call_dispatches_to_concrete(
    tmp_path: Path,
) -> None:
    # (H) `s.run()` on an interface-typed receiver: the STATIC callee is the
    # (H) interface method (removing its declaration breaks the call), and with
    # (H) exactly ONE implementer the concrete SvcImpl.run is what runs. Emit
    # (H) BOTH edges: interface-only binding orphaned the sole impl in Rust (no
    # (H) OVERRIDES there), impl-only binding orphaned the interface stub
    # (H) (gson's FieldNamingStrategy.translateName reported dead).
    calls = _calls(
        tmp_path,
        "Svc.java",
        "interface Svc { int run(); }\n"
        "class SvcImpl implements Svc { public int run() { return 1; } }\n"
        "class Client { int use(Svc s) { return s.run(); } }\n",
    )
    assert ("proj.Svc.Client.use(Svc)", "proj.Svc.SvcImpl.run()") in calls
    assert ("proj.Svc.Client.use(Svc)", "proj.Svc.Svc.run()") in calls


def test_multi_implementer_interface_call_stays_on_interface(tmp_path: Path) -> None:
    # (H) Two implementers -> ambiguous -> no concrete fan-out; the call stays on
    # (H) the interface method (no wrong-impl edge). Recall preserved.
    calls = _calls(
        tmp_path,
        "Svc.java",
        "interface Svc { int run(); }\n"
        "class A implements Svc { public int run() { return 1; } }\n"
        "class B implements Svc { public int run() { return 2; } }\n"
        "class Client { int use(Svc s) { return s.run(); } }\n",
    )
    assert ("proj.Svc.Client.use(Svc)", "proj.Svc.Svc.run()") in calls
    assert ("proj.Svc.Client.use(Svc)", "proj.Svc.A.run()") not in calls
    assert ("proj.Svc.Client.use(Svc)", "proj.Svc.B.run()") not in calls


def test_rust_single_trait_impl_dispatches_to_concrete(tmp_path: Path) -> None:
    # (H) The Rust `impl Trait for Type` path must also feed the implementers map:
    # (H) a trait-typed call keeps its static edge to the trait method AND fans
    # (H) out to the sole concrete impl (Rust has no OVERRIDES edges to revive it).
    calls = _calls(
        tmp_path,
        "m.rs",
        "trait Svc { fn run(&self) -> i32; }\n"
        "struct SvcImpl;\n"
        "impl Svc for SvcImpl { fn run(&self) -> i32 { 1 } }\n"
        "fn use_it(s: &dyn Svc) -> i32 { s.run() }\n",
    )
    assert ("proj.m.use_it", "proj.m.SvcImpl.run") in calls
    assert ("proj.m.use_it", "proj.m.Svc.run") in calls


def test_cross_file_interface_field_keeps_interface_edge(tmp_path: Path) -> None:
    # (H) The gson regression shape: a field declared with a CROSS-FILE interface
    # (H) type (`private final FieldNamingStrategy fieldNamingPolicy;`) whose sole
    # (H) implementer is an enum. #665's deferred-inherits resolver keys
    # (H) interface_implementers by the RESOLVED interface qn, so the sole-impl
    # (H) redirect started firing and stole the interface stub's only CALLS edge
    # (H) -> FieldNamingStrategy.translateName reported dead. Both edges must exist.
    pkg = tmp_path / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "Namer.java").write_text(
        "package com.example;\n"
        "public interface Namer { String translateName(String f); }\n",
        encoding="utf-8",
    )
    (pkg / "NamerPolicy.java").write_text(
        "package com.example;\n"
        "public enum NamerPolicy implements Namer {\n"
        "  IDENTITY {\n"
        "    @Override public String translateName(String f) { return f; }\n"
        "  };\n"
        "}\n",
        encoding="utf-8",
    )
    (pkg / "Factory.java").write_text(
        "package com.example;\n"
        "public final class Factory {\n"
        "  private final Namer fieldNamingPolicy;\n"
        "  public Factory(Namer fieldNamingPolicy) {\n"
        "    this.fieldNamingPolicy = fieldNamingPolicy;\n"
        "  }\n"
        "  private String getFieldName(String f) {\n"
        "    return fieldNamingPolicy.translateName(f);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"
    }
    assert (
        "proj.com.example.Factory.Factory.getFieldName(String)",
        "proj.com.example.Namer.Namer.translateName(String)",
    ) in calls, sorted(t for _f, t in calls if "translateName" in t)
    assert (
        "proj.com.example.Factory.Factory.getFieldName(String)",
        "proj.com.example.NamerPolicy.NamerPolicy.translateName(String)",
    ) in calls, sorted(t for _f, t in calls if "translateName" in t)
