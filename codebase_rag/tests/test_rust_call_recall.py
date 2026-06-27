from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] == cs.RelationshipType.CALLS:
            out.add((c.args[0][2], c.args[2][2]))
    return out


class TestRustTurbofishCalls:
    def test_turbofish_call_is_captured(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "tf.rs").write_text(
            "fn generic_function<T: Clone>(value: T) -> T { value }\n"
            "\n"
            "fn caller() {\n"
            "    let _ = generic_function::<i32>(10);\n"
            "}\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="rust")
        calls = _calls(mock_ingestor)

        assert any(
            caller.endswith(".caller") and callee.endswith(".generic_function")
            for caller, callee in calls
        ), f"turbofish call not captured; calls={sorted(calls)}"


class TestRustMacroCalls:
    def test_call_inside_macro_is_captured(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "mac.rs").write_text(
            "fn describe(x: i32) -> i32 { x }\n"
            "\n"
            "fn caller() {\n"
            '    println!("{}", describe(5));\n'
            "}\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="rust")
        calls = _calls(mock_ingestor)

        assert any(
            caller.endswith(".caller") and callee.endswith(".describe")
            for caller, callee in calls
        ), f"macro-internal call not captured; calls={sorted(calls)}"

    def test_bare_identifier_in_macro_is_not_a_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # (H) a plain value interpolated into a macro must not become a CALLS edge
        (temp_repo / "mac2.rs").write_text(
            "fn value() -> i32 { 1 }\n"
            "\n"
            "fn caller() {\n"
            "    let value = 5;\n"
            '    println!("{}", value);\n'
            "}\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="rust")
        calls = _calls(mock_ingestor)

        assert not any(
            caller.endswith(".caller") and callee.endswith(".value")
            for caller, callee in calls
        ), f"bare identifier wrongly captured as call; calls={sorted(calls)}"

    def test_struct_literal_in_macro_is_not_a_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # (H) `Widget { ... }` (token_tree starting with `{`) and `arr[..]` (starting
        # (H) with `[`) inside a macro are not calls; only `name(...)` is.
        (temp_repo / "mac3.rs").write_text(
            "struct Widget { n: i32 }\n"
            "fn helper() -> i32 { 1 }\n"
            "\n"
            "fn caller() {\n"
            '    println!("{}", Widget { n: helper() }.n);\n'
            "}\n",
            encoding="utf-8",
        )

        run_updater(temp_repo, mock_ingestor, skip_if_missing="rust")
        calls = _calls(mock_ingestor)

        # (H) the real call inside the macro is still captured
        assert any(
            caller.endswith(".caller") and callee.endswith(".helper")
            for caller, callee in calls
        ), f"macro call not captured; calls={sorted(calls)}"
        # (H) the struct literal `Widget { ... }` must not be a call
        assert not any(
            caller.endswith(".caller") and callee.endswith(".Widget")
            for caller, callee in calls
        ), f"struct literal wrongly captured as call; calls={sorted(calls)}"
