"""The recorded answers change only when an answer does (RUST_REWRITE_PLAN.md
"Keeping the tests green", item 6).

`fixtures/reference_answers` holds what commands and MCP tools answer on the
pinned repositories of the reference graphs, written by
`scripts/reference_answers.py` and checked by the `Reference Graphs` CI job.
These tests cover what the job cannot: that an answer is written the same
wherever and whenever it was asked, that a malformed case is refused rather
than skipped, that the MCP client speaks the protocol a server expects, and
that every listed repository has its cases and answers.
"""

from __future__ import annotations

import json
import shlex
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs
from codebase_rag.types_defs import ReferenceCase, ReferenceRepo
from scripts import reference_answers
from scripts.reference_answers import (
    ANSWERS_DIR,
    McpSession,
    Placeholders,
    _Arguments,
    _query_env,
    answer_path,
    call_answer,
    cases_path,
    cli_answer,
    compare,
    difference,
    load_cases,
    read_answer,
    record,
    recorded_names,
    tools_answer,
    write_answer,
)
from scripts.reference_graphs import Indexed, load_manifest

ROOT = Path("/work/checkout/demo")
WORK = Path("/scratch/cgr-reference-abc")
REPO = ReferenceRepo(name="demo", language="python", repository="o/demo", commit="0")
WORKFLOW = reference_answers.PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
EXPORT = {
    "nodes": [
        {
            "node_id": 41,
            "labels": ["Function"],
            "properties": {"qualified_name": "demo.f"},
        },
        {
            "node_id": 7,
            "labels": ["Method"],
            "properties": {"qualified_name": "demo.C.m"},
        },
        {
            "node_id": 8,
            "labels": ["Method"],
            "properties": {"qualified_name": "demo.C.m"},
        },
    ],
    "relationships": [],
}


def _write_cases(directory: Path, cases: object) -> None:
    path = cases_path(REPO, directory)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(cases), encoding="utf-8")


def test_a_case_list_reads_back_as_written(tmp_path: Path) -> None:
    _write_cases(
        tmp_path,
        [
            {"name": "graph-callers", "cli": ["graph", "callers", "demo.f"]},
            {
                "name": "mcp-callers",
                "tool": "callers",
                "arguments": {"qualified_name": "demo.f"},
            },
            {"name": "mcp-tools-list", "tool": "tools/list"},
        ],
    )

    assert load_cases(REPO, tmp_path) == [
        ReferenceCase(name="graph-callers", cli=["graph", "callers", "demo.f"]),
        ReferenceCase(
            name="mcp-callers", tool="callers", arguments={"qualified_name": "demo.f"}
        ),
        ReferenceCase(name="mcp-tools-list", tool="tools/list"),
    ]


@pytest.mark.parametrize(
    "cases",
    [
        {"name": "x", "cli": []},
        [{"name": "Upper Case", "cli": ["stats"]}],
        [{"name": "../escape", "cli": ["stats"]}],
        [{"name": "twice", "cli": ["stats"]}, {"name": "twice", "tool": "callers"}],
        [{"name": "both", "cli": ["stats"], "tool": "callers"}],
        [{"name": "neither"}],
        [{"name": "typo", "cli": ["stats"], "argument": {}}],
        [{"name": "args-on-cli", "cli": ["stats"], "arguments": {}}],
        [{"name": "cli-text", "cli": "stats"}],
        [{"name": "args-list", "tool": "callers", "arguments": ["demo.f"]}],
    ],
)
def test_a_malformed_case_list_is_refused_whole(tmp_path: Path, cases: object) -> None:
    # A case that silently asked nothing would read as an answer kept.
    _write_cases(tmp_path, cases)

    with pytest.raises(SystemExit):
        load_cases(REPO, tmp_path)


def test_an_answer_names_no_directory_of_the_machine(tmp_path: Path) -> None:
    root = tmp_path / "cache" / "demo"
    root.mkdir(parents=True)
    work = tmp_path / "scratch"
    text = (
        f"read {root}/src/app.py, {root.as_posix()}/x, {json.dumps(str(root))}, "
        f"home {work}/home/graph.sqlite"
    )

    applied = Placeholders.build(root, work).apply(text)

    assert str(tmp_path) not in applied
    assert f"read {cs.REFERENCE_ROOT_PLACEHOLDER}/src/app.py" in applied
    assert f"home {cs.REFERENCE_ANSWERS_TMP_PLACEHOLDER}/home/graph.sqlite" in applied


def test_a_sibling_of_the_checkout_is_not_taken_for_it() -> None:
    applied = Placeholders.build(ROOT, WORK).apply(f"{ROOT}-old/a {ROOT}2 {ROOT}.")

    assert applied == f"{ROOT}-old/a {ROOT}2 {cs.REFERENCE_ROOT_PLACEHOLDER}."


def test_only_the_node_id_itself_is_written_as_a_placeholder() -> None:
    placeholders = Placeholders.build(ROOT, WORK, [41])
    node = cs.REFERENCE_ANSWERS_NODE_ID_PLACEHOLDER

    assert placeholders.apply("node ID 41.") == f"node ID {node}."
    assert placeholders.apply("node ID 41:") == f"node ID {node}:"
    assert placeholders.apply("1410 41.5 x41 v1.41 41") == f"1410 41.5 x41 v1.41 {node}"


def test_nothing_else_in_an_answer_is_rewritten() -> None:
    text = "2026-11-22 line 41 took 0.64s in /usr/lib/python3"

    assert Placeholders.build(ROOT, WORK).apply(text) == text


def test_a_cases_arguments_name_the_checkout_and_nodes_by_name() -> None:
    arguments = _Arguments(ROOT, EXPORT)

    assert arguments.text("--repo-path=<root>/src") == f"--repo-path={ROOT}/src"
    assert arguments.json(
        {"node_id": "<node_id:demo.f>", "paths": ["<root>", 3], "flag": True}
    ) == {"node_id": 41, "paths": [str(ROOT), 3], "flag": True}
    assert arguments.used_ids == [41]


@pytest.mark.parametrize("name", ["demo.C.m", "demo.missing"])
def test_a_node_id_argument_must_name_exactly_one_node(name: str) -> None:
    with pytest.raises(SystemExit, match=name):
        _Arguments(ROOT, EXPORT).text(f"<node_id:{name}>")


def test_a_command_lines_answer_holds_its_status_and_both_streams() -> None:
    answer = cli_answer(
        ["graph", "callers", "x", "--repo-path", "<root>"],
        4,
        "",
        "'x' is not in the graph.",
    )

    assert answer == (
        "$ cgr -q graph callers x --repo-path '<root>'\n"
        "status: 4\n"
        "--- stdout\n"
        "--- stderr\n"
        "'x' is not in the graph.\n"
    )
    assert cli_answer(["stats"], 0, "a\n\n", "")[-len("a\n\n--- stderr\n") :] == (
        "a\n\n--- stderr\n"
    )


def test_a_tool_calls_answer_holds_is_error_and_its_text_verbatim() -> None:
    text = '{\n  "error": "nothing under ' + str(ROOT) + '"\n}'
    response = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }

    answer = call_answer(
        "endpoints", {"b": 1, "a": "<root>"}, response, Placeholders.build(ROOT, WORK)
    )

    assert answer == (
        '> tools/call endpoints {"a": "<root>", "b": 1}\n'
        "isError: true\n"
        "--- text\n"
        '{\n  "error": "nothing under <root>"\n}\n'
    )


def test_a_protocol_error_is_an_answer_too() -> None:
    response = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "bad"}}

    answer = call_answer("callers", {}, response, Placeholders.build(ROOT, WORK))

    assert answer == "> tools/call callers {}\nerror: -32602 bad\n"


def test_the_tool_list_keeps_names_and_schemas_without_prose() -> None:
    response = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {
                    "name": "callers",
                    "description": "Who calls it.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "depth": {"type": "integer", "description": "How far."}
                        },
                    },
                }
            ]
        },
    }

    listed = json.loads(tools_answer(response).split("--- tools\n", 1)[1])

    assert listed == [
        {
            "name": "callers",
            "inputSchema": {
                "type": "object",
                "properties": {"depth": {"type": "integer"}},
            },
        }
    ]


def test_difference_is_empty_only_when_the_answers_agree() -> None:
    assert difference("a\nb\n", "a\nb\n") == []
    assert [line for line in difference("a\nb\n", "a\nc\n") if line[:1] in "+-"] == [
        "--- recorded",
        "+++ answered",
        "-b",
        "+c",
    ]


def test_an_answer_reads_back_byte_for_byte(tmp_path: Path) -> None:
    # A file the repository holds with CRLF line ends is read back with them.
    text = "> tools/call read_file {}\nisError: false\n--- text\nline\r\nnext \n"
    path = tmp_path / "demo" / "answer.txt"

    write_answer(path, text)

    assert read_answer(path) == text


def test_compare_names_a_missing_a_different_and_a_stale_answer(tmp_path: Path) -> None:
    cases = [
        ReferenceCase(name="same", cli=["stats"]),
        ReferenceCase(name="changed", cli=["stats"]),
        ReferenceCase(name="new", cli=["stats"]),
    ]
    write_answer(answer_path(REPO, "same", tmp_path), "a\n")
    write_answer(answer_path(REPO, "changed", tmp_path), "a\n")
    write_answer(answer_path(REPO, "gone", tmp_path), "a\n")

    status, lines = compare(
        REPO, cases, {"same": "a\n", "changed": "b\n", "new": "c\n"}, tmp_path
    )
    text = "\n".join(lines)

    assert status == cs.REFERENCE_EXIT_DIFFERENT
    assert "same" not in text
    assert "changed differs" in text
    assert "new: no recorded answer" in text
    assert "gone.txt answers no case" in text
    assert compare(REPO, cases[:1], {"same": "a\n"}, tmp_path)[0] == (
        cs.REFERENCE_EXIT_DIFFERENT
    )


def test_recording_drops_the_answers_no_case_asks_for(tmp_path: Path) -> None:
    write_answer(answer_path(REPO, "gone", tmp_path), "old\n")
    write_answer(answer_path(REPO, "kept", tmp_path), "old\n")

    record(REPO, {"kept": "new\n"}, tmp_path)

    assert recorded_names(REPO, tmp_path) == {"kept"}
    assert read_answer(answer_path(REPO, "kept", tmp_path)) == "new\n"


def _gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reference_answers, "environment_gate", lambda *args, **kwargs: ([], True)
    )


def test_a_recording_whose_runs_disagree_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _gate_open(monkeypatch)
    _write_cases(tmp_path, [{"name": "stats", "cli": ["stats"]}])
    write_answer(answer_path(REPO, "stats", tmp_path), "kept\n")
    monkeypatch.setattr(reference_answers, "fetch", lambda repo, cache: tmp_path)
    monkeypatch.setattr(
        reference_answers,
        "_answers",
        lambda cgr, repo, cases, checkout, runs: (
            {"stats": "first\n"},
            [(1, {"stats": "second\n"})],
        ),
    )

    status = reference_answers.run("record", [REPO], ["cgr"], tmp_path, 2, tmp_path)

    assert status == cs.REFERENCE_EXIT_DIFFERENT
    assert read_answer(answer_path(REPO, "stats", tmp_path)) == "kept\n"
    assert "PYTHONHASHSEED=1" in capsys.readouterr().out


def test_a_check_fails_on_a_different_answer_and_passes_on_the_same(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _gate_open(monkeypatch)
    _write_cases(tmp_path, [{"name": "stats", "cli": ["stats"]}])
    write_answer(answer_path(REPO, "stats", tmp_path), "same\n")
    monkeypatch.setattr(reference_answers, "fetch", lambda repo, cache: tmp_path)

    def answering(text: str) -> None:
        monkeypatch.setattr(
            reference_answers,
            "_answers",
            lambda cgr, repo, cases, checkout, runs: ({"stats": text}, []),
        )

    answering("same\n")
    assert reference_answers.run("check", [REPO], ["cgr"], tmp_path, 1, tmp_path) == 0

    answering("other\n")
    status = reference_answers.run("check", [REPO], ["cgr"], tmp_path, 1, tmp_path)
    assert status == cs.REFERENCE_EXIT_DIFFERENT


def test_a_repository_without_cases_fails_rather_than_passing_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _gate_open(monkeypatch)

    status = reference_answers.run("check", [REPO], ["cgr"], tmp_path, 1, tmp_path)

    assert status == cs.REFERENCE_EXIT_DIFFERENT


def test_a_machine_that_cannot_compare_asks_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        reference_answers, "environment_gate", lambda *args, **kwargs: ([], False)
    )

    def _not_fetched(repo: ReferenceRepo, cache: Path) -> Path:
        raise AssertionError("fetched on a machine that cannot compare")

    monkeypatch.setattr(reference_answers, "fetch", _not_fetched)

    status = reference_answers.run("check", [REPO], ["cgr"], tmp_path, 1, tmp_path)

    assert status == cs.REFERENCE_EXIT_FAILED


def test_the_commands_read_the_index_graph_with_a_fixed_console() -> None:
    done = Indexed(
        EXPORT,
        WORK,
        {"CGR_HOME": f"{WORK}/home", "FORCE_COLOR": "1", "tty_compatible": "1"},
    )

    env = _query_env(done, ROOT)

    assert env["CGR_HOME"] == f"{WORK}/home"
    assert "FORCE_COLOR" not in env
    assert "tty_compatible" not in env
    assert env["COLUMNS"] == cs.REFERENCE_ANSWERS_CONSOLE_ENV["COLUMNS"]
    assert env[cs.REFERENCE_ANSWERS_TARGET_REPO_ENV] == str(ROOT)


_FAKE_SERVER = textwrap.dedent(
    """
    import json, sys
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        if message["method"] == "exit-now":
            sys.exit(3)
        sys.stderr.write("log line\\n" * 20000)
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}))
        print(json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}))
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                          "result": {"echo": message}}), flush=True)
    """
)


@pytest.fixture
def fake_server(tmp_path: Path) -> list[str]:
    script = tmp_path / "server.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    return [sys.executable, str(script)]


def test_the_client_skips_notifications_and_other_ids(
    fake_server: list[str], tmp_path: Path
) -> None:
    # The fake server also writes plenty to stderr before each answer: a
    # client that did not drain it would stall on a full pipe.
    session = McpSession(fake_server, {}, tmp_path)
    try:
        session.notify("notifications/initialized")
        first = session.request("tools/list")
        second = session.request("tools/call", {"name": "callers", "arguments": {}})
    finally:
        session.close()

    assert first["id"] == 1
    assert first["result"] == {
        "echo": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    }
    result = second["result"]
    assert isinstance(result, dict)
    assert result["echo"] == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "callers", "arguments": {}},
    }


def test_a_server_that_exits_mid_call_is_a_failure_not_an_answer(
    fake_server: list[str], tmp_path: Path
) -> None:
    session = McpSession(fake_server, {}, tmp_path)
    try:
        with pytest.raises(SystemExit, match="exited before answering exit-now"):
            session.request("exit-now")
    finally:
        session.close()


def test_every_listed_repository_has_cases_and_an_answer_for_each() -> None:
    for repo in load_manifest():
        cases = load_cases(repo)
        names = {case["name"] for case in cases}

        assert cases, repo["name"]
        assert recorded_names(repo, ANSWERS_DIR) == names, repo["name"]


def test_every_answer_folder_belongs_to_a_listed_repository() -> None:
    listed = {repo["name"] for repo in load_manifest()}

    assert {path.name for path in ANSWERS_DIR.iterdir() if path.is_dir()} == listed


def test_the_cases_cover_every_read_only_surface_the_rewrite_must_answer() -> None:
    # The answers that need no model, no network and no edit: if one of
    # these went missing from every repository, nothing would hold it.
    asked_cli: set[str] = set()
    asked_mcp: set[str] = set()
    for repo in load_manifest():
        for case in load_cases(repo):
            if "cli" in case:
                words = case["cli"]
                asked_cli.add(" ".join(words[:2]) if words[0] == "graph" else words[0])
            else:
                asked_mcp.add(case["tool"])

    graph_commands = (
        "callers",
        "callees",
        "definition",
        "implementors",
        "importers",
        "overrides",
        "resolve",
        "tests-reaching",
    )
    assert {f"graph {command}" for command in graph_commands} | {
        "stats",
        "dead-code",
        "duplicates",
        "context",
    } <= asked_cli
    read_only_tools = set(cs.MCPToolName) - {
        # Edits to files or the graph.
        cs.MCPToolName.DELETE_PROJECT,
        cs.MCPToolName.WIPE_DATABASE,
        cs.MCPToolName.INDEX_REPOSITORY,
        cs.MCPToolName.UPDATE_REPOSITORY,
        cs.MCPToolName.REINGEST,
        cs.MCPToolName.RENAME,
        cs.MCPToolName.CHANGE_SIGNATURE,
        cs.MCPToolName.MOVE,
        cs.MCPToolName.SURGICAL_REPLACE_CODE,
        cs.MCPToolName.WRITE_FILE,
        cs.MCPToolName.STRUCTURAL_REPLACE,
        cs.MCPToolName.ANNOTATE,
        # A model or embeddings.
        cs.MCPToolName.ASK_AGENT,
        cs.MCPToolName.QUERY_CODE_GRAPH,
        cs.MCPToolName.SEMANTIC_SEARCH,
        # Traces, which no pinned repository has.
        cs.MCPToolName.EXPLAIN_TRACEBACK,
        cs.MCPToolName.RANK_ROOT_CAUSES,
    }
    assert {str(tool) for tool in read_only_tools} | {
        cs.REFERENCE_ANSWERS_TOOLS_LIST
    } <= asked_mcp


def test_the_refusals_a_script_branches_on_are_recorded() -> None:
    statuses: set[str] = set()
    for repo in load_manifest():
        for name in recorded_names(repo, ANSWERS_DIR):
            text = read_answer(answer_path(repo, name, ANSWERS_DIR))
            statuses.update(
                line for line in text.splitlines()[1:2] if line.startswith("status:")
            )

    for code in (
        cs.GRAPH_EXIT_UNKNOWN_PROJECT,
        cs.GRAPH_EXIT_UNKNOWN_TARGET,
        cs.GRAPH_EXIT_AMBIGUOUS_TARGET,
    ):
        assert cs.REFERENCE_ANSWERS_STATUS.format(code=code) in statuses


def test_no_recorded_answer_names_a_directory_of_the_machine_it_ran_on() -> None:
    # A path the placeholders missed would differ on every machine.
    markers = (cs.REFERENCE_CACHE_DIRNAME, "cgr-reference-", str(Path.home()))
    for repo in load_manifest():
        for name in recorded_names(repo, ANSWERS_DIR):
            text = read_answer(answer_path(repo, name, ANSWERS_DIR))
            for marker in markers:
                assert marker not in text, (repo["name"], name, marker)


def test_the_ci_job_checks_the_answers_twice() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding=cs.ENCODING_UTF8))
    steps = workflow["jobs"]["reference-graphs"]["steps"]
    commands = [shlex.split(step["run"]) for step in steps if "run" in step]

    assert [
        "uv",
        "run",
        "--no-sync",
        "python",
        "-m",
        "scripts.reference_answers",
        "check",
        "--runs",
        "2",
    ] in commands
    assert shlex.split(cs.REFERENCE_ANSWERS_REGENERATE)[-3:] == [
        "-m",
        "scripts.reference_answers",
        "record",
    ]
