# (H) libc I/O for C (previously ZERO flow support) and C++: direct sinks
# (H) (getenv/printf/perror/scanf), fopen-bound FILE* handles, and the
# (H) call-shaped handle sinks (`fprintf(f, ...)` carries the handle as an
# (H) ARGUMENT), including the pre-bound stdout/stderr/stdin globals.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
WRITES_TO = cs.RelationshipType.WRITES_TO.value
FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
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
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in (READS_FROM, WRITES_TO, FLOWS_TO)
    }


def _has(rels: set[tuple[str, str, str]], caller: str, rel: str, resource: str) -> bool:
    return any(a.endswith(caller) and r == rel and b == resource for a, r, b in rels)


def test_c_getenv_reads_env(tmp_path: Path) -> None:
    files = {
        "main.c": (
            "#include <stdlib.h>\n"
            "void work(void) {\n"
            '    const char *s = getenv("SECRET");\n'
            "    (void)s;\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", READS_FROM, "resource::ENV::SECRET"), rels


def test_c_printf_writes_stdout_and_env_flows(tmp_path: Path) -> None:
    files = {
        "main.c": (
            "#include <stdio.h>\n"
            "#include <stdlib.h>\n"
            "void work(void) {\n"
            '    const char *s = getenv("SECRET");\n'
            '    printf("%s", s);\n'
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", WRITES_TO, "resource::STDOUT::<dynamic>"), rels
    assert (
        "resource::ENV::SECRET",
        FLOWS_TO,
        "resource::STDOUT::<dynamic>",
    ) in rels, rels


def test_c_fopen_bound_fprintf_writes_file(tmp_path: Path) -> None:
    files = {
        "main.c": (
            "#include <stdio.h>\n"
            "void work(void) {\n"
            '    FILE *f = fopen("out.txt", "w");\n'
            '    fprintf(f, "%d", 1);\n'
            "    fclose(f);\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", WRITES_TO, "resource::FILE::out.txt"), rels


def test_c_fopen_bound_fgets_reads_file(tmp_path: Path) -> None:
    files = {
        "main.c": (
            "#include <stdio.h>\n"
            "void work(char *buf) {\n"
            '    FILE *f = fopen("in.txt", "r");\n'
            "    fgets(buf, 100, f);\n"
            "    fclose(f);\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", READS_FROM, "resource::FILE::in.txt"), rels


def test_c_fprintf_stderr_writes_stderr(tmp_path: Path) -> None:
    files = {
        "main.c": (
            '#include <stdio.h>\nvoid work(void) {\n    fprintf(stderr, "boom");\n}\n'
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", WRITES_TO, "resource::STDERR::<dynamic>"), rels


def test_c_comment_before_handle_arg_still_resolves(tmp_path: Path) -> None:
    # (H) A comment inside the argument list is a named node in some grammars;
    # (H) it must not shift the handle-argument index.
    files = {
        "main.c": (
            "#include <stdio.h>\n"
            "void work(void) {\n"
            '    fprintf(/* stream */ stderr, "boom");\n'
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", WRITES_TO, "resource::STDERR::<dynamic>"), rels


def test_c_perror_writes_stderr(tmp_path: Path) -> None:
    files = {
        "main.c": ('#include <stdio.h>\nvoid work(void) {\n    perror("open");\n}\n')
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", WRITES_TO, "resource::STDERR::<dynamic>"), rels


def test_c_scanf_reads_stdin(tmp_path: Path) -> None:
    files = {
        "main.c": ('#include <stdio.h>\nvoid work(int *x) {\n    scanf("%d", x);\n}\n')
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", READS_FROM, "resource::STDIN::<dynamic>"), rels


def test_cpp_fopen_bound_fwrite_writes_file(tmp_path: Path) -> None:
    # (H) Real C++ uses the libc FILE* API too (spdlog's file sinks).
    files = {
        "main.cpp": (
            "#include <cstdio>\n"
            "void work(const char *data) {\n"
            '    FILE *f = std::fopen("log.txt", "w");\n'
            "    std::fwrite(data, 1, 8, f);\n"
            "    std::fclose(f);\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", WRITES_TO, "resource::FILE::log.txt"), rels


def test_c_unknown_handle_fprintf_is_dynamic_file(tmp_path: Path) -> None:
    # (H) A FILE* parameter is a file by signature even when its origin is
    # (H) unknown in this function.
    files = {
        "main.c": (
            '#include <stdio.h>\nvoid work(FILE *f) {\n    fprintf(f, "x");\n}\n'
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "main.work", WRITES_TO, "resource::FILE::<dynamic>"), rels
