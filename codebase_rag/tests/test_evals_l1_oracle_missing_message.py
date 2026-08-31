"""A missing toolchain must name the binary, not print `{binary}`.

`run_l1_eval` logged `oracle_missing` unformatted while the surviving
constants carry a `{binary}` field, so a user without the toolchain read
"Node toolchain '{binary}' not found on PATH" verbatim (issue #1518). The
retrieval path formatted the same constants correctly, which is why the
defect survived: whoever tested that path saw the right message.

Driven through `run_l1_eval` rather than by formatting the constants here.
Asserting on a locally formatted copy would test this test's arithmetic and
pass whatever the production line does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from loguru import logger

from evals import constants as ec
from evals import logs as ls
from evals.l1_eval import run_l1_eval

# Every arm whose message carries a `{binary}` field, with the binary its
# own module passes. `java_l1.py` is included because it hands over the bare
# constant too, which the issue's list of call sites missed.
_ARMS = [
    ("TS_ORACLE_MISSING", ls.TS_ORACLE_MISSING, ec.NODE_BIN),
    ("LUA_ORACLE_MISSING", ls.LUA_ORACLE_MISSING, ec.NODE_BIN),
    ("PHP_ORACLE_MISSING", ls.PHP_ORACLE_MISSING, ec.NODE_BIN),
    ("JAVA_ORACLE_MISSING", ls.JAVA_ORACLE_MISSING, ec.JAVAC_BIN),
]


def _fail(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the eval must not proceed past an unavailable oracle")


@pytest.mark.parametrize(("name", "message", "binary"), _ARMS)
def test_a_missing_toolchain_names_the_binary(
    name: str, message: str, binary: str, tmp_path: Path
) -> None:
    seen: list[str] = []
    sink = logger.add(lambda record: seen.append(str(record)), level="ERROR")
    try:
        with pytest.raises(typer.Exit):
            run_l1_eval(
                tmp_path,
                "proj",
                tmp_path,
                available=lambda: False,
                oracle_missing=message,
                extract_cgr=_fail,
                run_oracle=_fail,
                oracle_binary=binary,
                scored_node_kinds=(),
                extracting_cgr="",
                cgr_done="",
                extracting_oracle="",
                oracle_done="",
                scores_filename="s.csv",
                diff_filename="d.json",
                title="t",
            )
    finally:
        logger.remove(sink)

    logged = "\n".join(seen)
    assert logged, f"{name}: nothing was logged at ERROR"
    assert "{binary}" not in logged, f"{name}: leaked a literal placeholder"
    assert binary in logged, f"{name}: did not name the binary {binary!r}"
