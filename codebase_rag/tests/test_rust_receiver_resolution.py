from pathlib import Path

from evals.cgr_graph import _capture


def _calls(tmp_path: Path) -> set[tuple[str, str]]:
    ingestor = _capture(tmp_path, "crate")
    return {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }


def _make_crate(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lib.rs").write_text(body, encoding="utf-8")


# (H) Every fixture defines the called method name on more than one type (an `Aaa`
# (H) decoy sorting before the real type) so the name-only trie fallback is ambiguous
# (H) and would pick the wrong `Aaa` alphabetically: only real receiver-type inference
# (H) produces the correct edge. Mirrors mini-redis, where `apply`/`run`/`new`/
# (H) `into_frame` live on many command types.


def test_self_receiver_dispatch(tmp_path: Path) -> None:
    # (H) `self.accept()` inside a method must resolve to the impl target's method.
    _make_crate(
        tmp_path,
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    fn accept(&self) -> i32 { 2 }\n"
        "}\n"
        "pub struct Listener {}\n"
        "impl Listener {\n"
        "    fn accept(&self) -> i32 { 1 }\n"
        "    fn run(&self) -> i32 { self.accept() }\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.Listener.run", "crate.lib.Listener.accept") in calls
    assert ("crate.lib.Listener.run", "crate.lib.Aaa.accept") not in calls


def test_struct_literal_binding_dispatch(tmp_path: Path) -> None:
    # (H) `let s = Listener {}; s.run()` binds s to Listener via the struct literal.
    _make_crate(
        tmp_path,
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    fn run(&self) -> i32 { 2 }\n"
        "}\n"
        "pub struct Listener {}\n"
        "impl Listener {\n"
        "    fn run(&self) -> i32 { 1 }\n"
        "}\n"
        "pub fn go() -> i32 {\n"
        "    let mut server = Listener {};\n"
        "    server.run()\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.go", "crate.lib.Listener.run") in calls
    assert ("crate.lib.go", "crate.lib.Aaa.run") not in calls


def test_field_type_receiver_dispatch(tmp_path: Path) -> None:
    # (H) `self.shutdown.is_shutdown()` resolves through the struct field's type.
    _make_crate(
        tmp_path,
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    fn is_shutdown(&self) -> bool { false }\n"
        "}\n"
        "pub struct Shutdown {}\n"
        "impl Shutdown {\n"
        "    fn is_shutdown(&self) -> bool { true }\n"
        "}\n"
        "pub struct Handler { shutdown: Shutdown }\n"
        "impl Handler {\n"
        "    fn run(&self) -> bool { self.shutdown.is_shutdown() }\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.Handler.run", "crate.lib.Shutdown.is_shutdown") in calls
    assert ("crate.lib.Handler.run", "crate.lib.Aaa.is_shutdown") not in calls


def test_guard_deref_field_hop_binding_dispatch(tmp_path: Path) -> None:
    # (H) `let state = self.shared.state.lock().unwrap()` inside impl Db: type `state`
    # (H) by hopping self -> Db, field shared: Arc<Shared> -> Shared, field
    # (H) state: Mutex<State> -> State (deref through Arc/Mutex), then lock()/unwrap()
    # (H) as guard-accessor identities -> State. `state.next_expiration()` must then
    # (H) dispatch to State.next_expiration (mini-redis Db.set).
    _make_crate(
        tmp_path,
        "use std::sync::{Arc, Mutex};\n"
        "pub struct Aaa {}\n"
        "impl Aaa {\n    fn next_expiration(&self) -> i32 { 2 }\n}\n"
        "pub struct State {}\n"
        "impl State {\n    fn next_expiration(&self) -> i32 { 1 }\n}\n"
        "pub struct Shared { state: Mutex<State> }\n"
        "pub struct Db { shared: Arc<Shared> }\n"
        "impl Db {\n"
        "    fn set(&self) -> i32 {\n"
        "        let state = self.shared.state.lock().unwrap();\n"
        "        state.next_expiration()\n"
        "    }\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.Db.set", "crate.lib.State.next_expiration") in calls
    assert ("crate.lib.Db.set", "crate.lib.Aaa.next_expiration") not in calls


def test_guard_wrapper_local_not_erased_to_inner(tmp_path: Path) -> None:
    # (H) A Mutex/RwLock does NOT deref-coerce: a bare `let m: Mutex<Inner>` receiver
    # (H) can only reach Inner via a lock/borrow hop, so `m` must stay typed as the
    # (H) wrapper -- NOT erased to Inner (which would mis-resolve a direct `m.work()`
    # (H) to Inner.work). `work` is defined on two types so the trie can't mask a
    # (H) precise mis-resolution.
    _make_crate(
        tmp_path,
        "use std::sync::Mutex;\n"
        "pub struct Aaa {}\n"
        "impl Aaa {\n    fn work(&self) -> i32 { 2 }\n}\n"
        "pub struct Inner {}\n"
        "impl Inner {\n    fn work(&self) -> i32 { 1 }\n}\n"
        "pub fn go(m: Mutex<Inner>) -> i32 {\n"
        "    m.work()\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    # (H) m is a Mutex receiver; a direct m.work() must NOT bind to Inner.work
    assert ("crate.lib.go", "crate.lib.Inner.work") not in calls


def test_let_assoc_call_return_type_dispatch(tmp_path: Path) -> None:
    # (H) `let cmd = Command::from_frame(f); cmd.apply()` types cmd from the
    # (H) associated function's return type (Command).
    _make_crate(
        tmp_path,
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    fn apply(&self) -> i32 { 2 }\n"
        "}\n"
        "pub struct Command {}\n"
        "impl Command {\n"
        "    pub fn from_frame(f: i32) -> Command { Command {} }\n"
        "    pub fn apply(&self) -> i32 { 1 }\n"
        "}\n"
        "pub fn go() -> i32 {\n"
        "    let cmd = Command::from_frame(0);\n"
        "    cmd.apply()\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.go", "crate.lib.Command.apply") in calls
    assert ("crate.lib.go", "crate.lib.Aaa.apply") not in calls


def test_let_assoc_call_result_wrapper_return_type(tmp_path: Path) -> None:
    # (H) A Result<Command> return type is stripped to Command so the unwrapped
    # (H) local still dispatches.
    _make_crate(
        tmp_path,
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    fn apply(&self) -> i32 { 2 }\n"
        "}\n"
        "pub struct Command {}\n"
        "impl Command {\n"
        "    pub fn from_frame(f: i32) -> crate::Result<Command> { Ok(Command {}) }\n"
        "    pub fn apply(&self) -> i32 { 1 }\n"
        "}\n"
        "pub fn go() -> i32 {\n"
        "    let cmd = Command::from_frame(0).unwrap();\n"
        "    cmd.apply()\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.go", "crate.lib.Command.apply") in calls
    assert ("crate.lib.go", "crate.lib.Aaa.apply") not in calls


def test_enum_match_binding_dispatch(tmp_path: Path) -> None:
    # (H) `Get(cmd) => cmd.apply()` binds cmd to the variant's payload type (Get).
    _make_crate(
        tmp_path,
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    fn apply(&self) -> i32 { 2 }\n"
        "}\n"
        "pub struct Get {}\n"
        "impl Get {\n"
        "    fn apply(&self) -> i32 { 1 }\n"
        "}\n"
        "pub enum Command { Get(Get) }\n"
        "impl Command {\n"
        "    fn apply(&self) -> i32 {\n"
        "        use Command::*;\n"
        "        match self {\n"
        "            Get(cmd) => cmd.apply(),\n"
        "        }\n"
        "    }\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.Command.apply", "crate.lib.Get.apply") in calls
    # (H) must not mis-resolve to the enclosing type's same-named method
    assert ("crate.lib.Command.apply", "crate.lib.Command.apply") not in calls


def test_enum_match_multiarm_binding_shadowing(tmp_path: Path) -> None:
    # (H) Every arm reuses the binding name `cmd` for a DIFFERENT variant type. A flat
    # (H) var_types map keeps only the last arm's type, so all `cmd.apply()` collapse
    # (H) to the last variant. Each arm's `cmd.apply()` must dispatch to ITS OWN
    # (H) variant's method (mini-redis Command.apply dispatch).
    _make_crate(
        tmp_path,
        "pub struct Get {}\n"
        "impl Get {\n    fn apply(&self) -> i32 { 1 }\n}\n"
        "pub struct Set {}\n"
        "impl Set {\n    fn apply(&self) -> i32 { 2 }\n}\n"
        "pub struct Ping {}\n"
        "impl Ping {\n    fn apply(&self) -> i32 { 3 }\n}\n"
        "pub enum Command { Get(Get), Set(Set), Ping(Ping) }\n"
        "impl Command {\n"
        "    fn apply(&self) -> i32 {\n"
        "        use Command::*;\n"
        "        match self {\n"
        "            Get(cmd) => cmd.apply(),\n"
        "            Set(cmd) => cmd.apply(),\n"
        "            Ping(cmd) => cmd.apply(),\n"
        "        }\n"
        "    }\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.Command.apply", "crate.lib.Get.apply") in calls
    assert ("crate.lib.Command.apply", "crate.lib.Set.apply") in calls
    assert ("crate.lib.Command.apply", "crate.lib.Ping.apply") in calls


def test_nested_match_binding_not_leaked_to_outer_arm(tmp_path: Path) -> None:
    # (H) A nested `match inner { Foo(x) => ... }` rebinds `x` only within its own arm.
    # (H) The outer arm's `x.tag()` (x = the Bar param) must stay Bar.tag -- the nested
    # (H) Foo binding must NOT be scoped to the whole outer arm (which would mis-overlay
    # (H) the outer call to Foo.tag).
    _make_crate(
        tmp_path,
        "pub struct Bar {}\n"
        "impl Bar {\n    fn tag(&self) -> i32 { 1 }\n}\n"
        "pub struct Foo {}\n"
        "impl Foo {\n    fn tag(&self) -> i32 { 2 }\n}\n"
        "pub enum Inner { Foo(Foo) }\n"
        "pub enum E { A(Bar) }\n"
        "impl E {\n"
        "    fn run(&self, x: Bar, inner: Inner) -> i32 {\n"
        "        use Inner::*;\n"
        "        match self {\n"
        "            E::A(_) => {\n"
        "                match inner {\n"
        "                    Foo(x) => x.tag(),\n"
        "                }\n"
        "                x.tag()\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    # (H) nested arm's x:Foo resolves to Foo.tag; outer arm's x (param Bar) to Bar.tag
    assert ("crate.lib.E.run", "crate.lib.Foo.tag") in calls
    assert ("crate.lib.E.run", "crate.lib.Bar.tag") in calls


def test_assoc_fn_chain_dispatch(tmp_path: Path) -> None:
    # (H) `Ping::new(msg).into_frame()` resolves into_frame on the type Ping::new
    # (H) returns (Ping).
    _make_crate(
        tmp_path,
        "pub struct Frame {}\n"
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    pub fn new(msg: i32) -> Aaa { Aaa {} }\n"
        "    pub fn into_frame(self) -> Frame { Frame {} }\n"
        "}\n"
        "pub struct Ping {}\n"
        "impl Ping {\n"
        "    pub fn new(msg: i32) -> Ping { Ping {} }\n"
        "    pub fn into_frame(self) -> Frame { Frame {} }\n"
        "}\n"
        "pub fn go() -> i32 {\n"
        "    let frame = Ping::new(0).into_frame();\n"
        "    1\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.go", "crate.lib.Ping.new") in calls
    assert ("crate.lib.go", "crate.lib.Ping.into_frame") in calls
    assert ("crate.lib.go", "crate.lib.Aaa.into_frame") not in calls


def test_imported_assoc_call_return_type_dispatch(tmp_path: Path) -> None:
    # (H) `use crate::cmd::Command; let cmd = Command::from_frame(f); cmd.apply()`:
    # (H) the type is IMPORTED, so its import target is a raw `::`-path, not a registry
    # (H) qn. The call-return binding must resolve it to the real class node
    # (H) (crate.cmd.Command) to look up from_frame's recorded return type -- else it
    # (H) falls back to the ambiguous trie path.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib.rs").write_text("pub mod cmd;\npub mod app;\n", encoding="utf-8")
    (tmp_path / "cmd.rs").write_text(
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    pub fn apply(&self) -> i32 { 2 }\n"
        "}\n"
        "pub struct Command {}\n"
        "impl Command {\n"
        "    pub fn from_frame(f: i32) -> Command { Command {} }\n"
        "    pub fn apply(&self) -> i32 { 1 }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "app.rs").write_text(
        "use crate::cmd::Command;\n"
        "pub fn go() -> i32 {\n"
        "    let cmd = Command::from_frame(0);\n"
        "    cmd.apply()\n"
        "}\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert ("crate.app.go", "crate.cmd.Command.apply") in calls
    assert ("crate.app.go", "crate.cmd.Aaa.apply") not in calls


def test_reference_return_type_chained_dispatch(tmp_path: Path) -> None:
    # (H) A method returning a reference (`fn frame(&self) -> &Frame`) must still
    # (H) yield the referent type so a chained call (`self.frame().push_int()`)
    # (H) resolves to Frame.push_int, not the ambiguous trie pick.
    _make_crate(
        tmp_path,
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    fn push_int(&self) -> i32 { 2 }\n"
        "}\n"
        "pub struct Frame {}\n"
        "impl Frame {\n"
        "    fn push_int(&self) -> i32 { 1 }\n"
        "}\n"
        "pub struct Holder {}\n"
        "impl Holder {\n"
        "    fn frame(&self) -> &Frame { &Frame {} }\n"
        "    fn go(&self) -> i32 { self.frame().push_int() }\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.Holder.go", "crate.lib.Frame.push_int") in calls
    assert ("crate.lib.Holder.go", "crate.lib.Aaa.push_int") not in calls


def test_imported_type_disambiguated_by_path(tmp_path: Path) -> None:
    # (H) Two `Command` types in different modules whose `mk()` returns DIFFERENT
    # (H) types (cmd.Command -> Real, other.Command -> Fake). `use crate::cmd::Command`
    # (H) must resolve the call-return base to crate.cmd.Command so `x.run()` lands on
    # (H) Real.run. A simple-name-only match would pick the alphabetically-first
    # (H) crate.aaa.Command, type x as Fake, and mis-resolve to Fake.run.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib.rs").write_text(
        "pub mod aaa;\npub mod cmd;\npub mod types;\npub mod app;\n",
        encoding="utf-8",
    )
    (tmp_path / "types.rs").write_text(
        "pub struct Real {}\n"
        "impl Real {\n"
        "    pub fn run(&self) -> i32 { 1 }\n"
        "}\n"
        "pub struct Fake {}\n"
        "impl Fake {\n"
        "    pub fn run(&self) -> i32 { 2 }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "aaa.rs").write_text(
        "use crate::types::Fake;\n"
        "pub struct Command {}\n"
        "impl Command {\n"
        "    pub fn mk() -> Fake { Fake {} }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "cmd.rs").write_text(
        "use crate::types::Real;\n"
        "pub struct Command {}\n"
        "impl Command {\n"
        "    pub fn mk() -> Real { Real {} }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "app.rs").write_text(
        "use crate::cmd::Command;\n"
        "pub fn go() -> i32 {\n"
        "    let x = Command::mk();\n"
        "    x.run()\n"
        "}\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert ("crate.app.go", "crate.types.Real.run") in calls
    assert ("crate.app.go", "crate.types.Fake.run") not in calls


def test_fully_qualified_inline_assoc_call_dispatch(tmp_path: Path) -> None:
    # (H) A fully-qualified inline associated call with NO `use` import
    # (H) (`let x = crate::cmd::Command::mk()`) must keep the qualified path so the
    # (H) return-type base resolves to crate.cmd.Command (mk -> Real), not the
    # (H) alphabetically-first crate.aaa.Command (mk -> Fake).
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib.rs").write_text(
        "pub mod aaa;\npub mod cmd;\npub mod types;\npub mod app;\n",
        encoding="utf-8",
    )
    (tmp_path / "types.rs").write_text(
        "pub struct Real {}\n"
        "impl Real {\n"
        "    pub fn run(&self) -> i32 { 1 }\n"
        "}\n"
        "pub struct Fake {}\n"
        "impl Fake {\n"
        "    pub fn run(&self) -> i32 { 2 }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "aaa.rs").write_text(
        "use crate::types::Fake;\n"
        "pub struct Command {}\n"
        "impl Command {\n"
        "    pub fn mk() -> Fake { Fake {} }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "cmd.rs").write_text(
        "use crate::types::Real;\n"
        "pub struct Command {}\n"
        "impl Command {\n"
        "    pub fn mk() -> Real { Real {} }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "app.rs").write_text(
        "pub fn go() -> i32 {\n"
        "    let x = crate::cmd::Command::mk();\n"
        "    x.run()\n"
        "}\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert ("crate.app.go", "crate.types.Real.run") in calls
    assert ("crate.app.go", "crate.types.Fake.run") not in calls


def test_macro_buried_receiver_call_dispatch(tmp_path: Path) -> None:
    # (H) A `receiver.method()` call buried inside a macro token stream
    # (H) (`sel! { res = server.run() => {} }`, like tokio::select!) loses its
    # (H) field_expression structure -- the receiver `server` becomes a loose token.
    # (H) The reconstructed call must be `server.run` so it dispatches to the local's
    # (H) type (Listener.run), NOT the same-module free fn `run` (a false self-edge
    # (H) that severed the whole server/command cluster in mini-redis).
    _make_crate(
        tmp_path,
        "pub struct Listener {}\n"
        "impl Listener {\n"
        "    fn run(&self) -> i32 { 1 }\n"
        "}\n"
        "pub fn run(server: Listener) -> i32 {\n"
        "    sel! {\n"
        "        res = server.run() => {}\n"
        "    }\n"
        "    0\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.run", "crate.lib.Listener.run") in calls
    # (H) must not mis-resolve to the same-module free function (self-edge)
    assert ("crate.lib.run", "crate.lib.run") not in calls


def test_macro_buried_field_hop_call_dispatch(tmp_path: Path) -> None:
    # (H) A field-hop receiver buried in a macro (`self.shutdown.recv()`) must
    # (H) reconstruct the full chain so it hops self -> field type -> method.
    _make_crate(
        tmp_path,
        "pub struct Aaa {}\n"
        "impl Aaa {\n"
        "    fn recv(&self) -> i32 { 2 }\n"
        "}\n"
        "pub struct Shutdown {}\n"
        "impl Shutdown {\n"
        "    fn recv(&self) -> i32 { 1 }\n"
        "}\n"
        "pub struct Handler { shutdown: Shutdown }\n"
        "impl Handler {\n"
        "    fn run(&self) -> i32 {\n"
        "        sel! {\n"
        "            x = self.shutdown.recv() => {}\n"
        "        }\n"
        "        0\n"
        "    }\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.lib.Handler.run", "crate.lib.Shutdown.recv") in calls
    assert ("crate.lib.Handler.run", "crate.lib.Aaa.recv") not in calls
