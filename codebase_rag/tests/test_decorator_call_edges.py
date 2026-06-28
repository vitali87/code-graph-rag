from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater


def _calls(mock_ingestor: MagicMock) -> list[tuple[str, str, str]]:
    # (H) CALLS edges as (caller_label, caller_qn, callee_qn).
    out: list[tuple[str, str, str]] = []
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] == cs.RelationshipType.CALLS:
            out.append((c.args[0][0], c.args[0][2], c.args[2][2]))
    return out


class TestDecoratorCallEdges:
    def test_bare_decorator_emits_module_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # (H) `@task` applies task(handler) at module load -> a module-level call.
        (temp_repo / "app.py").write_text(
            "def task(fn):\n    return fn\n\n\n@task\ndef handler():\n    return 1\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        calls = _calls(mock_ingestor)

        assert any(
            label == cs.NodeLabel.MODULE
            and caller.endswith(".app")
            and callee.endswith(".task")
            for label, caller, callee in calls
        ), f"no module->task decorator edge; calls={sorted(calls)}"

    def test_call_decorator_emits_module_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # (H) `@register(...)` also runs at module load.
        (temp_repo / "app.py").write_text(
            "def register(name):\n"
            "    def wrap(fn):\n"
            "        return fn\n"
            "    return wrap\n"
            "\n"
            "\n"
            '@register("x")\n'
            "def handler():\n"
            "    return 1\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        calls = _calls(mock_ingestor)

        assert any(
            label == cs.NodeLabel.MODULE
            and caller.endswith(".app")
            and callee.endswith(".register")
            for label, caller, callee in calls
        ), f"no module->register decorator edge; calls={sorted(calls)}"

    def test_class_decorator_emits_module_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # (H) a bare decorator on a class also runs at module load.
        (temp_repo / "app.py").write_text(
            "def deco(cls):\n    return cls\n\n\n@deco\nclass MyClass:\n    pass\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        calls = _calls(mock_ingestor)

        assert any(
            label == cs.NodeLabel.MODULE
            and caller.endswith(".app")
            and callee.endswith(".deco")
            for label, caller, callee in calls
        ), f"no module->deco class decorator edge; calls={sorted(calls)}"

    def test_alias_decorator_resolves_to_first_party(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # (H) `@alias` where `alias = task` still calls task at module load.
        (temp_repo / "app.py").write_text(
            "def task(fn):\n"
            "    return fn\n"
            "\n"
            "\n"
            "alias = task\n"
            "\n"
            "\n"
            "@alias\n"
            "def handler():\n"
            "    return 1\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        calls = _calls(mock_ingestor)

        assert any(
            label == cs.NodeLabel.MODULE
            and caller.endswith(".app")
            and callee.endswith(".task")
            for label, caller, callee in calls
        ), f"alias decorator not resolved; calls={sorted(calls)}"

    def test_decorator_on_nested_function_not_module_attributed(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # (H) a decorator on a function nested in another function runs when the
        # (H) outer function is called, not at module load -> no module edge.
        (temp_repo / "app.py").write_text(
            "def deco(fn):\n"
            "    return fn\n"
            "\n"
            "\n"
            "def outer():\n"
            "    @deco\n"
            "    def inner():\n"
            "        return 1\n"
            "\n"
            "    return inner\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        module_callees = {
            callee.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            for label, _caller, callee in _calls(mock_ingestor)
            if label == cs.NodeLabel.MODULE
        }

        assert "deco" not in module_callees

    def test_undecorated_function_has_no_decorator_edge(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "app.py").write_text(
            "def plain():\n    return 1\n\n\ndef other():\n    return 2\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        module_callees = {
            callee.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            for label, _caller, callee in _calls(mock_ingestor)
            if label == cs.NodeLabel.MODULE
        }

        assert "plain" not in module_callees
        assert "other" not in module_callees
