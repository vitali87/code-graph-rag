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
