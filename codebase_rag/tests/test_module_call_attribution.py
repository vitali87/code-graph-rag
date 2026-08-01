from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater


def _calls(mock_ingestor: MagicMock) -> list[tuple[str, str, str]]:
    # CALLS edges as (caller_label, caller_qn, callee_qn).
    out: list[tuple[str, str, str]] = []
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] == cs.RelationshipType.CALLS:
            caller_label, _caller_key, caller_qn = c.args[0]
            _callee_label, _callee_key, callee_qn = c.args[2]
            out.append((caller_label, caller_qn, callee_qn))
    return out


def _module_callees(calls: list[tuple[str, str, str]]) -> set[str]:
    return {
        callee.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        for label, _caller, callee in calls
        if label == cs.NodeLabel.MODULE
    }


class TestModuleCallAttribution:
    def test_nested_call_not_attributed_to_module(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "app.py").write_text(
            "def main():\n"
            "    used_by_main()\n"
            "\n"
            "\n"
            "def used_by_main():\n"
            "    return 1\n"
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    main()\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        calls = _calls(mock_ingestor)
        module_callees = _module_callees(calls)

        # the function-body call is attributed to the function, not the module
        assert any(
            caller.endswith(".main") and callee.endswith(".used_by_main")
            for _label, caller, callee in calls
        )
        # used_by_main is only called inside main(), never at module top level
        assert "used_by_main" not in module_callees

    def test_top_level_call_is_attributed_to_module(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "app.py").write_text(
            "def main():\n"
            "    used_by_main()\n"
            "\n"
            "\n"
            "def used_by_main():\n"
            "    return 1\n"
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    main()\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        module_callees = _module_callees(_calls(mock_ingestor))

        # the `if __name__ == "__main__": main()` call runs at module load
        assert "main" in module_callees

    def test_bare_module_level_call_attributed_to_module(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "app.py").write_text(
            "def setup():\n"
            "    return 1\n"
            "\n"
            "\n"
            "def helper():\n"
            "    return 2\n"
            "\n"
            "\n"
            "VALUE = setup()\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        module_callees = _module_callees(_calls(mock_ingestor))

        assert "setup" in module_callees
        # helper is never called at all -> no module edge to it
        assert "helper" not in module_callees

    def test_default_argument_call_attributed_to_module(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # a default-argument expression runs at module-load (definition) time,
        # not when the function body executes, so it is a module-level call.
        (temp_repo / "app.py").write_text(
            "def make_default():\n"
            "    return 1\n"
            "\n"
            "\n"
            "def with_default(x=make_default()):\n"
            "    return x\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="python")
        module_callees = _module_callees(_calls(mock_ingestor))

        assert "make_default" in module_callees

    def test_cpp_file_scope_initializer_call_attributed_to_module(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # a C++ file-scope initializer runs at load time, so its call is
        # module-attributed; a call inside a function body is not.
        (temp_repo / "app.cpp").write_text(
            "int nested_cpp() { return 1; }\n"
            "int top_cpp() { return 2; }\n"
            "int run_cpp() { return nested_cpp(); }\n"
            "int module_value = top_cpp();\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")
        calls = _calls(mock_ingestor)
        module_callees = _module_callees(calls)

        assert "top_cpp" in module_callees
        assert "nested_cpp" not in module_callees
        assert any(
            caller.endswith(".run_cpp") and callee.endswith(".nested_cpp")
            for _label, caller, callee in calls
        )
