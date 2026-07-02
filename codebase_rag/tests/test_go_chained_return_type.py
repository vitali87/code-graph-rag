from pathlib import Path

from codebase_rag.parsers.call_resolver import _split_receiver_chain
from evals.cgr_graph import _capture


def test_split_receiver_chain_ignores_dots_inside_arguments() -> None:
    # (H) `.` inside call args / index / generic must not split the receiver chain.
    assert _split_receiver_chain("c.Find(1.5)") == ["c", "Find(1.5)"]
    assert _split_receiver_chain("a.b(x.y).c") == ["a", "b(x.y)", "c"]
    assert _split_receiver_chain("m[k.v].get") == ["m[k.v]", "get"]
    assert _split_receiver_chain("c.Root") == ["c", "Root"]


def _make(root: Path) -> None:
    # (H) `c.Root().Run()`: Root() returns *Command, so Run() must resolve on
    # (H) Command. cgr infers the receiver `c` type (Command) already; the missing
    # (H) piece is the RETURN type of Root() feeding the next hop. This is the cobra
    # (H) `cmd.Root().GenZshCompletion()` gap.
    (root / "m.go").write_text(
        "package p\n"
        "type Command struct{}\n"
        "func (c *Command) Root() *Command { return c }\n"
        "func (c *Command) Run() int { return 1 }\n"
        "func (c *Command) Use() int { return c.Root().Run() }\n",
        encoding="utf-8",
    )


def test_go_chained_return_type_resolves_second_hop(tmp_path: Path) -> None:
    _make(tmp_path)
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    # (H) first hop already resolves via receiver-type inference.
    assert ("proj.m.Command.Use", "proj.m.Command.Root") in calls
    # (H) second hop needs Root()'s return type (*Command) to resolve Run().
    assert ("proj.m.Command.Use", "proj.m.Command.Run") in calls


def _calls(tmp_path: Path, body: str) -> set[tuple[str, str]]:
    (tmp_path / "m.go").write_text(body, encoding="utf-8")
    ingestor = _capture(tmp_path, "proj")
    return {(str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"}


def test_chained_call_on_container_return_does_not_misresolve(tmp_path: Path) -> None:
    # (H) Kids() returns []Command (a slice); a chained `.Run()` is called on the
    # (H) slice, NOT on a Command, so it must NOT resolve to Command.Run. Unwrapping
    # (H) the container to its element type would emit a false edge.
    calls = _calls(
        tmp_path,
        "package p\n"
        "type Command struct{}\n"
        "func (c *Command) Kids() []Command { return nil }\n"
        "func (c *Command) Run() int { return 1 }\n"
        "func (c *Command) Use() int { return c.Kids().Run() }\n",
    )
    assert ("proj.m.Command.Use", "proj.m.Command.Run") not in calls


def test_chained_call_with_dotted_argument_resolves(tmp_path: Path) -> None:
    # (H) A dotted call argument (`1.5`) must not break the chain split: Find(1.5)
    # (H) returns *Command, so Run() still resolves.
    calls = _calls(
        tmp_path,
        "package p\n"
        "type Command struct{}\n"
        "func (c *Command) Find(x float64) *Command { return c }\n"
        "func (c *Command) Run() int { return 1 }\n"
        "func (c *Command) Use() int { return c.Find(1.5).Run() }\n",
    )
    assert ("proj.m.Command.Use", "proj.m.Command.Run") in calls
