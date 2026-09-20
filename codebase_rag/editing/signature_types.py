"""Public data types and spec parsing for change_signature."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import NamedTuple

from .. import constants as cs
from .contract import Verdict

_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?::(?P<annotation>[^=@]+))?"
    r"(?:=(?P<default>[^@]+))?"
    r"(?:@(?P<source>.+))?$"
)


class ParamSpec(NamedTuple):
    """One parameter of the new signature and where its value comes from."""

    name: str
    from_index: int | None = None
    from_name: str | None = None
    literal: str | None = None
    annotation: str | None = None
    default: str | None = None

    @property
    def unmapped(self) -> bool:
        return (
            self.from_index is None and self.from_name is None and self.literal is None
        )


def parse_param_spec(text: str) -> ParamSpec:
    """`name[:annotation][=default][@source]` as the CLI and MCP spell it.

    `@2` maps from old positional index 2, `@old` from the old parameter
    `old`; a `=default` without a source is the literal every site that
    passes nothing gains (and the definition's default); a bare name with
    neither is unmapped.
    """
    match = _SPEC_RE.match(text.strip())
    if match is None:
        raise SignatureRefused(cs.SIGNATURE_BAD_SPEC.format(spec=text))
    name = match.group("name")
    annotation = (match.group("annotation") or "").strip() or None
    default = (match.group("default") or "").strip() or None
    source = (match.group("source") or "").strip() or None
    from_index = from_name = None
    if source is not None:
        if source.isdigit():
            from_index = int(source)
        else:
            from_name = source
    literal = default if source is None and default is not None else None
    return ParamSpec(name, from_index, from_name, literal, annotation, default)


class SignatureRefused(ValueError):
    """The change cannot be planned as asked."""


class UnmappedSite(NamedTuple):
    path: str
    line: int
    col: int
    owner: str
    reason: str


class RewrittenSite(NamedTuple):
    path: str
    line: int
    col: int
    owner: str
    resolution: str | None
    before: str
    after: str


class SignatureReport(NamedTuple):
    qualified_name: str
    hierarchy: tuple[str, ...]
    old_params: tuple[str, ...]
    new_params: tuple[str, ...]
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    sites: tuple[RewrittenSite, ...]
    unmapped: tuple[UnmappedSite, ...]
    diff: str
    message: str
    verdict: Verdict | None = None


# --- parameters of a definition -----------------------------------------------------


def sites_for(sites: Iterable[NamedTuple]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]
