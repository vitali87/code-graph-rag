# ast-grep finding analyzer (issue #413). Runs categorized YAML rules over
# indexed source files and emits Pattern/CodeSmell/SecurityIssue nodes linked
# to each file's Module. The FINDINGS capture group is opt-in, so these tests
# build a selection with it enabled and assert the finding nodes/edges land.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

PATTERN = cs.NodeLabel.PATTERN.value
CODE_SMELL = cs.NodeLabel.CODE_SMELL.value
SECURITY_ISSUE = cs.NodeLabel.SECURITY_ISSUE.value
IMPLEMENTS_PATTERN = cs.RelationshipType.IMPLEMENTS_PATTERN.value
HAS_SMELL = cs.RelationshipType.HAS_SMELL.value
HAS_VULNERABILITY = cs.RelationshipType.HAS_VULNERABILITY.value

SINGLETON_PY = (
    "class Config:\n"
    "    _instance = None\n"
    "\n"
    "    def get(self):\n"
    "        return self._instance\n"
)

SQLI_PY = (
    "def lookup(db, user_id):\n"
    "    return db.execute('SELECT * FROM t WHERE id = ' + user_id)\n"
)

SAFE_PY = (
    "def lookup(db, user_id):\n"
    "    return db.execute('SELECT * FROM t WHERE id = ?', (user_id,))\n"
)


def _run(tmp_path: Path, files: dict[str, str], tokens: list[str]) -> MagicMock:
    parsers, queries = load_parsers()
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run()
    return mock


def _node_names(mock: MagicMock, label: str) -> set[str]:
    return {
        c.args[1].get(cs.KEY_NAME)
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == label
    }


def _rel_targets(mock: MagicMock, rel_type: str) -> set[str]:
    return {
        c.args[2][2]
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel_type
    }


def test_singleton_pattern_detected(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"config.py": SINGLETON_PY}, ["+findings"])
    assert "singleton" in _node_names(mock, PATTERN)


def test_sql_injection_detected(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"dao.py": SQLI_PY}, ["+findings"])
    assert "sqli_concat" in _node_names(mock, SECURITY_ISSUE)


def test_finding_linked_to_module(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"dao.py": SQLI_PY}, ["+findings"])
    targets = _rel_targets(mock, HAS_VULNERABILITY)
    assert any(qn.endswith("sqli_concat") for qn in targets), targets


def test_safe_query_not_flagged(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"dao.py": SAFE_PY}, ["+findings"])
    assert "sqli_concat" not in _node_names(mock, SECURITY_ISSUE)


def test_findings_opt_in_disabled_by_default(tmp_path: Path) -> None:
    # FINDINGS is not in DEFAULT_CAPTURE_GROUPS; a default index emits none.
    mock = _run(tmp_path, {"config.py": SINGLETON_PY, "dao.py": SQLI_PY}, [])
    assert _node_names(mock, PATTERN) == set()
    assert _node_names(mock, SECURITY_ISSUE) == set()


class _FakeIngestor:
    def __init__(self) -> None:
        self.nodes: list[tuple[str, dict]] = []
        self.rels: list[tuple[tuple, str, tuple]] = []

    def ensure_node_batch(self, label, props) -> None:
        self.nodes.append((str(label), props))

    def ensure_relationship_batch(self, src, rel, dst) -> None:
        self.rels.append((src, str(rel), dst))


def _labelled(rules, label) -> list:
    return [r for r in rules if r.node_label == label]


def test_rules_load_and_meet_acceptance_counts() -> None:
    from codebase_rag.analyzers.ast_grep_analyzer import load_finding_rules

    rules = load_finding_rules()
    assert {".py", ".js", ".ts"} <= set(rules)
    py = rules[".py"].rules
    assert len(_labelled(py, cs.NodeLabel.PATTERN)) >= 5
    assert len(_labelled(py, cs.NodeLabel.CODE_SMELL)) >= 5
    assert len(_labelled(py, cs.NodeLabel.SECURITY_ISSUE)) >= 5
    for ext in (".js", ".ts"):
        js = rules[ext].rules
        assert len(_labelled(js, cs.NodeLabel.PATTERN)) >= 3
        assert len(_labelled(js, cs.NodeLabel.CODE_SMELL)) >= 3
        assert len(_labelled(js, cs.NodeLabel.SECURITY_ISSUE)) >= 3


def test_every_language_has_all_three_categories() -> None:
    # (H) Each supported grammar must ship patterns + smells + security with at
    # (H) least 3 rules apiece, so a language never regresses to partial coverage.
    from codebase_rag.analyzers.ast_grep_analyzer import load_finding_rules

    by_grammar: dict[str, dict] = {}
    for lang in load_finding_rules().values():
        counts = by_grammar.setdefault(lang.ast_grep_id, {})
        for r in lang.rules:
            counts[r.node_label] = counts.get(r.node_label, 0) + 1
    for gid, counts in sorted(by_grammar.items()):
        for label in (
            cs.NodeLabel.PATTERN,
            cs.NodeLabel.CODE_SMELL,
            cs.NodeLabel.SECURITY_ISSUE,
        ):
            assert counts.get(label, 0) >= 3, f"{gid} has <3 {label}: {counts}"


def test_analyzer_emits_node_and_edge_directly(tmp_path: Path) -> None:
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "m.py"
    src.write_text(SINGLETON_PY, encoding="utf-8")
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.m": src}
    )
    pattern_nodes = [p for label, p in ing.nodes if label == PATTERN]
    assert any(p[cs.KEY_NAME] == "singleton" for p in pattern_nodes)
    assert any(rel == IMPLEMENTS_PATTERN for _s, rel, _d in ing.rels)


def test_analyzer_noops_when_findings_disabled(tmp_path: Path) -> None:
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "m.py"
    src.write_text(SINGLETON_PY, encoding="utf-8")
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture([])).analyze({"proj.m": src})
    assert ing.nodes == []
    assert ing.rels == []


def test_loose_equality_flags_inequality_not_strict(tmp_path: Path) -> None:
    # (H) `!=` coerces types exactly like `==`, so the loose_equality smell must
    # (H) catch it too; the strict `===`/`!==` forms must stay clean.
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "eq.js"
    src.write_text(
        "const a = (x === y);\n"
        "const b = (x !== y);\n"
        "const c = (x == y);\n"
        "const d = (x != y);\n",
        encoding="utf-8",
    )
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.eq": src}
    )
    lines = sorted(
        p[cs.KEY_START_LINE]
        for label, p in ing.nodes
        if label == CODE_SMELL and p[cs.KEY_NAME] == "loose_equality"
    )
    assert lines == [3, 4], lines


# Positive fixtures keyed by ast-grep grammar id. Each string is crafted to
# trigger every rule shipped for that grammar; test_every_rule_fires asserts so.
_POSITIVE_FIXTURES = {
    "python": (
        "from mod import *\n"
        "class Config:\n    _instance = None\n"
        "class Ctx:\n    def __enter__(self): return self\n"
        "    def __exit__(self, *a): pass\n"
        "class It:\n    def __iter__(self): return self\n"
        "    def __next__(self): raise StopIteration\n"
        "from abc import ABC\n"
        "class Base(ABC): pass\n"
        "def make_widget(): return 1\n"
        "def bad(x=[], y={}):\n"
        "    global COUNTER\n"
        "    try: pass\n    except: pass\n"
        "    try: pass\n    except Exception: pass\n"
        "def sec(db, u):\n"
        '    db.execute("SELECT " + u)\n'
        '    db.execute(f"SELECT {u}")\n'
        '    password = "supersecretvalue123"\n'
        "    eval(u); exec(u)\n"
        "    import os, subprocess, yaml, pickle\n"
        '    os.system("rm " + u)\n'
        "    subprocess.run(u, shell=True)\n"
        "    yaml.load(u); pickle.loads(u)\n"
    ),
    "javascript": (
        "class A { getInstance() { return 1; } }\n"
        "const p = new Promise((res) => res(1));\n"
        "function createThing() { return {}; }\n"
        "var legacy = 1;\n"
        "if (a == b) { doThing(); }\n"
        'console.log("hi");\n'
        "debugger;\n"
        "el.innerHTML = userInput;\n"
        "eval(userInput);\n"
        "document.write(userInput);\n"
        'const password = "supersecretvalue123";\n'
    ),
    "rust": (
        "struct S;\n"
        "impl S { fn new() -> Self { S } }\n"
        "trait Greet { fn hi(&self); }\n"
        "impl Greet for S { fn hi(&self) {} }\n"
        "fn build_it(conn: &Conn) -> i32 {\n"
        "    let x: Option<i32> = None;\n"
        "    let a = x.unwrap();\n"
        '    let b = x.expect("no");\n'
        "    dbg!(a);\n"
        '    panic!("boom");\n'
        "    unsafe { }\n"
        '    let password = "supersecretvalue";\n'
        '    std::process::Command::new("ls");\n'
        '    conn.execute(format!("SELECT {}", a));\n'
        "    b\n"
        "}\n"
    ),
    "go": (
        "package main\n"
        'import ("fmt"; "os/exec"; "sync")\n'
        "type Server struct { once sync.Once }\n"
        "type Greeter interface { Hi() }\n"
        "func NewServer() *Server { return nil }\n"
        "func run(db DB) {\n"
        '    fmt.Println("hi")\n'
        '    panic("x")\n'
        "    result, _ := doThing()\n"
        "    _ = other()\n"
        "    _ = result\n"
        '    exec.Command("ls")\n'
        '    db.Query("SELECT " + err)\n'
        '    password := "supersecretvalue"\n'
        "    _ = password\n"
        "}\n"
    ),
    "java": (
        "class FooBuilder {\n"
        "    static Foo getInstance() { return null; }\n"
        "    Foo createFoo() { return null; }\n"
        "    FooBuilder self() { return this; }\n"
        "    void go() {\n"
        '        System.out.println("x");\n'
        "        try { risky(); } catch (Exception e) {}\n"
        "        try { risky(); } catch (IOException e) { e.printStackTrace(); }\n"
        '        Runtime.getRuntime().exec("ls");\n'
        '        new ProcessBuilder("ls");\n'
        '        stmt.executeQuery("SELECT " + x);\n'
        '        String password = "supersecretval";\n'
        '        MessageDigest.getInstance("MD5");\n'
        "    }\n"
        "}\n"
    ),
    "c": (
        "#include <stdio.h>\n"
        "void* create_obj() { return 0; }\n"
        "void init_sys() {}\n"
        "void free_obj(void* p) {}\n"
        "void run(char* src) {\n"
        "    goto done;\n"
        '    printf("dbg");\n'
        "    while (1) { break; }\n"
        "    char buf[10];\n"
        "    strcpy(buf, src);\n"
        '    system("ls");\n'
        '    char *password = "supersecretvalue";\n'
        "done: ;\n"
        "}\n"
    ),
    "cpp": (
        "#include <iostream>\n"
        "using namespace std;\n"
        "class Base { public: virtual void f() = 0; };\n"
        "class Mgr {\n"
        "  public:\n"
        "    static Mgr& getInstance() { static Mgr m; return m; }\n"
        "    static Mgr* create() { return nullptr; }\n"
        "};\n"
        "void run(char* src) {\n"
        '    std::cout << "dbg";\n'
        "    int* p = new int(5);\n"
        "    char buf[10];\n"
        '    system("ls");\n'
        "    strcpy(buf, src);\n"
        '    const char* password = "supersecretval";\n'
        "}\n"
    ),
    "csharp": (
        "abstract class FooBuilder {\n"
        "    static Foo Instance;\n"
        "    public Foo CreateFoo() { return null; }\n"
        "    public void Dispose() {}\n"
        "    public void Go() {\n"
        '        Console.WriteLine("x");\n'
        "        try { Risky(); } catch (Exception e) {}\n"
        "        Thread.Sleep(100);\n"
        '        Process.Start("ls");\n'
        '        var cmd = new SqlCommand("SELECT " + x);\n'
        '        string password = "supersecretvalue";\n'
        "    }\n"
        "}\n"
    ),
    "php": (
        "<?php\n"
        "abstract class Widget {\n"
        "    public static function getInstance() { return null; }\n"
        "    public function createThing() { return null; }\n"
        "}\n"
        "function build_widget() { return null; }\n"
        "function run($db) {\n"
        "    var_dump($x);\n"
        '    @file_get_contents("x");\n'
        "    if ($a == $b) {}\n"
        "    global $conf;\n"
        "    eval($code);\n"
        '    system("ls");\n'
        '    $db->query("SELECT " . $x);\n'
        '    $password = "supersecretvalue";\n'
        "}\n"
    ),
    "lua": (
        "local M = {}\n"
        "function new_obj()\n"
        "    local self = setmetatable({}, M)\n"
        "    return self\n"
        "end\n"
        "M.__index = M\n"
        "function big(a, b, c, d, e, f, g, h) end\n"
        "function run()\n"
        '    print("dbg")\n'
        "    counter = 5\n"
        "    goto done\n"
        '    load("code")\n'
        '    os.execute("ls")\n'
        '    dofile("x.lua")\n'
        '    local password = "supersecretvalue"\n'
        "    ::done::\n"
        "end\n"
        "return M\n"
    ),
    "scala": (
        "object Registry {\n"
        "  def apply(): Registry = new Registry()\n"
        "  def create(): Registry = new Registry()\n"
        "  var count = 0\n"
        "  val x: String = null\n"
        "  def run(): Unit = {\n"
        '    println("dbg")\n'
        "    val s = obj.asInstanceOf[String]\n"
        '    Runtime.getRuntime.exec("ls")\n'
        '    val q = "SELECT x" + name\n'
        '    val password = "supersecretvalue1"\n'
        "  }\n"
        "}\n"
        "case class Point(x: Int, y: Int)\n"
        "class Registry\n"
    ),
    "dart": (
        "abstract class ShapeBuilder {\n"
        "  static final ShapeBuilder instance = ShapeBuilder._();\n"
        "  factory ShapeBuilder.of() => instance;\n"
        "  void run() {\n"
        '    print("dbg");\n'
        "    try { risky(); } catch (e) {}\n"
        "    dynamic x = 5;\n"
        "    if (y == null) {}\n"
        '    Process.run("ls", []);\n'
        '    db.rawQuery("SELECT * FROM t WHERE id=" + name);\n'
        '    var password = "supersecretvalue";\n'
        "  }\n"
        "}\n"
    ),
    "ruby": (
        "class Registry\n"
        "  include Singleton\n"
        "  def self.create\n"
        "  end\n"
        "  def run\n"
        '    puts "dbg"\n'
        "    begin\n"
        "      risky\n"
        "    rescue\n"
        "    end\n"
        "    $global = 5\n"
        "    eval(code)\n"
        '    system("ls")\n'
        '    execute("SELECT #{x}")\n'
        '    password = "supersecretvalue"\n'
        "  end\n"
        "end\n"
    ),
}
_POSITIVE_FIXTURES["typescript"] = _POSITIVE_FIXTURES["javascript"]
_POSITIVE_FIXTURES["tsx"] = _POSITIVE_FIXTURES["javascript"]


def test_every_rule_fires_on_positive_fixture(tmp_path: Path) -> None:
    # (H) Exhaustive functionality lock: a rule whose `kind`/`pattern` is wrong for
    # (H) ast-grep's bundled grammar silently matches nothing (the f_string class of
    # (H) bug). Every shipped rule must fire on a hand-crafted positive fixture, so a
    # (H) future typo that zeroes a rule fails here instead of shipping dead.
    from codebase_rag.analyzers import FindingAnalyzer
    from codebase_rag.analyzers.ast_grep_analyzer import load_finding_rules

    # (H) Keyed by ast-grep grammar id, not extension: .js/.jsx/.mjs/.cjs all
    # (H) share the javascript grammar and rule set, so one fixture covers them.
    by_grammar = _POSITIVE_FIXTURES
    for ext, lang in load_finding_rules().items():
        # (H) A new language with rules but no fixture would silently skip firing
        # (H) coverage; force a fixture mapping to exist for every loaded grammar.
        assert lang.ast_grep_id in by_grammar, (
            f"{ext}: no fixture for {lang.ast_grep_id}"
        )
        ids = [r.rule_id for r in lang.rules]
        # (H) The set comparison below is only sound if ids are unique per ext; a
        # (H) duplicate id could let one rule fire while its twin stays dead.
        assert len(ids) == len(set(ids)), f"{ext} duplicate rule ids: {ids}"
        stem = ext.replace(".", "")
        f = tmp_path / f"probe{stem}{ext}"
        f.write_text(by_grammar[lang.ast_grep_id], encoding="utf-8")
        ing = _FakeIngestor()
        FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
            {f"probe{stem}": f}
        )
        fired = {p[cs.KEY_NAME] for _label, p in ing.nodes}
        assert set(ids) <= fired, f"{ext} dead rules: {sorted(set(ids) - fired)}"


def _fire(tmp_path: Path, name: str, src: str) -> list:
    from codebase_rag.analyzers import FindingAnalyzer

    f = tmp_path / name
    f.write_text(src, encoding="utf-8")
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {name.split(".", maxsplit=1)[0]: f}
    )
    return [p for _label, p in ing.nodes]


def test_innerhtml_xss_catches_augmented_and_outerhtml(tmp_path: Path) -> None:
    # (H) `+=` and outerHTML are the same DOM-injection sink as `innerHTML =`.
    src = (
        "a.innerHTML = x;\n"
        "b.innerHTML += y;\n"
        "c.outerHTML = z;\n"
        "d.outerHTML += w;\n"
        "e.textContent = safe;\n"
    )
    lines = sorted(
        p[cs.KEY_START_LINE]
        for p in _fire(tmp_path, "x.js", src)
        if p[cs.KEY_NAME] == "innerhtml_xss"
    )
    assert lines == [1, 2, 3, 4], lines


def test_sqli_concat_catches_percent_format(tmp_path: Path) -> None:
    # (H) `execute("... %s" % params)` is the classic printf-style injection, as
    # (H) dangerous as `+` concatenation; a parametrized query stays clean, and a
    # (H) numeric modulo (line 4) must NOT be mistaken for string formatting.
    src = (
        'db.execute("SELECT " + u)\n'
        'db.execute("SELECT %s" % u)\n'
        'db.execute("SELECT ?", (u,))\n'
        "db.execute(index % shard_count)\n"
    )
    lines = sorted(
        p[cs.KEY_START_LINE]
        for p in _fire(tmp_path, "dao.py", src)
        if p[cs.KEY_NAME] == "sqli_concat"
    )
    assert lines == [1, 2], lines


def test_factory_function_catches_arrow_and_expression(tmp_path: Path) -> None:
    # (H) Modern factories are usually `const createX = () => {}` (arrow), a
    # (H) function expression, or a generator function expression, not a `function`
    # (H) declaration; catch all four. A non-factory name stays clean.
    src = (
        "function createA() { return {}; }\n"
        "const createB = () => ({});\n"
        "const makeC = function () { return {}; };\n"
        "const makeGen = function* () { yield {}; };\n"
        "const other = () => ({});\n"
    )
    names = sorted(
        p[cs.KEY_START_LINE]
        for p in _fire(tmp_path, "f.js", src)
        if p[cs.KEY_NAME] == "factory_function"
    )
    assert names == [1, 2, 3, 4], names


def test_go_ignored_error_only_flags_discarded_last_value(tmp_path: Path) -> None:
    # (H) In Go, `_, err := f()` keeps the error and is idiomatic; only a trailing
    # (H) `_` (discarding the conventionally-last error) is the smell. The rule
    # (H) must flag line 4 (result, _) and leave line 3 (_, err) clean.
    src = (
        "package main\n"
        "func f() {\n"
        "    _, err := doThing()\n"
        "    result, _ := doThing()\n"
        "    _ = err\n"
        "    _ = result\n"
        "}\n"
    )
    lines = sorted(
        p[cs.KEY_START_LINE]
        for p in _fire(tmp_path, "x.go", src)
        if p[cs.KEY_NAME] == "ignored_error_shortvar"
    )
    assert lines == [4], lines


def test_go_sqli_concat_requires_concat_inside_the_query_call(
    tmp_path: Path,
) -> None:
    # (H) Dogfood FP: the concatenation must be an ARGUMENT of the Query/Exec
    # (H) call, not merely present somewhere in the same expression. Line 4's
    # (H) `url.QueryEscape` is a net/url false friend (matches `^Query`) and the
    # (H) `+` belongs to a Header().Set call, not a database sink.
    src = (
        "package main\n"
        "func f(db D, c C, a, b, id string) {\n"
        '    db.Query("select * from t where x=" + id)\n'
        '    c.Header().Set("d", a + url.QueryEscape(b))\n'
        "}\n"
    )
    lines = sorted(
        p[cs.KEY_START_LINE]
        for p in _fire(tmp_path, "dao.go", src)
        if p[cs.KEY_NAME] == "sqli_concat"
    )
    assert lines == [3], lines


def test_go_hardcoded_secret_requires_literal_value(tmp_path: Path) -> None:
    # (H) Dogfood FP class: a credential-named var whose value is a function
    # (H) call (e.g. `token := getEnv("SOME_LONG_DEFAULT")`) is NOT a hardcoded
    # (H) secret; the string literal must be the assigned value itself, not
    # (H) buried inside a call argument. Line 3 is a real literal secret.
    src = (
        "package main\n"
        "func f() {\n"
        '    token := "literalsecretvalue"\n'
        '    apikey := getEnv("SOME_LONG_DEFAULT")\n'
        "    _ = token\n"
        "    _ = apikey\n"
        "}\n"
    )
    lines = sorted(
        p[cs.KEY_START_LINE]
        for p in _fire(tmp_path, "cfg.go", src)
        if p[cs.KEY_NAME] == "hardcoded_secret"
    )
    assert lines == [3], lines


def test_multilang_security_rules_avoid_common_false_positives(
    tmp_path: Path,
) -> None:
    # (H) Precision guards for the widened multi-language rules: each benign
    # (H) construct must NOT emit its neighbouring finding.
    cases = [
        (
            "q.php",
            '<?php build_query("a" . $x); $o->query_posts("z" . $w);\n',
            "sqli_concat",
        ),
        ("e.java", 'class A { void f(){ myExecutor.exec("job"); } }\n', "runtime_exec"),
        ("e.scala", "object O { def f() { list.exec() } }\n", "os_command_exec"),
        # prose containing SQL keyword pairs must not read as a query without a
        # real database sink around the concatenation
        (
            "d.dart",
            'void f(x){ var b = "Please select a file from the list: " + x; }\n',
            "sqli_concat",
        ),
        ("u.dart", 'void f(x){ var label = "Select " + option; }\n', "sqli_concat"),
        # `== nullValue` is a normal comparison, not an explicit null check
        (
            "n.dart",
            "void f(x, nullValue){ if (x == nullValue) {} }\n",
            "double_equals_null",
        ),
        (
            "r.java",
            'class A { void f(){ executor.exec("Runtime configuration"); } }\n',
            "runtime_exec",
        ),
        (
            "c.cs",
            'class A { void f(){ request.CommandText = "Choose " + option; } }\n',
            "sql_injection",
        ),
    ]
    for name, src, rule_id in cases:
        names = [p[cs.KEY_NAME] for p in _fire(tmp_path, name, src)]
        assert rule_id not in names, (name, rule_id, names)


def test_same_line_findings_get_distinct_ids(tmp_path: Path) -> None:
    # two matches of one rule on a single line must not collapse into one
    # node; the qualified_name has to distinguish them by column.
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "a.js"
    src.write_text("console.log(1); console.log(2);\n", encoding="utf-8")
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.a": src}
    )
    qns = [
        p[cs.KEY_QUALIFIED_NAME]
        for label, p in ing.nodes
        if label == CODE_SMELL and p[cs.KEY_NAME] == "console_log"
    ]
    assert len(qns) == 2, qns
    assert len(set(qns)) == 2, qns


def test_hardcoded_secret_ignores_empty_and_format_templates(tmp_path: Path) -> None:
    # empty strings and format/message templates (an f-string/.format {..}
    # placeholder or a printf %s/%d specifier) are not secrets. Real secret
    # literals that legitimately contain %, spaces, embedded-credential URLs
    # or SCREAMING_SNAKE shapes MUST still be caught (a security rule favours
    # recall). Lines 4-6 are the three false-negative shapes Greptile flagged.
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "s.py"
    src.write_text(
        'token = ""\n'
        'TOKEN_COUNT_FAILED = "Context token count failed: {error}"\n'
        'LOG_SECRET = "processed %s rows in %d ms"\n'
        'db_password = "postgres://admin:hardcoded-secret@db/prod"\n'
        'API_KEY = "sk-abcd1234efgh5678"\n'
        'GEN_TOKEN = "A1B2_C3D4_E5F6G7"\n',
        encoding="utf-8",
    )
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.s": src}
    )
    secrets = sorted(
        p[cs.KEY_START_LINE]
        for label, p in ing.nodes
        if label == SECURITY_ISSUE and p[cs.KEY_NAME] == "hardcoded_secret"
    )
    assert secrets == [4, 5, 6], secrets


def test_tsx_files_get_findings(tmp_path: Path) -> None:
    from codebase_rag.analyzers import FindingAnalyzer
    from codebase_rag.analyzers.ast_grep_analyzer import load_finding_rules

    assert ".tsx" in load_finding_rules()
    src = tmp_path / "c.tsx"
    src.write_text(
        "const C = () => { console.log('x'); return null; };\n", encoding="utf-8"
    )
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.c": src}
    )
    names = [p[cs.KEY_NAME] for label, p in ing.nodes if label == CODE_SMELL]
    assert "console_log" in names, names
