# (H) Rust inheritance was uncaptured: `impl Trait for Type` means Type
# (H) IMPLEMENTS Trait, and a supertrait bound `trait Sub: Super` means Sub
# (H) INHERITS Super. cgr emitted neither (impl blocks and trait bounds were
# (H) never turned into inheritance edges).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_RS = """\
pub trait Shape {}
pub trait Drawable: Shape {}

pub struct Circle;

impl Shape for Circle {}
impl Drawable for Circle {}
"""


def _pairs(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {
        (call[0][0][2], call[0][2][2]) for call in get_relationships(mock_ingestor, rel)
    }


def test_rust_impl_and_supertrait_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_inh"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_inh"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(encoding="utf-8", data=_RS)
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    inherits = _pairs(mock_ingestor, RelationshipType.INHERITS.value)
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_inh.src.lib"

    # (H) impl Trait for Type -> Type IMPLEMENTS Trait.
    assert (f"{base}.Circle", f"{base}.Shape") in implements, implements
    assert (f"{base}.Circle", f"{base}.Drawable") in implements, implements
    # (H) Supertrait bound -> Sub INHERITS Super.
    assert (f"{base}.Drawable", f"{base}.Shape") in inherits, inherits
