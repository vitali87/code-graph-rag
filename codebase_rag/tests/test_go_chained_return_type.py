from pathlib import Path

from codebase_rag.parsers.call_resolver import _split_receiver_chain
from evals.cgr_graph import _capture


def test_split_receiver_chain_ignores_dots_inside_arguments() -> None:
    # `.` inside call args / index / generic must not split the receiver chain.
    assert _split_receiver_chain("c.Find(1.5)") == ["c", "Find(1.5)"]
    assert _split_receiver_chain("a.b(x.y).c") == ["a", "b(x.y)", "c"]
    assert _split_receiver_chain("m[k.v].get") == ["m[k.v]", "get"]
    assert _split_receiver_chain("c.Root") == ["c", "Root"]


def _make(root: Path) -> None:
    # `c.Root().Run()`: Root() returns *Command, so Run() must resolve on
    # Command. cgr infers the receiver `c` type (Command) already; the missing
    # piece is the RETURN type of Root() feeding the next hop. This is the cobra
    # `cmd.Root().GenZshCompletion()` gap.
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
    # first hop already resolves via receiver-type inference.
    assert ("proj.m.Command.Use", "proj.m.Command.Root") in calls
    # second hop needs Root()'s return type (*Command) to resolve Run().
    assert ("proj.m.Command.Use", "proj.m.Command.Run") in calls


def _calls(tmp_path: Path, body: str) -> set[tuple[str, str]]:
    (tmp_path / "m.go").write_text(body, encoding="utf-8")
    ingestor = _capture(tmp_path, "proj")
    return {(str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"}


def test_chained_call_on_container_return_does_not_misresolve(tmp_path: Path) -> None:
    # Kids() returns []Command (a slice); a chained `.Run()` is called on the
    # slice, NOT on a Command, so it must NOT resolve to Command.Run. Unwrapping
    # the container to its element type would emit a false edge.
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
    # A dotted call argument (`1.5`) must not break the chain split: Find(1.5)
    # returns *Command, so Run() still resolves.
    calls = _calls(
        tmp_path,
        "package p\n"
        "type Command struct{}\n"
        "func (c *Command) Find(x float64) *Command { return c }\n"
        "func (c *Command) Run() int { return 1 }\n"
        "func (c *Command) Use() int { return c.Find(1.5).Run() }\n",
    )
    assert ("proj.m.Command.Use", "proj.m.Command.Run") in calls


def test_local_from_method_return_resolves_later_call(tmp_path: Path) -> None:
    # `root := c.Root()` stores the return of a method in a local across
    # statements; a later `root.Run()` must resolve on the return type
    # (Command), not stay unresolved. The stored-local form of the inline chain.
    calls = _calls(
        tmp_path,
        "package p\n"
        "type Command struct{}\n"
        "func (c *Command) Root() *Command { return c }\n"
        "func (c *Command) Run() int { return 1 }\n"
        "func (c *Command) Use() int { root := c.Root(); return root.Run() }\n",
    )
    assert ("proj.m.Command.Use", "proj.m.Command.Root") in calls
    assert ("proj.m.Command.Use", "proj.m.Command.Run") in calls


def test_local_from_field_method_chain_resolves(tmp_path: Path) -> None:
    # The gin router shape: `root := engine.trees.get(method)` then
    # `root.addRoute(...)`. `engine.trees` is a struct-field hop (needs Go field
    # types), `.get()` returns *node, so root.addRoute must resolve to
    # node.addRoute -- NOT mis-resolve to the enclosing Engine.addRoute.
    calls = _calls(
        tmp_path,
        "package p\n"
        "type node struct{}\n"
        "func (n *node) addChild(c *node) {}\n"
        "func (n *node) addRoute(path string) { n.addChild(&node{}) }\n"
        "type trees struct{}\n"
        "func (ts trees) get(m string) *node { return &node{} }\n"
        "type Engine struct { trees trees }\n"
        "func (e *Engine) addRoute(m string, p string) {\n"
        "  root := e.trees.get(m)\n"
        "  root.addRoute(p)\n"
        "}\n",
    )
    assert ("proj.m.Engine.addRoute", "proj.m.node.addRoute") in calls
    # the false self-edge from mis-resolving root.addRoute to the enclosing
    # type's same-named method must not appear.
    assert ("proj.m.Engine.addRoute", "proj.m.Engine.addRoute") not in calls


def test_direct_field_hop_method_call_resolves(tmp_path: Path) -> None:
    # The gin `ServeHTTP` shape: `c := pool.Get().(*Context)` binds c via a TYPE
    # ASSERTION, then `c.writermem.reset(w)` is a field-hop receiver called INLINE
    # with no intermediate local. Two gaps compose: (1) the assertion must type c
    # as Context; (2) `writermem` is a struct field of type responseWriter, so
    # `.reset` must resolve to responseWriter.reset via the field-type map. A
    # same-named decoy `Context.reset` (gin has one) defeats the trie coincidence:
    # the bare-method fallback mis-binds to Context.reset and orphans
    # responseWriter.reset unless both the assertion and the field hop resolve.
    (tmp_path / "m.go").write_text(
        "package p\n"
        "type responseWriter struct{}\n"
        "func (w *responseWriter) reset(x int) {}\n"
        "type Context struct { writermem responseWriter }\n"
        "func (c *Context) reset() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "gin.go").write_text(
        "package p\n"
        "type pool struct{}\n"
        "func (p *pool) Get() any { return nil }\n"
        "type Engine struct { pool pool }\n"
        "func (e *Engine) serve() {\n"
        "  c := e.pool.Get().(*Context)\n"
        "  c.writermem.reset(1)\n"
        "}\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"
    }
    assert ("proj.gin.Engine.serve", "proj.m.responseWriter.reset") in calls
    # must not mis-resolve the field hop to the same-named enclosing-type method.
    assert ("proj.gin.Engine.serve", "proj.m.Context.reset") not in calls
