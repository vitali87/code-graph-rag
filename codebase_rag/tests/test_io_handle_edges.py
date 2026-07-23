# Handle-aware I/O for the lean non-Python walk (issue #714): a call binding
# a resource handle (`os.OpenFile`, `fs.createWriteStream`, `new FileWriter`,
# `File::open`, `std::ifstream f("x")`) attributes later method calls on the
# bound variable to the constructor's resource, exactly as Python's handle
# walk does for `open()` / `sqlite3.connect()`.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
WRITES_TO = cs.RelationshipType.WRITES_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run_io(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    # Build the graph for `files` and return (caller_qn, rel_type, resource_qn)
    # for READS_FROM / WRITES_TO edges only.
    parsers, queries = load_parsers()
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in (READS_FROM, WRITES_TO)
    }


def _has(rels: set[tuple[str, str, str]], caller: str, rel: str, resource: str) -> bool:
    # Java method qns carry a parameter signature suffix (`A.fetch(String)`);
    # match on the qn with any trailing `(...)` stripped.
    return any(
        a.partition("(")[0].endswith(caller) and r == rel and b == resource
        for a, r, b in rels
    )


# Go tests below.


def test_go_openfile_handle_write(tmp_path: Path) -> None:
    # os.OpenFile is a handle constructor, NOT a direct sink (its direction
    # depends on flags), so this WRITE can only come from the method binding.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func save(s string) {\n"
            '\tf, _ := os.OpenFile("data.txt", os.O_WRONLY, 0644)\n'
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.save", WRITES_TO, "resource::FILE::data.txt")


def test_go_openfile_handle_read(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func load(buf []byte) {\n"
            '\tf, _ := os.OpenFile("data.txt", os.O_RDONLY, 0)\n'
            "\tf.Read(buf)\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.load", READS_FROM, "resource::FILE::data.txt")


def test_go_sql_open_query_reads(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "database/sql"\n\n'
            "func fetch() {\n"
            '\tdb, _ := sql.Open("postgres", "dsn")\n'
            '\tdb.Query("SELECT 1")\n'
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.fetch", READS_FROM, "resource::DATABASE::dsn")


def test_go_sql_open_exec_writes(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "database/sql"\n\n'
            "func store() {\n"
            '\tdb, _ := sql.Open("postgres", "dsn")\n'
            '\tdb.Exec("INSERT INTO t VALUES (1)")\n'
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.store", WRITES_TO, "resource::DATABASE::dsn")


def test_go_net_dial_write(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "net"\n\n'
            "func send(payload []byte) {\n"
            '\tconn, _ := net.Dial("tcp", "example.com:80")\n'
            "\tconn.Write(payload)\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.send", WRITES_TO, "resource::SOCKET::example.com:80")


def test_go_handle_alias_tracks_binding(tmp_path: Path) -> None:
    # `g := f` aliases the handle; I/O through the alias still attributes to
    # the constructor's resource.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func save(s string) {\n"
            '\tf, _ := os.OpenFile("data.txt", os.O_WRONLY, 0644)\n'
            "\tg := f\n"
            "\tg.WriteString(s)\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.save", WRITES_TO, "resource::FILE::data.txt")


def test_go_rebound_handle_emits_nothing(tmp_path: Path) -> None:
    # Rebinding the variable to a non-handle kills the binding.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func f(s string, other File) {\n"
            '\tw, _ := os.OpenFile("data.txt", os.O_WRONLY, 0644)\n'
            "\tw = other\n"
            "\tw.WriteString(s)\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "main.f", WRITES_TO, "resource::FILE::data.txt")


def test_go_block_local_handle_does_not_leak(tmp_path: Path) -> None:
    # A handle declared inside a nested block is out of scope after the
    # block; a same-named use outside must not attribute to it (greploop P1).
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func load(f Reader, buf []byte) {\n"
            "\t{\n"
            '\t\tf, _ := os.OpenFile("a.txt", os.O_RDONLY, 0)\n'
            "\t\t_ = f\n"
            "\t}\n"
            "\tf.Read(buf)\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not _has(rels, "main.load", READS_FROM, "resource::FILE::a.txt")


def test_go_unbound_receiver_emits_nothing(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\nfunc f(w Writer, s string) {\n\tw.WriteString(s)\n}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not any(r == WRITES_TO for _, r, _ in rels)


# JS/TS tests below.


def test_js_write_stream_handle(tmp_path: Path) -> None:
    files = {
        "app.js": (
            "const fs = require('fs');\n"
            "function save(data) {\n"
            "  const ws = fs.createWriteStream('out.txt');\n"
            "  ws.write(data);\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "app.save", WRITES_TO, "resource::FILE::out.txt")


def test_js_read_stream_handle(tmp_path: Path) -> None:
    files = {
        "app.js": (
            "const fs = require('fs');\n"
            "function load() {\n"
            "  const rs = fs.createReadStream('in.txt');\n"
            "  rs.read();\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "app.load", READS_FROM, "resource::FILE::in.txt")


def test_js_stream_end_writes(tmp_path: Path) -> None:
    # `ws.end(data)` flushes the final chunk: a WRITE.
    files = {
        "app.js": (
            "const fs = require('fs');\n"
            "function finish(data) {\n"
            "  const ws = fs.createWriteStream('log.txt');\n"
            "  ws.end(data);\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "app.finish", WRITES_TO, "resource::FILE::log.txt")


def test_js_unbound_receiver_emits_nothing(tmp_path: Path) -> None:
    files = {
        "app.js": ("function f(ws, data) {\n  ws.write(data);\n}\n"),
    }
    rels = _run_io(tmp_path, files)
    assert not any(r == WRITES_TO for _, r, _ in rels)


# Java tests below.


def test_java_new_filewriter_write(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "import java.io.FileWriter;\n"
            "class A {\n"
            "  void save(String s) throws Exception {\n"
            '    FileWriter w = new FileWriter("out.txt");\n'
            "    w.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.save", WRITES_TO, "resource::FILE::out.txt")


def test_java_buffered_reader_wrapper(tmp_path: Path) -> None:
    # The idiomatic wrapper: the resource identity comes from the INNER
    # constructor (`new FileReader("in.txt")`).
    files = {
        "A.java": (
            "import java.io.BufferedReader;\n"
            "import java.io.FileReader;\n"
            "class A {\n"
            "  void load() throws Exception {\n"
            '    BufferedReader br = new BufferedReader(new FileReader("in.txt"));\n'
            "    String line = br.readLine();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.load", READS_FROM, "resource::FILE::in.txt")


def test_java_files_new_buffered_reader_path_of(tmp_path: Path) -> None:
    # `Files.newBufferedReader(Path.of("cfg.txt"))`: the identity unwraps
    # through Path.of to the literal.
    files = {
        "A.java": (
            "import java.nio.file.Files;\n"
            "import java.nio.file.Path;\n"
            "class A {\n"
            "  void load() throws Exception {\n"
            '    var r = Files.newBufferedReader(Path.of("cfg.txt"));\n'
            "    r.readLine();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.load", READS_FROM, "resource::FILE::cfg.txt")


def test_java_connection_statement_query(tmp_path: Path) -> None:
    # DriverManager.getConnection binds a DATABASE handle; createStatement
    # DERIVES a same-resource handle; executeQuery reads through it.
    files = {
        "A.java": (
            "import java.sql.Connection;\n"
            "import java.sql.DriverManager;\n"
            "import java.sql.Statement;\n"
            "class A {\n"
            "  void fetch(String url) throws Exception {\n"
            "    Connection c = DriverManager.getConnection(url);\n"
            "    Statement st = c.createStatement();\n"
            '    st.executeQuery("SELECT 1");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.fetch", READS_FROM, "resource::DATABASE::<dynamic>")


def test_java_fully_qualified_new_constructor(tmp_path: Path) -> None:
    # `new java.io.FileWriter("out.txt")`: the fully qualified constructor
    # type must bind exactly like the simple name (greploop P1).
    files = {
        "A.java": (
            "class A {\n"
            "  void save(String s) throws Exception {\n"
            '    java.io.FileWriter w = new java.io.FileWriter("out.txt");\n'
            "    w.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.save", WRITES_TO, "resource::FILE::out.txt")


def test_java_scanner_new_file_identity(tmp_path: Path) -> None:
    # `new Scanner(new File("x"))`: Scanner is a wrapper; File is not a handle
    # itself but carries the identity literal.
    files = {
        "A.java": (
            "import java.io.File;\n"
            "import java.util.Scanner;\n"
            "class A {\n"
            "  void load() throws Exception {\n"
            '    Scanner sc = new Scanner(new File("data.csv"));\n'
            "    String line = sc.nextLine();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.load", READS_FROM, "resource::FILE::data.csv")


def test_java_printwriter_filename_overload(tmp_path: Path) -> None:
    # PrintWriter is both a wrapper and a direct filename constructor; the
    # filename overload must bind when arg0 is not a handle.
    files = {
        "A.java": (
            "import java.io.PrintWriter;\n"
            "class A {\n"
            "  void save(String s) throws Exception {\n"
            '    PrintWriter pw = new PrintWriter("report.txt");\n'
            "    pw.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.save", WRITES_TO, "resource::FILE::report.txt")


def test_java_statement_execute_sql_refinement(tmp_path: Path) -> None:
    # `execute(sql)` is READ_WRITE by signature; a SELECT literal refines it
    # to a READ only.
    files = {
        "A.java": (
            "import java.sql.Connection;\n"
            "import java.sql.DriverManager;\n"
            "import java.sql.Statement;\n"
            "class A {\n"
            "  void fetch(String url) throws Exception {\n"
            "    Connection c = DriverManager.getConnection(url);\n"
            "    Statement st = c.createStatement();\n"
            '    st.execute("SELECT * FROM t");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.fetch", READS_FROM, "resource::DATABASE::<dynamic>")
    assert not _has(rels, "A.fetch", WRITES_TO, "resource::DATABASE::<dynamic>")


def test_java_try_with_resources_binds_reader_handle(tmp_path: Path) -> None:
    # The idiomatic home of a Java handle is a try-with-resources header:
    # the `resource` declaration must bind exactly like a local declarator.
    files = {
        "A.java": (
            "import java.io.BufferedReader;\n"
            "import java.io.FileReader;\n"
            "class A {\n"
            "  void load() throws Exception {\n"
            "    try (BufferedReader br ="
            ' new BufferedReader(new FileReader("in.txt"))) {\n'
            "      String line = br.readLine();\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.load", READS_FROM, "resource::FILE::in.txt")


# Rust tests below.


def test_rust_file_open_read_to_string(tmp_path: Path) -> None:
    # `File::open("in.txt")?` binds through the try_expression wrapper.
    files = {
        "main.rs": (
            "use std::fs::File;\n"
            "use std::io::Read;\n"
            "fn load() -> std::io::Result<()> {\n"
            '    let mut f = File::open("in.txt")?;\n'
            "    let mut s = String::new();\n"
            "    f.read_to_string(&mut s)?;\n"
            "    Ok(())\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.load", READS_FROM, "resource::FILE::in.txt")


def test_rust_file_create_write_all_unwrap(tmp_path: Path) -> None:
    # `.unwrap()` on the constructor call must unwrap to the inner binding.
    files = {
        "main.rs": (
            "use std::fs::File;\n"
            "use std::io::Write;\n"
            "fn save(s: &str) {\n"
            '    let mut out = File::create("out.txt").unwrap();\n'
            "    out.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.save", WRITES_TO, "resource::FILE::out.txt")


def test_rust_fully_qualified_file_open(tmp_path: Path) -> None:
    files = {
        "main.rs": (
            "use std::io::Read;\n"
            "fn load() -> std::io::Result<()> {\n"
            '    let mut f = std::fs::File::open("cfg.toml")?;\n'
            "    let mut s = String::new();\n"
            "    f.read_to_string(&mut s)?;\n"
            "    Ok(())\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.load", READS_FROM, "resource::FILE::cfg.toml")


def test_rust_bufreader_wrapper(tmp_path: Path) -> None:
    # `BufReader::new(f)` wraps an existing handle: reads through the
    # wrapper attribute to the underlying file.
    files = {
        "main.rs": (
            "use std::fs::File;\n"
            "use std::io::{BufRead, BufReader};\n"
            "fn load() -> std::io::Result<()> {\n"
            '    let f = File::open("in.txt")?;\n'
            "    let mut r = BufReader::new(f);\n"
            "    let mut line = String::new();\n"
            "    r.read_line(&mut line)?;\n"
            "    Ok(())\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.load", READS_FROM, "resource::FILE::in.txt")


# C++ tests below.


def test_cpp_ofstream_insertion_writes(tmp_path: Path) -> None:
    # `std::ofstream out("out.txt"); out << line;` -- the declaration
    # constructs a FILE handle; `<<` on it is a WRITE to that file.
    files = {
        "main.cpp": (
            "#include <fstream>\n"
            "void save(const std::string& line) {\n"
            '    std::ofstream out("out.txt");\n'
            "    out << line;\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.save", WRITES_TO, "resource::FILE::out.txt")


def test_cpp_ifstream_extraction_reads(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <fstream>\n"
            "void load() {\n"
            '    std::ifstream in("in.txt");\n'
            "    std::string word;\n"
            "    in >> word;\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.load", READS_FROM, "resource::FILE::in.txt")


def test_cpp_ofstream_write_method(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <fstream>\n"
            "void save(const char* buf, int n) {\n"
            '    std::ofstream out("blob.bin");\n'
            "    out.write(buf, n);\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.save", WRITES_TO, "resource::FILE::blob.bin")


def test_cpp_vexing_parse_dynamic_identity(tmp_path: Path) -> None:
    # `std::ifstream dyn(path)` parses as a function_declarator (most vexing
    # parse); it still binds a FILE handle with a <dynamic> identity.
    files = {
        "main.cpp": (
            "#include <fstream>\n"
            "void load(const std::string& path) {\n"
            "    std::ifstream dyn(path);\n"
            "    std::string word;\n"
            "    dyn >> word;\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.load", READS_FROM, "resource::FILE::<dynamic>")


def test_cpp_arithmetic_shift_no_edge(tmp_path: Path) -> None:
    # `x << 2` on a non-handle base must not emit anything.
    files = {
        "main.cpp": (
            "#include <fstream>\n"
            "int shift(int x) {\n"
            "    int y = x << 2;\n"
            "    return y >> 1;\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert not any(a.endswith("main.shift") for a, _, _ in rels)


# Python derive tests below.


def test_python_cursor_derives_connection_handle(tmp_path: Path) -> None:
    # `cur = conn.cursor()` derives a same-resource DATABASE handle, so
    # cur.fetchall() reads the connection's database (issue #714).
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def fetch():\n"
            "    conn = sqlite3.connect('app.db')\n"
            "    cur = conn.cursor()\n"
            "    cur.fetchall()\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.fetch", READS_FROM, "resource::DATABASE::app.db")


def test_python_cursor_execute_select_reads(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def query():\n"
            "    conn = sqlite3.connect('app.db')\n"
            "    cur = conn.cursor()\n"
            "    cur.execute('SELECT * FROM t')\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.query", READS_FROM, "resource::DATABASE::app.db")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestGoSprintfUrls:
    """A Sprintf-built URL is the idiomatic Go way to add path parameters;
    its format verbs must become placeholders instead of discarding the
    whole URL (issue #885, the Go analogue of the f-string fix).
    """

    def test_http_get_sprintf_keeps_placeholder(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t"fmt"\n\t"net/http"\n)\n\n'
                "func fetchProduct(id int) (*http.Response, error) {\n"
                '\treturn http.Get(fmt.Sprintf("http://svc:8000/products/%d", id))\n'
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(
            rels,
            "main.fetchProduct",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/products/{*}",
        ), rels

    def test_double_percent_stays_literal(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t"fmt"\n\t"net/http"\n)\n\n'
                "func fetchSale(id int) (*http.Response, error) {\n"
                '\treturn http.Get(fmt.Sprintf("http://svc:8000/sale100%%/%v", id))\n'
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(
            rels,
            "main.fetchSale",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/sale100%/{*}",
        ), rels

    def test_dynamic_format_string_stays_dynamic(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t"fmt"\n\t"net/http"\n)\n\n'
                "func fetchAny(tpl string, id int) (*http.Response, error) {\n"
                "\treturn http.Get(fmt.Sprintf(tpl, id))\n"
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(
            rels, "main.fetchAny", READS_FROM, "resource::NETWORK::<dynamic>"
        ), rels

    def test_unrelated_wrapping_call_stays_dynamic(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t"net/http"\n\t"strings"\n)\n\n'
                "func fetchTrim(raw string) (*http.Response, error) {\n"
                "\treturn http.Get(strings.TrimSpace(raw))\n"
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(
            rels, "main.fetchTrim", READS_FROM, "resource::NETWORK::<dynamic>"
        ), rels

    def test_indexed_verbs_become_placeholders(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t"fmt"\n\t"net/http"\n)\n\n'
                "func fetchReview(id int, slug string) (*http.Response, error) {\n"
                "\treturn http.Get(fmt.Sprintf("
                '"http://svc:8000/products/%[1]d/reviews/%[2]s", id, slug))\n'
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(
            rels,
            "main.fetchReview",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/products/{*}/reviews/{*}",
        ), rels

    def test_indexed_width_and_precision_become_placeholder(
        self, tmp_path: Path
    ) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t"fmt"\n\t"net/http"\n)\n\n'
                "func fetchScore(w, p int, v float64) (*http.Response, error) {\n"
                "\treturn http.Get(fmt.Sprintf("
                '"http://svc:8000/scores/%[3]*.[2]*[1]f", v, p, w))\n'
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(
            rels,
            "main.fetchScore",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/scores/{*}",
        ), rels

    def test_raw_format_string_keeps_placeholder(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t"fmt"\n\t"net/http"\n)\n\n'
                "func fetchRaw(id int) (*http.Response, error) {\n"
                "\treturn http.Get(fmt.Sprintf("
                "`http://svc:8000/products/%d`, id))\n"
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(
            rels,
            "main.fetchRaw",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/products/{*}",
        ), rels

    def test_dot_imported_sprintf_keeps_placeholder(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import (\n\t. "fmt"\n\t"net/http"\n)\n\n'
                "func fetchDot(id int) (*http.Response, error) {\n"
                '\treturn http.Get(Sprintf("http://svc:8000/products/%d", id))\n'
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(
            rels,
            "main.fetchDot",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/products/{*}",
        ), rels


class TestGoRpcClientSinks:
    """Issue #912 slice 1: a connect-go generated client binding
    (`c := userv1connect.NewUserServiceClient(...)`) makes every exported
    method call on it an RPC sink on `resource::RPC::<Stem>.<Method>`, so
    interior service-mesh traffic becomes visible without URL literals.
    """

    _RPC = "resource::RPC::UserService.GetUser"

    def test_connect_client_call_emits_rpc_sink(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "example.com/gen/user/v1/userv1connect"\n\n'
                "func fetch(base string) {\n"
                "\tclient := userv1connect.NewUserServiceClient(nil, base)\n"
                "\tclient.GetUser(nil, nil)\n"
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(rels, "main.fetch", READS_FROM, self._RPC), rels
        assert _has(rels, "main.fetch", WRITES_TO, self._RPC), rels

    def test_alias_tracks_the_rpc_binding(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "example.com/gen/user/v1/userv1connect"\n\n'
                "func fetch(base string) {\n"
                "\tc := userv1connect.NewUserServiceClient(nil, base)\n"
                "\tg := c\n"
                "\tg.GetUser(nil, nil)\n"
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert _has(rels, "main.fetch", READS_FROM, self._RPC), rels

    def test_non_connect_package_is_not_rpc(self, tmp_path: Path) -> None:
        # `api.NewHTTPClient` lacks the connect-go package marker.
        files = {
            "main.go": (
                "package main\n\n"
                'import "example.com/api"\n\n'
                "func fetch(base string) {\n"
                "\tclient := api.NewHTTPClient(base)\n"
                "\tclient.Get(nil)\n"
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert not any("resource::RPC::" in b for _a, _r, b in rels), rels

    def test_unexported_method_is_ignored(self, tmp_path: Path) -> None:
        files = {
            "main.go": (
                "package main\n\n"
                'import "example.com/gen/user/v1/userv1connect"\n\n'
                "func fetch(base string) {\n"
                "\tclient := userv1connect.NewUserServiceClient(nil, base)\n"
                "\tclient.close()\n"
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert not any("resource::RPC::" in b for _a, _r, b in rels), rels

    def test_bare_new_client_without_package_is_not_rpc(self, tmp_path: Path) -> None:
        # A local helper `NewFooClient()` has no connect package qualifier.
        files = {
            "main.go": (
                "package main\n\n"
                "func fetch() {\n"
                "\tclient := NewFooClient()\n"
                "\tclient.Call(nil)\n"
                "}\n"
            ),
        }
        rels = _run_io(tmp_path, files)
        assert not any("resource::RPC::" in b for _a, _r, b in rels), rels
