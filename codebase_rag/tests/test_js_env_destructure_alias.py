# Issue #2753: a JS/TS env read through destructuring (`const { PORT } =
# process.env`), a renamed or defaulted property, a parameter defaulting to
# `process.env`, or an alias (`const env = process.env; env.PORT`) is an ENV
# read, and the bound local carries it. Only the literal `process.env.X` and
# `process.env["X"]` spellings registered, so these produced no READS_FROM and
# no FLOWS_TO.
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
STDOUT = "resource::STDOUT::<dynamic>"
DYNAMIC = "resource::ENV::<dynamic>"

ENV_JS = """const fs = require("fs");

function dotted() {
  console.log(process.env.JE_DOTTED);
}

function bracket() {
  console.log(process.env["JE_BRACKET"]);
}

function destructured() {
  const { JE_DESTR } = process.env;
  console.log(JE_DESTR);
}

function destructuredRenamed() {
  const { JE_RENAMED: key } = process.env;
  console.log(key);
}

function destructuredDefault() {
  const { JE_DEFAULT = "dev" } = process.env;
  console.log(JE_DEFAULT);
}

function renamedDefault() {
  const { JE_RDEF: rd = "x" } = process.env;
  console.log(rd);
}

function quotedKey() {
  const { "JE_QUOTED": q } = process.env;
  console.log(q);
}

function assigned() {
  let v;
  ({ JE_ASSIGN: v } = process.env);
  console.log(v);
}

function restPattern() {
  const { JE_FIRST, ...others } = process.env;
  console.log(others);
}

function partlyUsed() {
  const { JE_UNUSED, JE_USED } = process.env;
  console.log(JE_USED);
}

function aliased() {
  const env = process.env;
  console.log(env.JE_ALIAS);
}

function aliasedBracket() {
  const env = process.env;
  console.log(env["JE_ALIAS_BR"]);
}

function paramDestructure({ JE_PARAM } = process.env) {
  console.log(JE_PARAM);
}

const moduleEnv = process.env;

function moduleAlias() {
  console.log(moduleEnv.JE_MODALIAS);
}

function notEnv(config) {
  const { JE_NOT } = config;
  console.log(JE_NOT);
}

function shadowedProcess(process) {
  const { JE_SHADOW } = process.env;
  console.log(JE_SHADOW);
}

function reboundAlias() {
  let env = process.env;
  env = { JE_REBOUND: "x" };
  console.log(env.JE_REBOUND);
}

function paramNamedEnv(env) {
  console.log(env.JE_PARAMENV);
}

function shadowsModuleAlias(moduleEnv) {
  console.log(moduleEnv.JE_SHADOWED_ALIAS);
}

// Bot review on PR #2767: an alias resolves by lexical scope at each access.
function enclosingAlias() {
  const env = process.env;
  function inner() {
    console.log(env.JE_ENCLOSING);
  }
  inner();
}

function blockShadowsModuleAlias() {
  {
    const moduleEnv = {};
    console.log(moduleEnv.JE_BLOCK_INNER);
  }
  console.log(moduleEnv.JE_BLOCK_OUTER);
}

function blockShadowsOwnAlias() {
  const env = process.env;
  if (env) {
    const env = {};
    console.log(env.JE_OWN_INNER);
  }
  console.log(env.JE_OWN_OUTER);
}

function caught() {
  try {
    run();
  } catch (moduleEnv) {
    console.log(moduleEnv.JE_CAUGHT);
  }
}

function destructureOverHandle() {
  let stream = fs.createWriteStream("/tmp/je_stream.txt");
  ({ JE_STREAM: stream } = process.env);
  stream.write(process.env.JE_SECRET);
}

module.exports = { dotted };
"""

ENV_TS = """function tsDestructured(): void {
  const { JE_TS_DESTR }: Record<string, string | undefined> = process.env;
  console.log(JE_TS_DESTR);
}

function tsParam({ JE_TS_PARAM }: NodeJS.ProcessEnv = process.env): void {
  console.log(JE_TS_PARAM);
}

function tsAliased(): void {
  const env: NodeJS.ProcessEnv = process.env;
  console.log(env.JE_TS_ALIAS);
}

export { tsDestructured };
"""


@pytest.fixture(scope="module")
def edges(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str, str]]:
    root = tmp_path_factory.mktemp("jsenv")
    (root / "env.js").write_text(ENV_JS, encoding="utf-8")
    (root / "env.ts").write_text(ENV_TS, encoding="utf-8")
    parsers, queries = load_parsers()
    missing = sorted(
        str(lang.value)
        for lang in (cs.SupportedLanguage.JS, cs.SupportedLanguage.TS)
        if lang not in parsers
    )
    if missing:
        # A module-scoped fixture runs before the per-test grammar skip
        # hook is installed, so a base install must skip here.
        pytest.skip(f"{', '.join(missing)} parser not available")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (str(c.args[0][2]), str(c.args[1]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _reads(edges: set[tuple[str, str, str]], function: str) -> set[str]:
    return {
        target
        for source, rel, target in edges
        if rel == cs.RelationshipType.READS_FROM.value
        and source.endswith(f".{function}")
    }


def _flows(edges: set[tuple[str, str, str]], key: str) -> bool:
    return (
        f"resource::ENV::{key}",
        cs.RelationshipType.FLOWS_TO.value,
        STDOUT,
    ) in edges


CASES = [
    ("destructured", "JE_DESTR"),
    ("destructuredRenamed", "JE_RENAMED"),
    ("destructuredDefault", "JE_DEFAULT"),
    ("renamedDefault", "JE_RDEF"),
    ("quotedKey", "JE_QUOTED"),
    ("assigned", "JE_ASSIGN"),
    ("partlyUsed", "JE_USED"),
    ("aliased", "JE_ALIAS"),
    ("aliasedBracket", "JE_ALIAS_BR"),
    ("paramDestructure", "JE_PARAM"),
    ("moduleAlias", "JE_MODALIAS"),
    ("tsDestructured", "JE_TS_DESTR"),
    ("tsParam", "JE_TS_PARAM"),
    ("tsAliased", "JE_TS_ALIAS"),
    ("enclosingAlias.inner", "JE_ENCLOSING"),
    ("blockShadowsModuleAlias", "JE_BLOCK_OUTER"),
    ("blockShadowsOwnAlias", "JE_OWN_OUTER"),
]


@pytest.mark.parametrize(("function", "key"), CASES)
def test_the_env_key_is_read(
    edges: set[tuple[str, str, str]], function: str, key: str
) -> None:
    assert f"resource::ENV::{key}" in _reads(edges, function)


@pytest.mark.parametrize(("function", "key"), CASES)
def test_the_bound_value_flows_to_stdout(
    edges: set[tuple[str, str, str]], function: str, key: str
) -> None:
    assert _flows(edges, key)


def test_a_destructured_key_that_is_not_printed_is_still_read(
    edges: set[tuple[str, str, str]],
) -> None:
    assert "resource::ENV::JE_UNUSED" in _reads(edges, "partlyUsed")


def test_a_rest_element_reads_the_whole_mapping(
    edges: set[tuple[str, str, str]],
) -> None:
    assert {"resource::ENV::JE_FIRST", DYNAMIC} <= _reads(edges, "restPattern")
    assert (DYNAMIC, cs.RelationshipType.FLOWS_TO.value, STDOUT) in edges


# Negative: what must not change.


@pytest.mark.parametrize(
    ("function", "key"), [("dotted", "JE_DOTTED"), ("bracket", "JE_BRACKET")]
)
def test_member_reads_still_read_and_flow(
    edges: set[tuple[str, str, str]], function: str, key: str
) -> None:
    assert f"resource::ENV::{key}" in _reads(edges, function)
    assert _flows(edges, key)


@pytest.mark.parametrize(
    "key",
    [
        "JE_NOT",
        "JE_SHADOW",
        "JE_REBOUND",
        "JE_PARAMENV",
        "JE_SHADOWED_ALIAS",
        "JE_BLOCK_INNER",
        "JE_OWN_INNER",
        "JE_CAUGHT",
    ],
)
def test_a_value_that_is_not_the_env_mapping_reads_nothing(
    edges: set[tuple[str, str, str]], key: str
) -> None:
    assert not any(target == f"resource::ENV::{key}" for _s, _r, target in edges)


def test_a_destructured_key_that_is_not_printed_does_not_flow(
    edges: set[tuple[str, str, str]],
) -> None:
    assert not _flows(edges, "JE_UNUSED")


def test_a_destructured_rebinding_drops_the_handle_the_name_held(
    edges: set[tuple[str, str, str]],
) -> None:
    # `({ JE_STREAM: stream } = process.env)` replaces the file stream, so the
    # later `stream.write(..)` writes no file (bot review on PR #2767).
    assert (
        "resource::ENV::JE_SECRET",
        cs.RelationshipType.FLOWS_TO.value,
        "resource::FILE::/tmp/je_stream.txt",
    ) not in edges
