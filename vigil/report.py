from datetime import date

from . import constants as vc
from .types_defs import Outcome

_NOTHING = "Nothing new reached code-graph-rag tonight."


def _subject(item_id: str) -> str:
    # pypi:protobuf:7.36.2 -> protobuf; an adopt id may carry any suffix.
    parts = item_id.split(vc.ITEM_ID_SEPARATOR)
    return parts[1] if len(parts) > 2 else item_id


def render(night: str, outcomes: list[Outcome], base: str, considered: int) -> str:
    """The morning standing: what changed your position, what is ready to
    merge, what was tried and dropped, and what is still pending.

    Everything in it comes from recorded outcomes; nothing is summarised
    that was not measured.
    """
    day = date.fromisoformat(night).strftime("%a %d %b %Y")
    minutes = sum(o.minutes for o in outcomes)
    lines = [
        f"## vigil · code-graph-rag · standing for {day}",
        "",
        f"{considered} items considered · {len(outcomes)} experiments · "
        f"{minutes:.0f} min · base `{base}`",
    ]
    if not outcomes:
        return "\n".join([*lines, "", _NOTHING, ""])
    for verdict in vc.VERDICT_ORDER:
        group = [o for o in outcomes if o.verdict == verdict]
        if not group:
            continue
        lines += ["", f"### {verdict} ({len(group)})", ""]
        for o in sorted(group, key=lambda o: o.item_id):
            branch = f" · branch `{o.branch}`" if o.branch else ""
            lines.append(f"- **{_subject(o.item_id)}** {o.headline}{branch}")
            lines += [f"  - {e}" for e in o.evidence]
    return "\n".join([*lines, ""])
