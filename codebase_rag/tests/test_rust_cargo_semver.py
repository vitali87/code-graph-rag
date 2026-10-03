"""Cargo's version requirements, as the patch check reads them (PR #2791).

A root `[patch]` stands in for a dependency only when the patched package's
version meets the dependency's requirement, so the matcher must follow
Cargo's default caret semantics and its other operators, and must say no to
anything it cannot read.
"""

import pytest

from codebase_rag.parsers.rs.cargo_semver import satisfies


@pytest.mark.parametrize(
    ("version", "requirement", "expected"),
    [
        # A bare version is a caret requirement.
        ("1.0.0", "1", True),
        ("1.2.0", "1", True),
        ("0.1.0", "1", False),
        ("2.0.0", "1", False),
        ("1.2.3", "1.2", True),
        ("1.1.9", "1.2", False),
        ("1.2.3", "^1.2.3", True),
        ("1.2.2", "^1.2.3", False),
        ("1.9.0", "^1.2.3", True),
        # Below 1.0.0 the leftmost non-zero part is the compatible one.
        ("0.2.5", "0.2", True),
        ("0.3.0", "0.2", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        ("0.0.9", "0.0", True),
        ("0.1.0", "0.0", False),
        ("0.9.0", "0", True),
        ("1.0.0", "0", False),
        # Tilde, exact and comparison operators.
        ("1.2.9", "~1.2.3", True),
        ("1.3.0", "~1.2.3", False),
        ("1.9.0", "~1", True),
        ("2.0.0", "~1", False),
        ("1.2.3", "=1.2.3", True),
        ("1.2.4", "=1.2.3", False),
        ("1.2.9", "=1.2", True),
        ("1.3.0", "=1.2", False),
        ("1.4.0", ">=1.2, <1.5", True),
        ("1.5.0", ">=1.2, <1.5", False),
        ("1.1.0", ">= 1.2 , < 1.5", False),
        ("1.3.0", ">1.2", True),
        ("1.2.9", ">1.2", False),
        ("1.2.4", ">1.2.3", True),
        ("1.2.3", ">1.2.3", False),
        ("1.2.9", "<=1.2", True),
        ("1.3.0", "<=1.2", False),
        ("1.2.3", "<=1.2.3", True),
        # Wildcards.
        ("9.9.9", "*", True),
        ("1.7.0", "1.*", True),
        ("2.0.0", "1.*", False),
        ("1.2.7", "1.2.x", True),
        ("1.3.0", "1.2.*", False),
        # A pre-release needs a pre-release comparator on its own numbers.
        ("1.0.0-alpha", "1", False),
        ("1.0.0-beta", "^1.0.0-alpha", True),
        ("1.0.0-alpha", "^1.0.0-beta", False),
        ("1.0.0", "^1.0.0-alpha", True),
        ("1.0.1-alpha", "^1.0.0-alpha", False),
        ("1.0.0-alpha.2", ">=1.0.0-alpha.1", True),
        ("1.0.0-alpha.10", ">1.0.0-alpha.9", True),
        # Build metadata never counts.
        ("1.0.0+build", "1", True),
        ("1.2.3", "1.2.3+meta", True),
        # Anything unreadable satisfies nothing.
        ("1.0.0", "latest", False),
        ("1.0", "1", False),
        ("1.0.0", "", False),
        ("1.0.0", "1.2.3.4", False),
    ],
)
def test_satisfies(version: str, requirement: str, expected: bool) -> None:
    assert satisfies(version, requirement) is expected
