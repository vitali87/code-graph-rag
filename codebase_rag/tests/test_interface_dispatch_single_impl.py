from pathlib import Path

from evals.cgr_graph import _capture


def _calls(tmp_path: Path, name: str, body: str) -> set[tuple[str, str]]:
    (tmp_path / name).write_text(body, encoding="utf-8")
    ingestor = _capture(tmp_path, "proj")
    return {(str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"}


def test_single_implementer_interface_call_dispatches_to_concrete(
    tmp_path: Path,
) -> None:
    # (H) `s.run()` on an interface-typed receiver resolves to the interface method;
    # (H) since Svc has exactly ONE implementer, redirect to the concrete SvcImpl.run
    # (H) (the method that actually runs).
    calls = _calls(
        tmp_path,
        "Svc.java",
        "interface Svc { int run(); }\n"
        "class SvcImpl implements Svc { public int run() { return 1; } }\n"
        "class Client { int use(Svc s) { return s.run(); } }\n",
    )
    assert ("proj.Svc.Client.use(Svc)", "proj.Svc.SvcImpl.run()") in calls
    # (H) it must NOT stay on the interface method when a unique impl exists.
    assert ("proj.Svc.Client.use(Svc)", "proj.Svc.Svc.run()") not in calls


def test_multi_implementer_interface_call_stays_on_interface(tmp_path: Path) -> None:
    # (H) Two implementers -> ambiguous -> no redirect; the call stays on the
    # (H) interface method (no wrong-impl edge). Recall preserved.
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
    # (H) The Rust `impl Trait for Type` path must also feed the implementers map, so
    # (H) a trait-typed call redirects to the sole concrete impl.
    calls = _calls(
        tmp_path,
        "m.rs",
        "trait Svc { fn run(&self) -> i32; }\n"
        "struct SvcImpl;\n"
        "impl Svc for SvcImpl { fn run(&self) -> i32 { 1 } }\n"
        "fn use_it(s: &dyn Svc) -> i32 { s.run() }\n",
    )
    assert ("proj.m.use_it", "proj.m.SvcImpl.run") in calls
