# A bodied templated C++ class is captured twice by the class query: once as the
# template_declaration wrapper (no `body` field, no methods attach) and once as the
# inner class_specifier (has the body, methods attach). Registering both suffixes the
# second with `@line`, so methods live under `Box@N.*` while callers reference the
# natural `Box.*` and the class is unreachable. It must register exactly ONCE, under
# its natural qn, with methods and member calls on it.
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


def _write(project: Path, body: str) -> None:
    (project / "box.cpp").write_text(body, encoding="utf-8")


def test_templated_class_registers_once_under_natural_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(
        temp_repo,
        "template<typename T>\n"
        "class Box {\n"
        "  public:\n"
        "    void open() { close(); }\n"
        "    void close() {}\n"
        "};\n",
    )
    run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    classes = {
        c
        for c in get_node_names(mock_ingestor, "Class")
        if c.endswith("Box") or ".Box@" in c or ".Box" in c
    }
    # exactly one Box class node, no @line duplicate
    box_nodes = {c for c in classes if c.rsplit(".", 1)[-1].split("@")[0] == "Box"}
    assert box_nodes == {f"{temp_repo.name}.box.Box"}, box_nodes

    methods = get_node_names(mock_ingestor, "Method")
    assert f"{temp_repo.name}.box.Box.open" in methods, sorted(
        m for m in methods if "Box" in m
    )
    assert f"{temp_repo.name}.box.Box.close" in methods


def test_templated_class_member_call_resolves_to_natural_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(
        temp_repo,
        "template<typename T>\n"
        "class Box {\n"
        "  public:\n"
        "    void open() { close(); }\n"
        "    void close() {}\n"
        "};\n",
    )
    run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    p = temp_repo.name
    # the in-class `open() { close(); }` edge must land on the same natural qn
    assert (f"{p}.box.Box.open", f"{p}.box.Box.close") in calls, sorted(
        e for e in calls if "Box" in e[0]
    )
