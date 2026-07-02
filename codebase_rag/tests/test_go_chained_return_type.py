from pathlib import Path

from evals.cgr_graph import _capture


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
