"""Handle-based writes in the lean FLOWS_TO walk (issue #1204). A tainted value
written through a resource handle (`f := os.Create(..); f.Write(secret)`) must
emit a flow edge to the handle's resource, instead of the false NO_FLOW the walk
produced when it modelled only direct call sinks. First landing: Go."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


_LANG_BY_EXT = {
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "c_sharp",
    ".js": "javascript",
    ".ts": "typescript",
    ".lua": "lua",
    ".c": "c",
    ".cpp": "cpp",
}


def _run_flow(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    needed = {
        _LANG_BY_EXT[Path(rel).suffix]
        for rel in files
        if Path(rel).suffix in _LANG_BY_EXT
    }
    missing = [lang for lang in needed if lang not in parsers]
    if missing:
        pytest.skip(f"parser(s) not available: {missing}")
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


_ENV_FILE = ("resource::ENV::K", "resource::FILE::out.txt")


def test_go_tainted_write_through_file_handle(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("out.txt")\n'
            "\tf.Write([]byte(s))\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_go_tainted_write_string_through_file_handle(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("out.txt")\n'
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_go_untainted_handle_write_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\tf, _ := os.Create("out.txt")\n'
            '\tf.WriteString("literal")\n'
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_go_handle_reassignment_tracks_the_new_resource(tmp_path: Path) -> None:
    # Rebinding the handle var to a second file redirects the write; the taint
    # must reach the SECOND file only, never the first.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("first.txt")\n'
            '\tf, _ = os.Create("second.txt")\n'
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::second.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::first.txt") not in flows


def test_go_read_method_is_not_a_write_sink(tmp_path: Path) -> None:
    # A read through the handle is not a taint sink; only writes emit flow edges.
    # The arg is genuinely tainted (`[]byte(s)` preserves taint), so this validates
    # that `Read` is classified as a READ method, not merely that the arg is clean.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("out.txt")\n'
            "\tf.Read([]byte(s))\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_go_read_only_open_handle_write_emits_no_flow(tmp_path: Path) -> None:
    # `os.Open` is read-only, so `f.Write` on it is not a real write sink and must
    # emit no edge (constructor access mode gates write emission).
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak() {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Open("out.txt")\n'
            "\tf.Write([]byte(s))\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_go_conditional_rebind_emits_to_both_files(tmp_path: Path) -> None:
    # `f` may hold either handle after the if, so the write must reach BOTH files
    # (path-sensitive MAY merge of the handle bindings).
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak(cond bool) {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("first.txt")\n'
            "\tif cond {\n"
            '\t\tf, _ = os.Create("second.txt")\n'
            "\t}\n"
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::second.txt") in flows


def test_go_for_rebind_preserves_outer_flow(tmp_path: Path) -> None:
    # A rebind inside a for body may or may not run; the outer handle's flow must
    # survive the loop join alongside the loop-body handle.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak(items []int) {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("first.txt")\n'
            "\tfor range items {\n"
            '\t\tf, _ = os.Create("loop.txt")\n'
            "\t}\n"
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::loop.txt") in flows


def test_go_switch_rebind_preserves_outer_flow(tmp_path: Path) -> None:
    # A rebind in one switch arm (no default) leaves the outer handle live on the
    # implicit no-match path; the write must reach both.
    files = {
        "main.go": (
            "package main\n\n"
            'import "os"\n\n'
            "func leak(n int) {\n"
            '\ts := os.Getenv("K")\n'
            '\tf, _ := os.Create("first.txt")\n'
            "\tswitch n {\n"
            "\tcase 1:\n"
            '\t\tf, _ = os.Create("case1.txt")\n'
            "\t}\n"
            "\tf.WriteString(s)\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::case1.txt") in flows


# --- Rust (issue #1204, second language) ---
_RUST_ENV_FILE = ("resource::ENV::K", "resource::FILE::out.txt")


def test_rust_tainted_write_through_file_handle_unwrap(tmp_path: Path) -> None:
    # `File::create(p).unwrap()` yields the handle; `s.as_bytes()` carries s's
    # taint (value-preserving conversion), so the write reaches the file.
    files = {
        "m.rs": (
            "fn leak() {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::create("out.txt").unwrap();\n'
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE in _run_flow(tmp_path, files)


def test_rust_tainted_write_through_file_handle_try(tmp_path: Path) -> None:
    # The `?` operator unwraps the Result to the inner handle just like `.unwrap()`.
    files = {
        "m.rs": (
            "use std::io::Write;\n"
            "fn leak() -> std::io::Result<()> {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::create("out.txt")?;\n'
            "    f.write_all(s.as_bytes())?;\n"
            "    Ok(())\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE in _run_flow(tmp_path, files)


def test_rust_imported_file_create(tmp_path: Path) -> None:
    # `use std::fs::File;` + `File::create(..)` resolves through the import map to
    # the registered `std::fs::File::create` constructor.
    files = {
        "m.rs": (
            "use std::fs::File;\n"
            "fn leak() {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = File::create("out.txt").unwrap();\n'
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE in _run_flow(tmp_path, files)


def test_rust_read_only_open_handle_write_emits_no_flow(tmp_path: Path) -> None:
    # `File::open` is read-only, so a write through it is not a real sink.
    files = {
        "m.rs": (
            "fn leak() {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::open("out.txt").unwrap();\n'
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE not in _run_flow(tmp_path, files)


def test_rust_untainted_handle_write_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "m.rs": (
            "fn leak() {\n"
            '    let mut f = std::fs::File::create("out.txt").unwrap();\n'
            '    f.write_all("literal".as_bytes()).unwrap();\n'
            "}\n"
        )
    }
    assert _RUST_ENV_FILE not in _run_flow(tmp_path, files)


def test_rust_conditional_rebind_emits_to_both_files(tmp_path: Path) -> None:
    # Path-sensitive handle bindings apply to Rust too: a rebind in the if arm
    # leaves both handles live, so the write reaches both files.
    files = {
        "m.rs": (
            "fn leak(cond: bool) {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::create("first.txt").unwrap();\n'
            "    if cond {\n"
            '        f = std::fs::File::create("second.txt").unwrap();\n'
            "    }\n"
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::second.txt") in flows


def test_rust_taint_does_not_cross_a_terminal_method(tmp_path: Path) -> None:
    # `s.as_bytes().len()` is a usize, not the secret: the value-preserving
    # recursion must stop at `.len()` (a terminal method) and not propagate s's
    # taint to the write (CodeRabbit review, #1204).
    files = {
        "m.rs": (
            "fn leak() {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = std::fs::File::create("out.txt").unwrap();\n'
            "    f.write_all(&[s.as_bytes().len() as u8]).unwrap();\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE not in _run_flow(tmp_path, files)


def test_rust_parenthesized_constructor_is_unwrapped(tmp_path: Path) -> None:
    # A parenthesized constructor `(File::create(p).unwrap())` wraps the handle
    # expression; the binder peels the parens (and the Result) to reach it.
    files = {
        "m.rs": (
            "fn leak() {\n"
            '    let s = std::env::var("K").unwrap();\n'
            '    let mut f = (std::fs::File::create("out.txt").unwrap());\n'
            "    f.write_all(s.as_bytes()).unwrap();\n"
            "}\n"
        )
    }
    assert _RUST_ENV_FILE in _run_flow(tmp_path, files)


# --- Java (issue #1204, third language; `new`-shaped handles) ---


def test_java_tainted_write_through_new_filewriter(tmp_path: Path) -> None:
    # `new FileWriter("out.txt")` is a WRITE handle; a tainted `.write(s)` through
    # it reaches the file (the common `new`-shaped idiom the call-shaped walk missed).
    files = {
        "A.java": (
            "import java.io.FileWriter;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    FileWriter w = new FileWriter("out.txt");\n'
            "    w.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_java_write_through_printwriter_println(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "import java.io.PrintWriter;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    PrintWriter w = new PrintWriter("out.txt");\n'
            "    w.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_java_wrapper_around_nested_constructor(tmp_path: Path) -> None:
    # `new BufferedWriter(new FileWriter("out.txt"))`: the wrapper delegates its
    # resource to the nested constructor at arg0.
    files = {
        "A.java": (
            "import java.io.*;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    BufferedWriter w = new BufferedWriter(new FileWriter("out.txt"));\n'
            "    w.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_java_wrapper_around_bound_variable(tmp_path: Path) -> None:
    # `new BufferedWriter(fw)`: the wrapper inherits the handle already bound to the
    # variable `fw`, so the write reaches its file.
    files = {
        "A.java": (
            "import java.io.*;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    FileWriter fw = new FileWriter("out.txt");\n'
            "    BufferedWriter w = new BufferedWriter(fw);\n"
            "    w.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_java_printwriter_around_new_file_identity(tmp_path: Path) -> None:
    # `new PrintWriter(new File("out.txt"))`: `new File` is not a handle but carries
    # the resource identity, resolved through the wrapper.
    files = {
        "A.java": (
            "import java.io.*;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    PrintWriter w = new PrintWriter(new File("out.txt"));\n'
            "    w.println(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_java_files_factory_pathof_identity(tmp_path: Path) -> None:
    # The call-shaped `Files.newBufferedWriter(Path.of("out.txt"))` carries its
    # identity one level down in the `Path.of` factory call.
    files = {
        "A.java": (
            "import java.nio.file.*;\n"
            "import java.io.*;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    BufferedWriter w = Files.newBufferedWriter(Path.of("out.txt"));\n'
            "    w.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_java_files_factory_static_import_identity(tmp_path: Path) -> None:
    # A static-imported factory (`import static java.nio.file.Path.of`) spells the
    # call bare (`of("out.txt")`); the identity lookup resolves it through the import
    # map to `Path.of`, so the concrete path is still recovered (Greptile review).
    files = {
        "A.java": (
            "import static java.nio.file.Path.of;\n"
            "import java.nio.file.Files;\n"
            "import java.io.BufferedWriter;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    BufferedWriter w = Files.newBufferedWriter(of("out.txt"));\n'
            "    w.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_java_read_only_filereader_emits_no_flow(tmp_path: Path) -> None:
    # `new FileReader` is a READ-only handle, so it never binds as a write sink.
    # The write-named `.write(s)` carries genuine taint, so this fails if the
    # `IODirection.READ` gate is removed (not merely because the arg is clean).
    files = {
        "A.java": (
            "import java.io.FileReader;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    FileReader r = new FileReader("out.txt");\n'
            "    r.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_java_untainted_new_handle_write_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "import java.io.FileWriter;\n"
            "class A {\n"
            "  void leak() throws Exception {\n"
            '    FileWriter w = new FileWriter("out.txt");\n'
            '    w.write("literal");\n'
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_java_conditional_rebind_emits_to_both_files(tmp_path: Path) -> None:
    # A rebind in the if arm leaves both `new`-shaped handles live at the write, so
    # the taint reaches both files (path-sensitive MAY merge, as for Go/Rust).
    files = {
        "A.java": (
            "import java.io.FileWriter;\n"
            "class A {\n"
            "  void leak(boolean cond) throws Exception {\n"
            '    String s = System.getenv("K");\n'
            '    FileWriter w = new FileWriter("first.txt");\n'
            '    if (cond) { w = new FileWriter("second.txt"); }\n'
            "    w.write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::second.txt") in flows


# --- C# (issue #1204, fourth language; `new`-shaped handles only) ---


def test_csharp_tainted_write_through_new_streamwriter(tmp_path: Path) -> None:
    # C# has no call-shaped lean handle constructors; every handle is `new`-shaped.
    files = {
        "A.cs": (
            "using System;\n"
            "using System.IO;\n"
            "class A {\n"
            "  void Leak() {\n"
            '    var s = Environment.GetEnvironmentVariable("K");\n'
            '    var w = new StreamWriter("out.txt");\n'
            "    w.Write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_csharp_write_through_streamwriter_async(tmp_path: Path) -> None:
    # The async sibling `WriteLineAsync` is a WRITE method too; the call sits inside
    # an `await`, which the walk descends into.
    files = {
        "A.cs": (
            "using System;\n"
            "using System.IO;\n"
            "class A {\n"
            "  async void Leak() {\n"
            '    var s = Environment.GetEnvironmentVariable("K");\n'
            '    var w = new StreamWriter("out.txt");\n'
            "    await w.WriteLineAsync(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_csharp_write_through_filestream(tmp_path: Path) -> None:
    # `new FileStream` is a mode-flexible READ_WRITE handle (a sound may-write).
    files = {
        "A.cs": (
            "using System;\n"
            "using System.IO;\n"
            "class A {\n"
            "  async void Leak() {\n"
            '    var s = Environment.GetEnvironmentVariable("K");\n'
            '    var w = new FileStream("out.txt", FileMode.Create);\n'
            "    await w.WriteAsync(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_csharp_read_only_streamreader_emits_no_flow(tmp_path: Path) -> None:
    # `new StreamReader` is READ-only, so it never binds as a write sink. The
    # write-named `.Write(s)` carries genuine taint, so this fails if the
    # `IODirection.READ` gate is removed (not merely because the arg is clean).
    files = {
        "A.cs": (
            "using System;\n"
            "using System.IO;\n"
            "class A {\n"
            "  void Leak() {\n"
            '    var s = Environment.GetEnvironmentVariable("K");\n'
            '    var r = new StreamReader("out.txt");\n'
            "    r.Write(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_csharp_untainted_new_handle_write_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System;\n"
            "using System.IO;\n"
            "class A {\n"
            "  void Leak() {\n"
            '    var w = new StreamWriter("out.txt");\n'
            '    w.Write("literal");\n'
            "  }\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def _js_ts_write_stream(rel: str) -> dict[str, str]:
    return {
        rel: (
            "function leak() {\n"
            "  const s = process.env.K;\n"
            "  const ws = fs.createWriteStream('out.txt');\n"
            "  ws.write(s);\n"
            "}\n"
        )
    }


# --- JS/TS (issue #1204; coverage predates the Go/Rust/Java/C# increments via the
# call-shaped `fs.createWriteStream` handle table, but was previously untested). ---


def test_js_tainted_write_through_write_stream(tmp_path: Path) -> None:
    # `fs.createWriteStream('out.txt')` is a WRITE handle; `ws.write(process.env.K)`
    # reaches the file. `process.env.K` is the ENV read source.
    assert _ENV_FILE in _run_flow(tmp_path, _js_ts_write_stream("a.js"))


def test_ts_tainted_write_through_write_stream(tmp_path: Path) -> None:
    assert _ENV_FILE in _run_flow(tmp_path, _js_ts_write_stream("a.ts"))


def test_js_untainted_write_stream_emits_no_flow(tmp_path: Path) -> None:
    # A literal write through the handle taints nothing, so the walk emits no
    # FLOWS_TO edge at all (not merely "not the ENV->FILE one").
    files = {
        "a.js": (
            "function leak() {\n"
            "  const ws = fs.createWriteStream('out.txt');\n"
            "  ws.write('literal');\n"
            "}\n"
        )
    }
    assert _run_flow(tmp_path, files) == set()


# --- C / C++ (issue #1204; arg-shaped libc FILE* writes) ---


def test_c_tainted_write_through_fwrite_handle(tmp_path: Path) -> None:
    # `fwrite(s, 1, n, f)` carries the handle at arg 3 and the tainted data at arg 0;
    # the data flows to the bound `fopen` handle's file.
    files = {
        "c.c": (
            "#include <stdio.h>\n"
            "#include <stdlib.h>\n"
            "#include <string.h>\n"
            "void leak() {\n"
            '  const char* s = getenv("K");\n'
            '  FILE* f = fopen("out.txt", "w");\n'
            "  fwrite(s, 1, strlen(s), f);\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_c_tainted_write_through_fprintf_handle(tmp_path: Path) -> None:
    # `fprintf(f, fmt, s)` carries the handle at arg 0 and the tainted data at arg 2.
    files = {
        "c.c": (
            "#include <stdio.h>\n"
            "#include <stdlib.h>\n"
            "void leak() {\n"
            '  const char* s = getenv("K");\n'
            '  FILE* f = fopen("out.txt", "w");\n'
            '  fprintf(f, "%s", s);\n'
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_c_fprintf_to_stderr_stream(tmp_path: Path) -> None:
    # `fprintf(stderr, fmt, s)` targets the pre-bound stderr stream, no fopen needed.
    files = {
        "c.c": (
            "#include <stdio.h>\n"
            "#include <stdlib.h>\n"
            "void leak() {\n"
            '  const char* s = getenv("K");\n'
            '  fprintf(stderr, "%s", s);\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::STDERR::<dynamic>") in flows


def test_cpp_tainted_write_through_fwrite_handle(tmp_path: Path) -> None:
    # `std::fwrite` resolves under the `std::`-qualified spelling too.
    files = {
        "a.cpp": (
            "#include <cstdio>\n"
            "#include <cstdlib>\n"
            "#include <cstring>\n"
            "void leak() {\n"
            '  const char* s = std::getenv("K");\n'
            '  FILE* f = std::fopen("out.txt", "w");\n'
            "  std::fwrite(s, 1, std::strlen(s), f);\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_c_untainted_fwrite_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "c.c": (
            "#include <stdio.h>\n"
            "void leak() {\n"
            '  FILE* f = fopen("out.txt", "w");\n'
            '  fwrite("literal", 1, 7, f);\n'
            "}\n"
        )
    }
    assert _run_flow(tmp_path, files) == set()


def test_c_fwrite_metadata_arg_is_not_payload(tmp_path: Path) -> None:
    # `fwrite(buffer, size, count, stream)` writes only arg 0. `getenv("K")` sits at
    # the `count` position (arg 2) as a directly-modeled ENV source, with a literal
    # buffer: it is control metadata, not exfiltrated data, so it must emit no flow to
    # the file (Greptile + CodeRabbit review, #1204).
    files = {
        "c.c": (
            "#include <stdio.h>\n"
            "#include <stdlib.h>\n"
            "void leak() {\n"
            '  FILE* f = fopen("out.txt", "w");\n'
            '  fwrite("literal", 1, getenv("K"), f);\n'
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert not any(src == "resource::ENV::K" and "FILE" in dst for src, dst in flows)


def test_c_project_defined_fwrite_is_not_treated_as_libc(tmp_path: Path) -> None:
    # A project function named `fwrite` resolves to that definition and is analysed
    # as an ordinary callee; it must NOT be swallowed by the libc arg-handle model
    # (no spurious `FILE:<dynamic>` write), so interprocedural analysis is preserved
    # (Greptile review, #1204).
    files = {
        "c.c": (
            "#include <stdlib.h>\n"
            "void fwrite(const char* data, int a, int b, void* f) { }\n"
            "void leak() {\n"
            '  const char* s = getenv("K");\n'
            "  fwrite(s, 1, 2, 0);\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::<dynamic>") not in flows


def test_cpp_tainted_write_through_ofstream_insertion(tmp_path: Path) -> None:
    # `std::ofstream out("out.txt")` binds a FILE handle via its type declaration;
    # `out << s` inserts the ENV-tainted `s` into that file (issue #1220).
    files = {
        "a.cpp": (
            "#include <fstream>\n"
            "#include <cstdlib>\n"
            "void leak() {\n"
            '  const char* s = std::getenv("K");\n'
            '  std::ofstream out("out.txt");\n'
            "  out << s;\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_cpp_tainted_write_through_ofstream_chain(tmp_path: Path) -> None:
    # A chained insertion `out << a << s << b` routes every operand; the tainted one
    # still reaches the file.
    files = {
        "a.cpp": (
            "#include <fstream>\n"
            "#include <cstdlib>\n"
            "void leak() {\n"
            '  const char* s = std::getenv("K");\n'
            '  std::ofstream out("out.txt");\n'
            '  out << "prefix" << s << "\\n";\n'
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_cpp_tainted_write_through_ofstream_method(tmp_path: Path) -> None:
    # `out.write(s, n)` is a method write on the bound ofstream handle.
    files = {
        "a.cpp": (
            "#include <fstream>\n"
            "#include <cstdlib>\n"
            "#include <cstring>\n"
            "void leak() {\n"
            '  const char* s = std::getenv("K");\n'
            '  std::ofstream out("out.txt");\n'
            "  out.write(s, strlen(s));\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_cpp_ofstream_move_assignment_rebind(tmp_path: Path) -> None:
    # `out = std::ofstream("out.txt")` is a call-shaped move-assignment rebind (not a
    # type declaration); the ofstream call constructor binds the handle so a later
    # `out << s` still reaches the file (CodeRabbit review, #1220).
    files = {
        "a.cpp": (
            "#include <fstream>\n"
            "#include <cstdlib>\n"
            "void leak() {\n"
            '  const char* s = std::getenv("K");\n'
            "  std::ofstream out;\n"
            '  out = std::ofstream("out.txt");\n'
            "  out << s;\n"
            "}\n"
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_cpp_cout_insertion_still_writes_stdout(tmp_path: Path) -> None:
    # The bound-handle branch must not regress the cout/cerr stream sinks: `cout << s`
    # still flows to STDOUT (its base is a stream sink, not a bound handle).
    files = {
        "a.cpp": (
            "#include <iostream>\n"
            "#include <cstdlib>\n"
            "void leak() {\n"
            '  const char* s = std::getenv("K");\n'
            "  std::cout << s;\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::STDOUT::<dynamic>") in flows


def test_cpp_untainted_ofstream_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "a.cpp": (
            "#include <fstream>\n"
            "void leak() {\n"
            '  std::ofstream out("out.txt");\n'
            '  out << "literal";\n'
            "}\n"
        )
    }
    assert _run_flow(tmp_path, files) == set()


def test_c_fread_is_not_a_write_sink(tmp_path: Path) -> None:
    # `fread` reads FROM the file into a buffer; it is a READ arg-sink, not a write
    # destination, so a tainted argument routes no flow to the file.
    files = {
        "c.c": (
            "#include <stdio.h>\n"
            "#include <stdlib.h>\n"
            "void leak() {\n"
            '  char* s = getenv("K");\n'
            '  FILE* f = fopen("out.txt", "w");\n'
            "  fread(s, 1, 10, f);\n"
            "}\n"
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


# --- Lua (issue #1204; `io.open` handle + `:` method-call writes) ---


def test_lua_tainted_write_through_file_handle(tmp_path: Path) -> None:
    # `io.open("out.txt", "w")` binds a FILE handle; `f:write(s)` (a `:` method call)
    # writes the ENV-tainted `s` to the file.
    files = {
        "a.lua": (
            'local s = os.getenv("K")\nlocal f = io.open("out.txt", "w")\nf:write(s)\n'
        )
    }
    assert _ENV_FILE in _run_flow(tmp_path, files)


def test_lua_untainted_handle_write_emits_no_flow(tmp_path: Path) -> None:
    files = {"a.lua": ('local f = io.open("out.txt", "w")\nf:write("literal")\n')}
    assert _run_flow(tmp_path, files) == set()


def test_lua_read_method_is_not_a_write_sink(tmp_path: Path) -> None:
    # `f:read(...)` is a READ method, so even a tainted argument emits no flow edge;
    # only writes through the handle are sinks.
    files = {
        "a.lua": (
            'local s = os.getenv("K")\nlocal f = io.open("out.txt", "w")\nf:read(s)\n'
        )
    }
    assert _ENV_FILE not in _run_flow(tmp_path, files)


def test_lua_conditional_rebind_emits_to_both_files(tmp_path: Path) -> None:
    # A rebind of the handle in the if arm leaves both handles live at the write, so
    # the taint reaches both files (path-sensitive MAY merge, as for Go/Rust/Java).
    files = {
        "a.lua": (
            'local s = os.getenv("K")\n'
            'local f = io.open("first.txt", "w")\n'
            "if cond then\n"
            '  f = io.open("second.txt", "w")\n'
            "end\n"
            "f:write(s)\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::FILE::first.txt") in flows
    assert ("resource::ENV::K", "resource::FILE::second.txt") in flows


def test_csharp_sqlcommand_inherits_every_merged_connection(tmp_path: Path) -> None:
    # `new SqlCommand(sql, conn)` inherits the connection's identity from arg1. When
    # `conn` was branch-merged over two connections, the command must inherit BOTH,
    # so a tainted execute reaches both databases (CodeRabbit review, #1204).
    files = {
        "A.cs": (
            "using System;\n"
            "using Microsoft.Data.SqlClient;\n"
            "class A {\n"
            "  void Leak(bool c) {\n"
            '    var s = Environment.GetEnvironmentVariable("K");\n'
            '    var conn = new SqlConnection("db1");\n'
            '    if (c) { conn = new SqlConnection("db2"); }\n'
            '    var cmd = new SqlCommand("q", conn);\n'
            "    cmd.ExecuteNonQuery(s);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::K", "resource::DATABASE::db1") in flows
    assert ("resource::ENV::K", "resource::DATABASE::db2") in flows
