# The object-literal sibling of test_ts_overload_variant_inline_refs: a
# redeclared function registers its second body under a duplicate-variant qn
# (`useStore@2`), while the object-literal function values and shorthand
# methods inside it register under the NATURAL qn (`useStore.onSuccess`).
# The inline-argument path already tries the variant-stripped scope; the
# object-literal paths built candidates from the variant qn alone, so these
# values got no REFERENCES edge and read as dead.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_SOURCE = (
    "export function useStore(api: any) { return api }\n"
    "export function useStore(api: any, selector?: any) {\n"
    "  const handlers = {\n"
    "    onSuccess: () => selector(api),\n"
    "    'on-error': () => api.reset(),\n"
    "    flush() { return api.flush() },\n"
    "  }\n"
    "  return api.subscribe(handlers)\n"
    "}\n"
)


def _referenced_from_variant(temp_repo: Path, mock_ingestor: MagicMock) -> set[str]:
    root = temp_repo / "zovl"
    root.mkdir(parents=True)
    (root / "react.ts").write_text(_SOURCE, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return {
        c.args[2][2]
        for c in get_relationships(mock_ingestor, "REFERENCES")
        if c.args[0][2].endswith(".useStore@2")
    }


def test_variant_caller_references_its_keyed_value_arrow(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    refs = _referenced_from_variant(temp_repo, mock_ingestor)
    assert any(t.endswith(".useStore.onSuccess") for t in refs), sorted(refs)


def test_variant_caller_references_its_string_keyed_value_arrow(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    refs = _referenced_from_variant(temp_repo, mock_ingestor)
    assert any(".useStore.anonymous_4_" in t for t in refs), sorted(refs)


def test_variant_caller_references_its_shorthand_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    refs = _referenced_from_variant(temp_repo, mock_ingestor)
    assert any(t.endswith(".useStore.flush") for t in refs), sorted(refs)
