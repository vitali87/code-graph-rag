# (H) Two inheritance-recall losses the thrift oracle re-measure surfaced
# (H) after the dangling-edge campaign:
# (H) 1. A src-root layout (setup.py maps lib/py/src -> package `thrift`)
# (H)    makes an import-mapped base like `thrift.Thrift.TProcessor` look
# (H)    project-internal while the real class qn is path-based
# (H)    (`thrift.src.Thrift.TProcessor`); the deferred resolver dropped what
# (H)    a unique whole-segment suffix match resolves to the real node.
# (H) 2. A written base that resolves nowhere in the index (`object`,
# (H)    Rust `Default`, generated `Iface`) is BY CONSTRUCTION defined outside
# (H)    the indexed tree; dropping it loses a syntactic inheritance fact the
# (H)    source declares. The written name (not the module-anchored guess)
# (H)    now emits onto an ExternalModule node, generalizing the JS-global
# (H)    and java.lang tables into a language-agnostic fallback.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater


def _inherits(mock_ingestor: MagicMock) -> set[tuple[str, str, str]]:
    return {
        (call.args[0][2], str(call.args[2][0]), call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.INHERITS.value)
    }


def test_src_root_base_suffix_resolves_to_real_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Mirrors thrift's layout: the package root is src/ but imports are
    # (H) written against the distribution name, which equals the project.
    src = temp_repo / "src"
    src.mkdir()
    (src / "Thrift.py").write_text("class TProcessor(object):\n    pass\n")
    (src / "TMultiplexedProcessor.py").write_text(
        "from "
        + temp_repo.name
        + ".Thrift import TProcessor\n\n\n"
        + "class TMultiplexedProcessor(TProcessor):\n    pass\n"
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    inherits = _inherits(mock_ingestor)
    expected = (
        f"{project}.src.TMultiplexedProcessor.TMultiplexedProcessor",
        cs.NodeLabel.CLASS.value,
        f"{project}.src.Thrift.TProcessor",
    )
    assert expected in inherits, inherits


def test_python_object_base_externalizes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "legacy.py").write_text("class Legacy(object):\n    pass\n")
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    inherits = _inherits(mock_ingestor)
    expected = (
        f"{project}.legacy.Legacy",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "object",
    )
    assert expected in inherits, inherits


def test_rust_bare_std_trait_impl_externalizes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "lib.rs").write_text(
        """
pub struct Config {
    level: u32,
}

impl Default for Config {
    fn default() -> Config {
        Config { level: 0 }
    }
}
"""
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    implements = {
        (call.args[0][2], str(call.args[2][0]), call.args[2][2])
        for call in get_relationships(
            mock_ingestor, cs.RelationshipType.IMPLEMENTS.value
        )
    }
    expected = (
        f"{project}.lib.Config",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "Default",
    )
    assert expected in implements, implements


def test_php_builtin_exception_base_externalizes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "AppError.php").write_text(
        """<?php
class AppError extends Exception
{
    public function report(): void
    {
    }
}
"""
    )
    run_updater(temp_repo, mock_ingestor, skip_if_missing="php")

    project = temp_repo.name
    inherits = _inherits(mock_ingestor)
    expected = (
        f"{project}.AppError.AppError",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "Exception",
    )
    assert expected in inherits, inherits


def test_rust_shadowed_std_trait_externalizes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) thrift's errors.rs: `pub enum Error` implements the std trait ALSO
    # (H) written `Error`; parse-time resolution lands on the enum itself and
    # (H) a self-edge is never real, but the WRITTEN bare name is still a
    # (H) syntactic fact and must externalize (the reference is to the
    # (H) shadowed outer name, not the child).
    (temp_repo / "errors.rs").write_text(
        """
use std::fmt;

pub enum Error {
    Transport(u32),
}

impl Error for Error {
    fn description(&self) -> &str {
        "err"
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "err")
    }
}
"""
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    implements = {
        (call.args[0][2], str(call.args[2][0]), call.args[2][2])
        for call in get_relationships(
            mock_ingestor, cs.RelationshipType.IMPLEMENTS.value
        )
    }
    expected = (
        f"{project}.errors.Error",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "Error",
    )
    assert expected in implements, implements
    for frm, _, to in implements:
        assert frm != to, implements
