"""The fuzz harnesses must keep working when the code they drive moves.

A harness only runs in CI under ClusterFuzzLite, and atheris does not build on
macOS, so a refactor that renames `sorted_captures` or changes `reingest`'s
signature would break every harness while the whole local suite stayed green.
The breakage would surface days later as a ClusterFuzzLite build failure, which
reads like an infrastructure problem rather than the API change it is.

These tests drive the harness bodies directly with a stub provider, so they run
everywhere pytest does and fail immediately on that kind of drift. They are not
fuzzing: each asserts that the harness executes its target and that its
assertions still discriminate.
"""

from __future__ import annotations

import importlib.util
import os
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

FUZZ_DIR = Path(__file__).resolve().parents[2] / "fuzz"


class _StubProvider:
    """A faithful port of the atheris FuzzedDataProvider methods used here.

    Faithful, not approximate, and that distinction is the point: an earlier
    version seeded `random.Random` with the input bytes and returned random
    values, so it agreed with any corpus encoding at all. Three separately
    broken corpora passed under it -- the seeds decoded to the wrong language,
    the wrong command and the wrong edit plan, and nothing noticed.

    Transcribed from atheris `src/native/fuzzed_data_provider.cc`:

    * `ConsumeSmallIntInRange` walks the buffer from the BACK
      (`--remaining_bytes_; result = (result << 8) | data_ptr_[remaining_bytes_]`)
      and reduces modulo the range size.
    * `ConsumeUnicodeNoSurrogates` consumes one leading `string_spec` byte and
      discards it, returning ASCII only when `spec & 1`.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._front = 0
        self._remaining = len(data)

    def _advance(self, count: int) -> None:
        count = min(count, self._remaining)
        self._front += count
        self._remaining -= count

    def remaining_bytes(self) -> int:
        return self._remaining

    def ConsumeIntInRange(self, low: int, high: int) -> int:  # noqa: N802
        if low == high:
            return low
        span = high - low
        bits = span.bit_length()
        result = 0
        offset = 0
        while offset < bits and (span >> offset) > 0 and self._remaining != 0:
            self._remaining -= 1
            result = (result << 8) | self._data[self._front + self._remaining]
            offset += 8
        return low + (result % (span + 1))

    def ConsumeBool(self) -> bool:  # noqa: N802
        return bool(self.ConsumeIntInRange(0, 1))

    def ConsumeBytes(self, count: int) -> bytes:  # noqa: N802
        count = min(count, self._remaining)
        out = self._data[self._front : self._front + count]
        self._advance(count)
        return out

    def ConsumeUnicodeNoSurrogates(self, count: int) -> str:  # noqa: N802
        if count == 0 or self._remaining == 0:
            return ""
        if self._remaining == 1:
            self._advance(1)
            return ""
        spec = self._data[self._front]
        self._advance(1)

        if spec & 1:
            take = min(count, self._remaining)
            buf = bytes(
                byte & 0x7F for byte in self._data[self._front : self._front + take]
            )
            self._advance(take)
            return buf.decode("ascii")

        if spec & 2:
            take = min(count * 2, self._remaining)
            even = take & ~1
            values = struct.unpack(
                f"<{even // 2}H", self._data[self._front : self._front + even]
            )
            self._advance(take)
            return "".join(
                chr(v - 0xD800 if 0xD800 <= v < 0xE000 else v) for v in values
            )

        take = min(count * 4, self._remaining)
        groups = take & ~3
        values = struct.unpack(
            f"<{groups // 4}I", self._data[self._front : self._front + groups]
        )
        self._advance(take)
        out = []
        for value in values:
            value &= 0x1FFFFF
            if value & 0x100000:
                value &= ~0x0F0000
            if 0xD800 <= value < 0xE000:
                value -= 0xD800
            out.append(chr(value))
        return "".join(out)


def _stub_atheris() -> ModuleType:
    """A module object exposing just the atheris API the harnesses import."""
    import contextlib

    module = ModuleType("atheris")

    @contextlib.contextmanager
    def instrument_imports() -> Any:
        yield

    module.instrument_imports = instrument_imports  # type: ignore[attr-defined]
    module.instrument_func = lambda func: func  # type: ignore[attr-defined]
    module.FuzzedDataProvider = _StubProvider  # type: ignore[attr-defined]
    module.Setup = lambda *a, **k: None  # type: ignore[attr-defined]
    module.Fuzz = lambda *a, **k: None  # type: ignore[attr-defined]
    return module


def import_harness_module(name: str) -> ModuleType:
    """Return the harness module `name`, imported with atheris stubbed out.

    The harnesses import atheris at module scope, which is unavailable on
    macOS, so a stub stands in for the duration of the import. Both that stub
    and any environment variable the harness sets at import time are undone
    on the way out: `fuzz_shell_command` setdefaults
    PYDANTIC_DISABLE_PLUGINS, and leaving it set would silently change how
    every later test in the session constructs pydantic models.
    """
    path = FUZZ_DIR / f"{name}.py"
    if not path.exists():
        pytest.fail(f"harness {name} is missing from {FUZZ_DIR}")

    original = sys.modules.get("atheris")
    original_env = {key: os.environ.get(key) for key in ("PYDANTIC_DISABLE_PLUGINS",)}
    sys.modules["atheris"] = _stub_atheris()
    try:
        spec = importlib.util.spec_from_file_location(f"_fuzz_{name}", path)
        assert spec is not None, f"no import spec for {path}"
        assert spec.loader is not None, f"import spec for {path} has no loader"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if original is None:
            sys.modules.pop("atheris", None)
        else:
            sys.modules["atheris"] = original
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def parse_harness() -> ModuleType:
    return import_harness_module("fuzz_parse_source")


@pytest.fixture(scope="module")
def shell_harness() -> ModuleType:
    return import_harness_module("fuzz_shell_command")


# Seeds that are reproducers for an OPEN defect: the harness is expected to
# detect them, so "runs without raising" is the wrong assertion for these.
# Delete an entry when its issue is fixed -- the seed then has to pass like
# any other, and the harness re-detects a regression.
_KNOWN_DEFECT_SEEDS = {
    # #1810: a bad byte inside an identifier truncates it silently.
    "edge_truncated_identifier.bin",
    "edge_truncated_identifier_tail.bin",
}


def test_parse_harness_runs_every_seed(parse_harness: ModuleType) -> None:
    """Every seed in the corpus drives the harness without raising.

    Except the reproducers for open defects, which the harness is supposed to
    catch. Those are asserted the other way round in
    `test_known_defect_seeds_are_still_detected`, so an entry here cannot
    quietly become a seed that passes for the wrong reason.
    """
    seeds = sorted((FUZZ_DIR / "corpus" / "fuzz_parse_source").iterdir())
    assert seeds, "the parse corpus is empty; run fuzz/build_corpus.py"
    for seed in seeds:
        parse_harness.fuzz_parse_source(seed.read_bytes())


def test_known_defect_seeds_are_still_detected(parse_harness: ModuleType) -> None:
    """The other half of the exemption above.

    An exemption list that is only ever skipped is indistinguishable from a
    list of seeds that stopped reproducing: both leave the suite green. Each
    exempt seed must still trip the detector, so the day one is fixed this
    test fails and says to remove it from the set rather than letting the
    exemption outlive the defect.
    """
    for name in sorted(_KNOWN_DEFECT_SEEDS):
        seed = FUZZ_DIR / "corpus" / "fuzz_parse_source" / name
        assert seed.exists(), f"missing seed {name}; run fuzz/build_corpus.py"
        payload = seed.read_bytes()

        parse_harness._TRUNCATED_NAMES.clear()
        parse_harness.fuzz_parse_source(payload)

        assert parse_harness._TRUNCATED_NAMES, (
            f"{name} no longer trips the truncation oracle; if #1810's "
            "extractors were fixed, drop it from _KNOWN_DEFECT_SEEDS"
        )


def test_the_oracle_records_a_replacement_character_name(
    parse_harness: ModuleType,
) -> None:
    """The fuzz oracle must see the second damage shape too.

    Production reports a name carrying U+FFFD (Lua keeps the bad byte in an
    ERROR node between the two identifiers of a dotted name, so the whole
    expression decodes to `Greeter.gr\ufffdeet` rather than truncating). The
    oracle needs the same rule or that production branch has no regression
    check -- the adjacency test cannot see it, because nothing is adjacent to
    a shortened span.
    """
    from codebase_rag import constants as cs

    language = cs.SupportedLanguage.LUA
    if language not in parse_harness._PARSERS:
        pytest.skip("no lua grammar available")

    clean = b"function Greeter.greet(n) return n end\n"
    dirty = b"function Greeter.gr\xffeet(n) return n end\n"

    parse_harness._TRUNCATED_NAMES.clear()
    tree = parse_harness._PARSERS[language].parse(clean)
    parse_harness._extract_names(language, tree.root_node, clean)
    assert not parse_harness._TRUNCATED_NAMES, "false alarm on clean Lua"

    parse_harness._TRUNCATED_NAMES.clear()
    tree = parse_harness._PARSERS[language].parse(dirty)
    parse_harness._extract_names(language, tree.root_node, dirty)
    assert parse_harness._TRUNCATED_NAMES, (
        "the oracle did not record a name carrying the replacement character"
    )


def test_the_truncation_record_is_bounded(parse_harness: ModuleType) -> None:
    """The oracle records instead of raising, so it must not grow without end.

    libFuzzer runs millions of iterations and each can contribute a DISTINCT
    mangled name, so an unbounded collection would exhaust memory and kill the
    run -- the same "the harness crashed itself" failure that recording rather
    than raising exists to avoid.

    Each input carries a different identifier: feeding the same one repeatedly
    would dedup to a single entry and pass whether or not the cap exists,
    which is what the first version of this test did.
    """
    from codebase_rag import constants as cs

    parse_harness._TRUNCATED_NAMES.clear()
    limit = parse_harness._TRUNCATED_NAME_LIMIT
    parser = parse_harness._PARSERS[cs.SupportedLanguage.PYTHON]

    for index in range(limit + 50):
        source = f"def na{index}me():\n    return 1\n".encode()
        source = source.replace(b"na", b"n\xffa", 1)
        tree = parser.parse(source)
        parse_harness._extract_names(
            cs.SupportedLanguage.PYTHON, tree.root_node, source
        )

    assert parse_harness._TRUNCATED_NAMES, "the oracle stopped recording"
    assert len(parse_harness._TRUNCATED_NAMES) <= limit, (
        f"the record grew to {len(parse_harness._TRUNCATED_NAMES)}, past its "
        f"cap of {limit}; a long fuzz run would exhaust memory"
    )


def test_parse_harness_reaches_the_extractor(parse_harness: ModuleType) -> None:
    """A planted fault in `get_name` must surface.

    Without this, `test_parse_harness_runs_every_seed` passing would be
    equally consistent with a harness that silently extracts nothing.
    """
    from codebase_rag import constants as cs
    from codebase_rag.language_spec import LANGUAGE_FQN_SPECS

    original = LANGUAGE_FQN_SPECS[cs.SupportedLanguage.PYTHON]
    sentinel = "planted fault reached the extractor"

    def exploding(node: Any) -> str | None:
        if node.type == "function_definition":
            raise IndexError(sentinel)
        return original.get_name(node)

    LANGUAGE_FQN_SPECS[cs.SupportedLanguage.PYTHON] = original._replace(
        get_name=exploding
    )
    try:
        # The harness picks the language from the provider, so drive
        # `_extract_names` directly rather than guessing an input that
        # selects Python.
        parser = parse_harness._PARSERS[cs.SupportedLanguage.PYTHON]
        source = b"def f():\n    return 1\n"
        tree = parser.parse(source)
        with pytest.raises(IndexError, match=sentinel):
            parse_harness._extract_names(
                cs.SupportedLanguage.PYTHON, tree.root_node, source
            )
    finally:
        LANGUAGE_FQN_SPECS[cs.SupportedLanguage.PYTHON] = original


@pytest.mark.parametrize(
    ("language_name", "source"),
    [
        ("PYTHON", b"def al\xffpha():\n    return 1\n"),
        ("LUA", b"function al\xffpha() return 1 end\n"),
        ("JS", b"function al\xffpha() { return 1; }\n"),
    ],
    ids=["python", "lua", "javascript"],
)
def test_parse_harness_detects_a_truncated_name(
    parse_harness: ModuleType, language_name: str, source: bytes
) -> None:
    """Issue #1810: a bad byte inside an identifier truncates it silently.

    tree-sitter treats the byte as a token boundary, so the `name` node covers
    only what follows it and the extractor decodes that shortened node without
    error. `alpha` is indexed as `pha` with nothing raised and nothing logged,
    which is why catching exceptions cannot find this class.
    """
    from codebase_rag import constants as cs

    language = getattr(cs.SupportedLanguage, language_name)
    if language not in parse_harness._PARSERS:
        pytest.skip(f"no {language_name} grammar available")
    tree = parse_harness._PARSERS[language].parse(source)

    parse_harness._TRUNCATED_NAMES.clear()
    parse_harness._extract_names(language, tree.root_node, source)

    assert parse_harness._TRUNCATED_NAMES, (
        f"{language_name}: the oracle did not record a truncated name"
    )


@pytest.mark.parametrize(
    "identifier",
    ["alpha", "caf\u00e9", "\u00e9lan", "\u51fd\u6570"],
    ids=["ascii", "latin_accent", "leading_accent", "cjk"],
)
def test_parse_harness_is_quiet_on_valid_identifiers(
    parse_harness: ModuleType, identifier: str
) -> None:
    """The control that decides whether the detector is usable.

    A naive "is this name pure ASCII" check passes every test anyone would
    think to write and then fires on every non-English codebase. These inputs
    separate "extracted from undecodable bytes" from "merely not ASCII", which
    is why the detector asks the SOURCE to round-trip rather than inspecting
    the extracted name.
    """
    from codebase_rag import constants as cs

    language = cs.SupportedLanguage.PYTHON
    if language not in parse_harness._PARSERS:
        pytest.skip("no python grammar available")
    source = f"def {identifier}():\n    return 1\n".encode()
    tree = parse_harness._PARSERS[language].parse(source)

    parse_harness._extract_names(language, tree.root_node, source)


def test_the_containment_oracle_would_miss_this_defect() -> None:
    """Why the detector is not `name.encode() in raw`, pinned as a test.

    The obvious check is that the extracted name appears in the bytes it came
    from. It is satisfied BY the corruption whenever the corruption truncates:
    `pha` really is a substring of `al\\xffpha`, so the oracle returns True on
    the exact defect it was written for and can never fire. Written down here
    because it is the first thing a future reader will try to "simplify" the
    detector into.
    """
    raw = b"al\xffpha"
    truncated = b"pha"

    assert truncated in raw, "the containment oracle passes on the defect"

    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")


# Sources that make `parse_with_preproc_recovery` take its retry: each parses
# with an error, and the retry blanks lines to recover. A harness that calls
# `parser.parse` directly never reaches that code, though production does for
# every C, C++ and C# file.
_RECOVERY_SOURCES = {
    "c_sharp": (
        b"public interface ILogger {\n    void M()\n#if NET\n"
        b"        => Impl()\n#endif\n    ;\n    void N() { }\n}\n",
        "_blank_csharp_directives",
    ),
    "cpp": (b"class Foo {\n public:\n  MY_EXPORT\n  void f();\n};\n", "_blank"),
    "c": (b"API_EXPORT\nint f(void) { return 0; }\n", "_blank"),
}


def _selecting(harness: ModuleType, language: str, source: bytes) -> bytes:
    """The fuzz input that makes the parse harness pick `language`."""
    names = [str(each) for each in harness._LANGUAGES]
    if language not in names:
        pytest.skip(f"no {language} grammar available")
    return source + bytes([names.index(language)])


@pytest.mark.parametrize("language", sorted(_RECOVERY_SOURCES))
def test_parse_harness_parses_through_preproc_recovery(
    parse_harness: ModuleType, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    from codebase_rag.parsers.cpp import preproc_recovery

    source, retry = _RECOVERY_SOURCES[language]
    real = getattr(preproc_recovery, retry)
    calls: list[bytes] = []

    def spy(*args: Any) -> Any:
        calls.append(args[0] if isinstance(args[0], bytes) else b"")
        return real(*args)

    monkeypatch.setattr(preproc_recovery, retry, spy)
    parse_harness.fuzz_parse_source(_selecting(parse_harness, language, source))
    assert calls, f"the {language} retry was never reached"


def test_the_oracle_reads_the_bytes_a_recovered_tree_was_parsed_from(
    parse_harness: ModuleType,
) -> None:
    """A C# retry drops the directive lines, so its offsets index other bytes.

    Probing the ORIGINAL source at those offsets lands mid-character in the
    comment above `N`, which reads as a bad byte beside a valid name.
    """
    source = (
        "class K {\n    void M()\n#if X\n        => Impl()\n#endif\n    ;\n"
        "    // " + "\u00e9" * 12 + "\nvoid N() { }\n}\n"
    ).encode()
    parse_harness._TRUNCATED_NAMES.clear()
    parse_harness.fuzz_parse_source(_selecting(parse_harness, "c_sharp", source))
    assert not parse_harness._TRUNCATED_NAMES


@pytest.mark.parametrize("lead", [b"\n", b"\n\n\n", b"    \n\t"])
def test_the_oracle_lines_up_with_a_tree_that_starts_after_whitespace(
    parse_harness: ModuleType, lead: bytes
) -> None:
    """The root starts at the first token, but node offsets count from byte 0."""
    parse_harness._TRUNCATED_NAMES.clear()
    parse_harness.fuzz_parse_source(
        _selecting(parse_harness, "python", lead + b"def al\xffpha():\n    pass\n")
    )
    assert parse_harness._TRUNCATED_NAMES, "missed a truncation after whitespace"

    parse_harness._TRUNCATED_NAMES.clear()
    parse_harness.fuzz_parse_source(
        _selecting(
            parse_harness, "python", lead + "def caf\u00e9():\n    pass\n".encode()
        )
    )
    assert not parse_harness._TRUNCATED_NAMES, "false alarm on a valid name"


def test_parse_harness_runs_every_query_slot(
    parse_harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every compiled query production loads, not a hand-picked four."""
    from codebase_rag.types_defs import LanguageQueries

    slots = [
        key
        for key, hint in LanguageQueries.__annotations__.items()
        if "Query" in str(hint)
    ]
    real = parse_harness.QueryCursor
    seen: set[int] = set()

    def recording(query: Any) -> Any:
        seen.add(id(query))
        return real(query)

    monkeypatch.setattr(parse_harness, "QueryCursor", recording)
    missed: list[str] = []
    for language in parse_harness._LANGUAGES:
        seen.clear()
        parse_harness.fuzz_parse_source(
            _selecting(parse_harness, str(language), b"x\n")
        )
        queries = parse_harness._QUERIES[language]
        missed += [
            f"{language}.{slot}"
            for slot in slots
            if queries.get(slot) is not None and id(queries[slot]) not in seen
        ]
    assert not missed, f"queries never executed: {missed}"


def test_parse_edge_seeds_reach_the_grammar_they_are_written_for(
    parse_harness: ModuleType, corpus_builder: ModuleType
) -> None:
    """An edge seed is about its source, which is written for one grammar.

    The Python snippets used to carry selector 0, handing `def f(` to the
    first grammar alphabetically, where it is not a truncated definition.
    """
    languages = [str(language) for language in parse_harness._LANGUAGES]
    corpus = FUZZ_DIR / "corpus" / "fuzz_parse_source"
    targeted = corpus_builder.RECOVERY_EDGE_CASES | corpus_builder.PREDICATE_EDGE_CASES
    wrong: list[str] = []
    for seed in sorted(corpus.glob("edge_*.bin")):
        name = seed.stem.removeprefix("edge_")
        expected = targeted.get(name, ("python", b""))[0]
        if expected not in languages:
            pytest.skip(f"no {expected} grammar available")
        provider = _StubProvider(seed.read_bytes())
        got = languages[provider.ConsumeIntInRange(0, len(languages) - 1)]
        if got != expected:
            wrong.append(f"{seed.name}: {got}, not {expected}")
    assert not wrong, wrong
    assert corpus_builder.RECOVERY_EDGE_CASES, "no seed reaches the retries"
    assert corpus_builder.PREDICATE_EDGE_CASES, "no seed reaches a #match?"
    for name in targeted:
        assert (corpus / f"edge_{name}.bin").exists(), name


def _undecodable_predicate_texts(
    parse_harness: ModuleType, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> tuple[int, list[bytes]]:
    """How often a rewritten `#match?` ran, and its capture texts that are not UTF-8."""
    from codebase_rag import constants as cs
    from codebase_rag import query_predicates

    real = query_predicates.query_predicate
    calls = 0
    seen: list[bytes] = []

    def spying(
        predicate: str,
        args: list[tuple[str, str]],
        pattern_index: int,
        captures: dict[str, list[Any]],
    ) -> bool:
        nonlocal calls
        if predicate.startswith(cs.QUERY_PREDICATE_REWRITE_PREFIX):
            calls += 1
            for value, kind in args:
                if kind != cs.QUERY_PREDICATE_ARG_CAPTURE:
                    continue
                for node in captures.get(value, []):
                    text = node.text or b""
                    try:
                        text.decode()
                    except UnicodeDecodeError:
                        seen.append(text)
        return real(predicate, args, pattern_index, captures)

    monkeypatch.setattr(query_predicates, "query_predicate", spying)
    parse_harness.fuzz_parse_source(payload)
    return calls, seen


def test_every_predicate_seed_puts_undecodable_text_under_a_match_predicate(
    parse_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seed is the reproducer for the `#match?` decode crash.

    A bad byte that never reaches a predicate's capture is just another bad
    byte, and would stay green with the predicates evaluated natively.
    """
    assert corpus_builder.PREDICATE_EDGE_CASES, "no predicate seed"
    corpus = FUZZ_DIR / "corpus" / "fuzz_parse_source"
    for name, (language, _) in corpus_builder.PREDICATE_EDGE_CASES.items():
        if language not in parse_harness._PARSERS:
            pytest.skip(f"no {language} grammar available")
        payload = (corpus / f"edge_{name}.bin").read_bytes()
        _, seen = _undecodable_predicate_texts(parse_harness, monkeypatch, payload)
        assert seen, name


def test_a_predicate_seed_made_decodable_puts_no_bad_text_under_a_predicate(
    parse_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the same sources with only their bad bytes replaced.

    Without it a spy that flags every capture passes the test above; and the
    call count shows the predicates still ran, so the empty result is not
    just a source that never reaches one.
    """
    for name, (language, raw) in corpus_builder.PREDICATE_EDGE_CASES.items():
        if language not in parse_harness._PARSERS:
            pytest.skip(f"no {language} grammar available")
        payload = raw.decode(errors="replace").encode() + bytes(
            [[str(each) for each in parse_harness._LANGUAGES].index(language)]
        )
        calls, seen = _undecodable_predicate_texts(parse_harness, monkeypatch, payload)
        assert calls, f"{name}: no #match? ran, so the clean result says nothing"
        assert not seen, name


def _retry_rewrote(parse_harness: ModuleType, language: str, raw: bytes) -> bool:
    from codebase_rag.parsers.cpp.preproc_recovery import (
        parse_with_preproc_recovery,
    )

    parser = parse_harness._PARSERS[language]
    plain = parser.parse(raw).root_node.text
    return parse_with_preproc_recovery(parser, raw, language).root_node.text != plain


def test_every_recovery_seed_sends_the_parse_down_a_retry(
    parse_harness: ModuleType, corpus_builder: ModuleType
) -> None:
    """A recovery seed that parses cleanly first time exercises no retry."""
    for name, (language, raw) in corpus_builder.RECOVERY_EDGE_CASES.items():
        if language not in parse_harness._PARSERS:
            pytest.skip(f"no {language} grammar available")
        assert _retry_rewrote(parse_harness, language, raw), name


def test_a_source_needing_no_retry_is_not_counted_as_one(
    parse_harness: ModuleType,
) -> None:
    """Control: without this, a `_retry_rewrote` that always says yes passes."""
    if "c" not in parse_harness._PARSERS:
        pytest.skip("no c grammar available")
    assert not _retry_rewrote(parse_harness, "c", b"int f(void) { return 0; }\n")


@pytest.fixture(scope="module")
def corpus_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_fuzz_build_corpus", FUZZ_DIR / "build_corpus.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shell_harness_runs_every_seed(shell_harness: ModuleType) -> None:
    seeds = sorted((FUZZ_DIR / "corpus" / "fuzz_shell_command").iterdir())
    assert seeds, "the shell corpus is empty; run fuzz/build_corpus.py"
    for seed in seeds:
        shell_harness.fuzz_shell_command(seed.read_bytes())


def _drive_shell(
    harness: ModuleType,
    builder: ModuleType,
    command: str,
    suffix_choice: int = 0,
    respell_seed: int = 0,
) -> None:
    """Run the harness body on the bytes the corpus encoder writes.

    Through `fuzz_shell_command` and the faithful provider port, not
    `check_command` directly, so a broken encoding fails here too.
    """
    harness.fuzz_shell_command(
        builder.encode_shell_seed(command, suffix_choice, respell_seed)
    )


def _plant_screen(
    monkeypatch: pytest.MonkeyPatch,
    harness: ModuleType,
    planted: Any,
) -> None:
    """Replace the production screen with `planted(original, self, command)`."""
    original = harness.ShellCommander._screen
    monkeypatch.setattr(
        harness.ShellCommander,
        "_screen",
        lambda self, command: planted(original, self, command),
    )


def _skip_without_links(harness: ModuleType, command: str) -> None:
    for name in ("linked_dir", "linked_file", "-linked", "inner_link", "loop"):
        if name in command and not (harness.SANDBOX_ROOT / name).is_symlink():
            pytest.skip(f"no symlink privilege to plant {name!r} on this host")


@pytest.mark.parametrize(
    ("exc", "message"),
    ((ValueError("planted"), "raised ValueError"), (RecursionError(), "recursion")),
)
@pytest.mark.parametrize(
    ("gate", "label"),
    (
        ("screen", "default screen"),
        ("_requires_approval", "approval gate"),
        ("_noninteractive_denial", "non-interactive gate"),
    ),
)
def test_shell_harness_catches_a_gate_that_raises(
    shell_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    message: str,
    gate: str,
    label: str,
) -> None:
    """Every gate is total, and the report names the one that raised."""
    if gate == "screen":

        def planted(original: Any, self: Any, command: str) -> Any:
            if "boom" in command:
                raise exc
            return original(self, command)

        _plant_screen(monkeypatch, shell_harness, planted)
    else:
        original = getattr(shell_harness, gate)

        def raising(command: str, *args: Any) -> Any:
            if "boom" in command:
                raise exc
            return original(command, *args)

        monkeypatch.setattr(shell_harness, gate, raising)
    with pytest.raises(AssertionError, match=f"{label} .*{message}"):
        _drive_shell(shell_harness, corpus_builder, "echo boom")


def _allow_double_ampersand(harness: ModuleType) -> Any:
    """A planted screen that waves any `&&` command through unvalidated."""

    def planted(original: Any, self: Any, command: str) -> Any:
        if "&&" in command:
            return None, harness._parse_command(command)
        return original(self, command)

    return planted


def test_shell_harness_catches_a_spawned_segment_refused_alone(
    shell_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The laundering property: an operator must not carry a segment through.

    The plant waves through this one command only, so the suffixed variant is
    refused honestly and only the check on the bare command can see it.
    """
    target = "echo hi && rm ../outside_project"

    def planted(original: Any, self: Any, command: str) -> Any:
        if command == target:
            return None, shell_harness._parse_command(command)
        return original(self, command)

    _plant_screen(monkeypatch, shell_harness, planted)
    with pytest.raises(AssertionError, match="refused on its own") as caught:
        _drive_shell(shell_harness, corpus_builder, target)
    assert not str(caught.value).startswith("suffix laundering")


def test_shell_harness_catches_a_laundered_suffix(
    shell_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fuzzer's prefix alone is fine; it is the appended suffix that leaks.

    Choice 0 is the first suffix behind `&&`, which the planted screen waves
    through, so only the suffix property can see it.
    """
    _plant_screen(monkeypatch, shell_harness, _allow_double_ampersand(shell_harness))
    assert shell_harness.OPERATORS[0] == "&&"
    with pytest.raises(AssertionError, match="suffix laundering"):
        _drive_shell(shell_harness, corpus_builder, "echo hi", suffix_choice=0)


@pytest.mark.parametrize("command", ("ls", "echo hi > out.txt"))
def test_shell_harness_catches_an_approval_free_path_read(
    shell_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """`ls` takes paths and `>` names a file; neither may skip approval."""
    monkeypatch.setattr(shell_harness, "_requires_approval", lambda _c: False)
    with pytest.raises(AssertionError, match="runs without approval"):
        _drive_shell(shell_harness, corpus_builder, command)


def test_shell_harness_catches_an_xargs_wrapping_that_launders(
    shell_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def planted(original: Any, self: Any, command: str) -> Any:
        if command.startswith("xargs "):
            return None, shell_harness._parse_command(command)
        return original(self, command)

    _plant_screen(monkeypatch, shell_harness, planted)
    with pytest.raises(AssertionError, match="allows the wrapping"):
        _drive_shell(shell_harness, corpus_builder, "rm ../outside_project")


def test_shell_harness_catches_a_verdict_that_depends_on_quoting(
    shell_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed 1 single-quotes every token: `'ls' 'x'`, the same argv as `ls x`."""

    def planted(original: Any, self: Any, command: str) -> Any:
        if "'" in command:
            return "planted refusal", []
        return original(self, command)

    _plant_screen(monkeypatch, shell_harness, planted)
    assert shell_harness._respell(["ls", "x"], 1) == "'ls' 'x'"
    with pytest.raises(AssertionError, match="flipped the default screen"):
        _drive_shell(shell_harness, corpus_builder, "ls x", respell_seed=1)


@pytest.mark.parametrize(
    ("command", "message"),
    (
        ("cat /etc/passwd", "escapes the project root"),
        ("cat linked_file", "escapes the project root"),
        ("rg -flinked_file x", "escapes the project root"),
        ("git status", "non-read-only program"),
    ),
)
def test_shell_harness_catches_a_non_interactive_escape(
    shell_harness: ModuleType,
    corpus_builder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    message: str,
) -> None:
    _skip_without_links(shell_harness, command)
    monkeypatch.setattr(shell_harness, "_noninteractive_denial", lambda *_a: None)
    with pytest.raises(AssertionError, match=message):
        _drive_shell(shell_harness, corpus_builder, command)


@pytest.mark.parametrize(
    "command",
    (
        "ls",
        "perl -e 1",
        "rm ../outside_project",
        "git status",
        "xargs ls",
        "echo 'a | b'",
        "uv run pytest -q",
        "find . -exec rm {} ;",
        "cat data.txt inner_link",
        "cat /etc/passwd",
        "cat linked_file",
        "rg -flinked_file x",
        "cat loop",
        "echo hi > out.txt",
    ),
)
def test_shell_harness_is_quiet_on_unplanted_input(
    shell_harness: ModuleType, corpus_builder: ModuleType, command: str
) -> None:
    """The control: with production intact, every input above passes.

    Each is a planted-fault input from the tests above, so those tests fire
    because of the fault and not because the harness raises on everything.
    Every suffix and operator, and a spread of respellings, are covered.
    """
    for choice in range(shell_harness.SUFFIX_CHOICES):
        _drive_shell(
            shell_harness,
            corpus_builder,
            command,
            choice,
            (choice * 7919) % (shell_harness.RESPELL_SEEDS + 1),
        )


def test_the_confinement_oracle_judges_meaning_not_spelling(
    shell_harness: ModuleType,
) -> None:
    contained = shell_harness._contained
    for escaping in ("/etc/passwd", "../outside/secret.txt", "sub/../../outside"):
        assert not contained(escaping), escaping
    for inside in ("data.txt", "sub/../data.txt", "not-yet-created", ".", "a\x00b"):
        assert contained(inside), inside
    if (shell_harness.SANDBOX_ROOT / "linked_file").is_symlink():
        assert not contained("linked_file")
        assert not contained("linked_dir/secret.txt")
        assert not contained("-linked")
        assert contained("inner_link")
    if os.name != "nt":
        # A backslash is an ordinary filename character on POSIX.
        assert contained("\\\\srs")


def test_the_hard_coded_program_sets_match_the_settings_defaults(
    shell_harness: ModuleType,
) -> None:
    """Widening what runs unasked must be a deliberate edit to the harness too."""
    from codebase_rag.config import AppConfig

    fields = AppConfig.model_fields
    read_only = fields["SHELL_READ_ONLY_COMMANDS"].default
    noninteractive = fields["SHELL_NONINTERACTIVE_READ_COMMANDS"].default
    assert shell_harness.APPROVAL_FREE_PROGRAMS == read_only
    assert shell_harness.NONINTERACTIVE_PROGRAMS == read_only | noninteractive
    # The approval property assumes no git subcommand skips approval.
    assert fields["SHELL_SAFE_GIT_SUBCOMMANDS"].default == frozenset()


def test_the_respelling_styles_all_preserve_the_argv(shell_harness: ModuleType) -> None:
    import shlex

    argv = ["rm", "-rf", "a b", "it's", 'q"x', "back\\slash", "", "-"]
    for style in range(shell_harness.RESPELL_STYLES):
        spelt = " ".join(shell_harness._respell_token(t, style) for t in argv)
        assert shlex.split(spelt) == argv, (style, spelt)


def test_build_script_makes_unpackaged_imports_resolvable() -> None:
    """`evals` is not in the wheel, so the build must put it on the path.

    `fuzz_incremental_update` imports `evals.cgr_graph`, but pyproject's
    package discovery includes only `codebase_rag*`, `codec*` and `cgr*`. In
    the ClusterFuzzLite image pyinstaller freezes each harness from the
    INSTALLED packages, so without the repo root on the path that import is
    unresolvable and the target builds into something that fails on first
    run -- which reads as a fuzzing crash rather than a build mistake.

    Asserted against the build script because that is where the fix lives and
    nothing else in the suite would notice it being dropped.
    """
    harness = (FUZZ_DIR / "fuzz_incremental_update.py").read_text()
    if "evals" not in harness:
        pytest.skip("the harness no longer imports evals")

    build = (FUZZ_DIR.parent / ".clusterfuzzlite" / "build.sh").read_text()
    # Ignore comments: the word PYTHONPATH appears in the rationale above the
    # line that does the work, so a substring test over the whole file passes
    # on the comment alone. Checked with the comment stripped, and verified by
    # deleting the export and watching this test go red.
    code = "\n".join(
        line for line in build.splitlines() if not line.lstrip().startswith("#")
    )
    assert "export PYTHONPATH=" in code, (
        "build.sh must export PYTHONPATH so pyinstaller can resolve `evals`, "
        "which is not part of the installed wheel"
    )
    assert "--paths" in code, (
        "compile_python_fuzzer must be given --paths so the frozen target can "
        "resolve `evals`"
    )


def test_packaged_wheel_really_excludes_evals() -> None:
    """The premise of the test above, checked rather than assumed.

    If `evals` were ever added to the wheel, the PYTHONPATH dance in build.sh
    would be dead weight and this test says so instead of quietly passing.
    """
    pyproject = (FUZZ_DIR.parent / "pyproject.toml").read_text()
    include_line = next(
        (
            line
            for line in pyproject.splitlines()
            if line.strip().startswith("include =")
        ),
        "",
    )
    assert include_line, "could not find the package-discovery include list"
    assert "evals" not in include_line, (
        "evals is now packaged; the PYTHONPATH/--paths workaround in "
        ".clusterfuzzlite/build.sh is no longer needed"
    )


def test_parse_seeds_select_the_language_they_are_named_for(
    parse_harness: ModuleType,
) -> None:
    """Each seed must reach its own grammar with uncorrupted source.

    The encoding is easy to get backwards -- `ConsumeIntInRange` reads from
    the back of the buffer while `ConsumeBytes` reads from the front -- and
    getting it backwards is silent: the corpus still runs, it just feeds every
    seed to one grammar as byte-corrupted garbage.

    The index a seed carries is positional within whatever grammars are
    LOADED, so it only means the intended language when the full set is
    present. On a base install (no `treesitter-full`) the set is shorter and
    every index points somewhere else, which is a property of the install and
    not a defect -- hence the skip rather than a failure, the contract the
    "Unit Tests (base install)" job enforces (issues #1371, #1410).
    """
    languages = [str(language) for language in parse_harness._LANGUAGES]
    corpus = FUZZ_DIR / "corpus" / "fuzz_parse_source"
    # `edge_*` seeds are about degenerate SOURCE and `repro_*` are crash
    # reproducers kept for regression; neither is named after a language.
    named = sorted(
        seed
        for seed in corpus.iterdir()
        if not seed.stem.startswith(("edge_", "repro_"))
    )

    missing = [seed.stem for seed in named if seed.stem not in languages]
    if missing:
        pytest.skip(
            "grammars not installed for: "
            + ", ".join(missing)
            + " (install the treesitter-full extra to run this)"
        )

    for seed in named:
        provider = _StubProvider(seed.read_bytes())
        index = provider.ConsumeIntInRange(0, len(languages) - 1)
        source = provider.ConsumeBytes(provider.remaining_bytes())

        assert languages[index] == seed.stem, (
            f"{seed.name} decodes to the {languages[index]!r} grammar, "
            f"not {seed.stem!r}"
        )
        # The selector must not survive inside the source it precedes.
        assert source, f"{seed.name} decodes to an empty source"
        assert source.decode("utf-8", "replace")[0].isprintable(), (
            f"{seed.name} source starts with a non-printable byte "
            f"({source[:4]!r}), which is the signature of a stray "
            "selector byte left in the source"
        )


def _decode_shell_seed(harness: ModuleType, data: bytes) -> tuple[str, int, int]:
    provider = _StubProvider(data)
    suffix_choice = provider.ConsumeIntInRange(0, harness.SUFFIX_CHOICES - 1)
    respell_seed = provider.ConsumeIntInRange(0, harness.RESPELL_SEEDS)
    command = provider.ConsumeUnicodeNoSurrogates(harness.MAX_COMMAND_CHARS)
    return command, suffix_choice, respell_seed


def test_shell_seeds_decode_to_the_values_they_were_built_from(
    shell_harness: ModuleType, corpus_builder: ModuleType
) -> None:
    """Each seed must reach the harness as the command, suffix and respelling
    it was written for, and the corpus must hold no seed the builder dropped.

    The integers come off the back of the buffer and the command off the
    front, after a spec byte that only yields ASCII when odd; any slip there
    still runs, it just fuzzes something else.
    """
    corpus = FUZZ_DIR / "corpus" / "fuzz_shell_command"
    table = corpus_builder.SHELL_SEEDS
    assert {seed.stem for seed in corpus.iterdir()} == set(table), (
        "the shell corpus and SHELL_SEEDS disagree; run fuzz/build_corpus.py"
    )
    for name, expected in table.items():
        decoded = _decode_shell_seed(
            shell_harness, (corpus / f"{name}.bin").read_bytes()
        )
        assert decoded == expected, f"{name}.bin decodes to {decoded!r}"

    # Literal spot checks, so a builder and table that drift together still fail.
    assert _decode_shell_seed(
        shell_harness, (corpus / "allowed_ls.bin").read_bytes()
    ) == ("ls -la src", 0, 0)
    assert _decode_shell_seed(
        shell_harness, (corpus / "refused_respelled_rm.bin").read_bytes()
    ) == ("\\r\\m ../outside_project", 27, 29)

    suffixes = {
        choice % len(shell_harness.STRUCTURAL_SUFFIXES)
        for _, choice, _ in table.values()
    }
    operators = {
        choice // len(shell_harness.STRUCTURAL_SUFFIXES)
        for _, choice, _ in table.values()
    }
    assert suffixes == set(range(len(shell_harness.STRUCTURAL_SUFFIXES)))
    assert operators == set(range(len(shell_harness.OPERATORS)))


def test_shell_seeds_reach_the_verdict_their_prefix_names(
    shell_harness: ModuleType, corpus_builder: ModuleType
) -> None:
    """A mislabelled seed fuzzes the wrong side of a gate while looking green."""
    strict, yolo = shell_harness.STRICT, shell_harness.YOLO
    root = shell_harness.SANDBOX_ROOT
    checked = 0
    for name, (command, _choice, _seed) in corpus_builder.SHELL_SEEDS.items():
        if any(
            link in command and not (root / link).is_symlink()
            for link in ("linked_dir", "linked_file", "-linked", "inner_link", "loop")
        ):
            continue
        allowed = strict.refusal(command) is None
        yolo_allowed = yolo.refusal(command) is None
        if name.startswith("allowed_"):
            assert allowed, f"{name}: {strict.refusal(command)}"
        elif name.startswith("nonint_"):
            assert allowed, f"{name}: {strict.refusal(command)}"
            denial = shell_harness._noninteractive_denial(command, root)
            assert denial is not None, f"{name} passes the non-interactive gate"
        elif name.startswith("yolo_only_"):
            assert not allowed, f"{name} is allowed by default"
            assert yolo_allowed, f"{name}: {yolo.refusal(command)}"
        else:
            assert not allowed, f"{name} is allowed by default"
            assert not yolo_allowed, f"{name} is allowed in YOLO mode"
        checked += 1
    assert checked >= len(corpus_builder.SHELL_SEEDS) // 2


def test_incremental_seeds_decode_to_the_plan_they_are_named_for() -> None:
    """Each seed must produce its named edit, and the set must cover every kind.

    The selectors come off the back of the buffer, so a trailing filler string
    silently supplies them instead; that collapsed all eight seeds onto one
    identical plan while every seed still ran and passed.
    """
    editable = (
        "main.py",
        "pkg/__init__.py",
        "pkg/app.py",
        "pkg/unrelated.py",
        "pkg/util.py",
    )
    expected = {
        "truncate": [("main.py", 0)],
        "splice": [("pkg/app.py", 1)],
        "rewrite": [("pkg/app.py", 2)],
        "empty_file": [("pkg/app.py", 3)],
        "delete": [("pkg/util.py", 4)],
        "delete_recreate": [("pkg/app.py", 5)],
        "append_call": [("pkg/util.py", 6)],
        "multi_edit": [("pkg/util.py", 0), ("pkg/app.py", 4), ("main.py", 6)],
        "new_file": [("pkg/util.py", 7)],
        "rename": [("pkg/util.py", 8)],
        "move_to_new_dir": [("pkg/util.py", 9)],
    }

    seen_kinds: set[int] = set()
    corpus = FUZZ_DIR / "corpus" / "fuzz_incremental_update"
    for stem, plan in expected.items():
        seed = corpus / f"{stem}.bin"
        assert seed.exists(), f"missing seed {seed.name}"
        provider = _StubProvider(seed.read_bytes())
        provider.ConsumeBool()
        count = provider.ConsumeIntInRange(1, 3)
        decoded = [
            (
                editable[provider.ConsumeIntInRange(0, len(editable) - 1)],
                provider.ConsumeIntInRange(0, _EDIT_KINDS - 1),
            )
            for _ in range(count)
        ]
        assert decoded == plan, f"{seed.name} decodes to {decoded}"
        seen_kinds.update(kind for _path, kind in decoded)

    assert seen_kinds == set(range(_EDIT_KINDS)), (
        f"the corpus covers edit kinds {sorted(seen_kinds)}, not all {_EDIT_KINDS}"
    )


_EDIT_KINDS = 10


def _reingest_arguments(seed_name: str) -> tuple[list[str], list[str], set[str]]:
    """What `reingest` received for a seed, and which named paths were on disk."""
    harness = import_harness_module("fuzz_incremental_update")
    assert harness.EDIT_KINDS == _EDIT_KINDS
    seed = FUZZ_DIR / "corpus" / "fuzz_incremental_update" / seed_name
    assert seed.exists(), f"missing seed {seed.name}"
    real = harness.GraphUpdater.reingest
    seen: dict[str, object] = {}

    def spy(self, paths, deleted=(), before_write=None):
        named = [*map(str, paths), *map(str, deleted)]
        seen["paths"] = sorted(map(str, paths))
        seen["deleted"] = sorted(map(str, deleted))
        seen["present"] = {p for p in named if (self.repo_path / p).is_file()}
        return real(self, paths, deleted=deleted, before_write=before_write)

    harness.GraphUpdater.reingest = spy
    try:
        harness.fuzz_incremental_update(seed.read_bytes())
    finally:
        harness.GraphUpdater.reingest = real
    assert seen, f"{seed_name} never reached reingest"
    return seen["paths"], seen["deleted"], seen["present"]  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("seed_name", "paths", "deleted"),
    [
        ("new_file.bin", ["pkg/util_new.py"], []),
        ("rename.bin", ["pkg/util_renamed.py"], ["pkg/util.py"]),
        ("move_to_new_dir.bin", ["pkg/moved/util.py"], ["pkg/util.py"]),
    ],
)
def test_incremental_harness_creates_renames_and_moves_files(
    seed_name: str, paths: list[str], deleted: list[str]
) -> None:
    """A path the first index never saw is a shape no in-place edit reaches."""
    got_paths, got_deleted, present = _reingest_arguments(seed_name)
    assert (got_paths, got_deleted) == (paths, deleted)
    assert present == set(paths), f"on disk at reingest: {present}"


def test_the_builder_agrees_with_the_harness_on_files_and_edit_kinds(
    corpus_builder: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale count re-maps every seed's kind byte onto a different edit."""
    corpus_builder._check_editable_matches_harness()
    monkeypatch.setattr(corpus_builder, "EDIT_KINDS", _EDIT_KINDS - 1)
    with pytest.raises(SystemExit, match="EDIT_KINDS"):
        corpus_builder._check_editable_matches_harness()


def test_a_rename_of_an_already_deleted_file_is_not_an_edit() -> None:
    """Renaming needs the original, so a file gone earlier in the plan is skipped.

    Without the skip the rename raises FileNotFoundError: a harness crash that
    reads as a finding in the code under test.
    """
    paths, deleted, present = _reingest_arguments("delete_then_rename.bin")
    assert (paths, deleted, present) == ([], ["pkg/util.py"], set())


@pytest.mark.parametrize(
    "name",
    (
        "fuzz_parse_source",
        "fuzz_shell_command",
        "fuzz_incremental_update",
        "fuzz_cypher_guard",
        "fuzz_dependency_manifest",
    ),
)
def test_every_harness_silences_production_logging(name: str) -> None:
    """A fuzz log of production DEBUG lines buries the one crash report.

    The incremental target's batch log reached hundreds of megabytes, and
    every line is a write libFuzzer waits on instead of executing.
    """
    from loguru import logger

    harness = import_harness_module(name)
    captured: list[str] = []
    sink = logger.add(captured.append, level="DEBUG")

    def emit() -> None:
        # loguru names the module from the caller frame's __name__.
        exec(  # noqa: S102 - a fixed literal, run to fake the caller module
            "logger.warning('probe')",
            {"__name__": "codebase_rag.probe", "logger": logger},
        )

    try:
        emit()
        assert captured, "the probe does not reach a sink; the test is blind"
        captured.clear()
        harness.main()
        emit()
        assert not captured, f"{name}.main() leaves production logging on"
    finally:
        logger.enable("codebase_rag")
        logger.remove(sink)


def test_incremental_harness_runs_every_seed() -> None:
    """Every seed drives the harness without tripping its oracle.

    Includes `repro_1799_deleted_but_present.bin`, whose defect is fixed and
    whose suppression is gone: if #1799 regresses, the differential oracle
    sees the delta and this test fails. That is the point of removing the
    filter rather than keeping it as documentation.
    """
    harness = import_harness_module("fuzz_incremental_update")
    seeds = sorted((FUZZ_DIR / "corpus" / "fuzz_incremental_update").iterdir())
    assert seeds, "the incremental corpus is empty; run fuzz/build_corpus.py"
    for seed in seeds:
        harness.fuzz_incremental_update(seed.read_bytes())


def _stale_main_calls_edge(updater: object) -> None:
    updater.ingestor.ensure_relationship_batch(  # type: ignore[attr-defined]
        ("Function", "qualified_name", "proj.main.main"),
        "CALLS",
        ("Function", "qualified_name", "proj.pkg.app.run"),
    )


def _kept_package_node(updater: object) -> None:
    updater.ingestor.ensure_node_batch(  # type: ignore[attr-defined]
        "Package", {"qualified_name": "proj.pkg", "name": "pkg"}
    )


@pytest.mark.parametrize(
    ("seed_name", "plant", "named"),
    [
        (
            "repro_1794_truncated_to_no_definitions.bin",
            _stale_main_calls_edge,
            "proj.main.main",
        ),
        ("repro_1798_init_deleted.bin", _kept_package_node, "'Package', 'proj.pkg'"),
        (
            "repro_1798_init_deleted_fresh.bin",
            _kept_package_node,
            "'Package', 'proj.pkg'",
        ),
    ],
)
def test_a_closed_defect_reappearing_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, seed_name: str, plant: object, named: str
) -> None:
    """#1794 and #1798 are fixed, so their exact deltas are findings again.

    The harness used to discard both shapes as known defects. Each case plants
    the very row the old filter explained, on the plan that filter required,
    and requires the run to fail naming it.
    """
    harness = import_harness_module("fuzz_incremental_update")
    seed = FUZZ_DIR / "corpus" / "fuzz_incremental_update" / seed_name
    assert seed.exists(), f"missing seed {seed.name}"
    real = harness.GraphUpdater.reingest

    def planting(self, paths, deleted=(), before_write=None):
        result = real(self, paths, deleted=deleted, before_write=before_write)
        plant(self)  # type: ignore[operator]
        return result

    monkeypatch.setattr(harness.GraphUpdater, "reingest", planting)
    with pytest.raises(AssertionError, match="reingest disagreed") as raised:
        harness.fuzz_incremental_update(seed.read_bytes())
    assert named in str(raised.value)


def test_the_1799_seed_really_reaches_a_deleted_path_that_exists() -> None:
    """The reproducer must actually drive the fixed code path.

    Written because the first seed committed under this name did NOT: it
    decoded to two plain deletes, so both paths were genuinely absent by the
    time `reingest` ran and the race was never exercised. The harness passed
    for a reason unrelated to #1799, which is indistinguishable from passing
    because the fix works.

    Asserting the decoded plan is not enough either -- what matters is the
    state at the call, so this observes the arguments `reingest` actually
    receives and checks the path is on disk at that moment.
    """
    harness = import_harness_module("fuzz_incremental_update")
    seed = (
        FUZZ_DIR
        / "corpus"
        / "fuzz_incremental_update"
        / "repro_1799_deleted_but_present.bin"
    )
    assert seed.exists(), f"missing seed {seed.name}"

    real = harness.GraphUpdater.reingest
    seen: dict[str, bool] = {}

    def spy(self, paths, deleted=(), before_write=None):
        for raw in deleted:
            seen[str(raw)] = (self.repo_path / str(raw)).is_file()
        return real(self, paths, deleted=deleted, before_write=before_write)

    harness.GraphUpdater.reingest = spy
    try:
        harness.fuzz_incremental_update(seed.read_bytes())
    finally:
        harness.GraphUpdater.reingest = real

    assert seen, "the seed never reached reingest with a deleted path"
    assert any(seen.values()), (
        f"the #1799 seed names {seen}, none of them present on disk, "
        "so it never exercises the atomic-save race"
    )


@pytest.fixture(scope="module")
def cypher_harness() -> ModuleType:
    return import_harness_module("fuzz_cypher_guard")


def _cypher_seeds() -> list[Path]:
    seeds = sorted((FUZZ_DIR / "corpus" / "fuzz_cypher_guard").glob("*.bin"))
    assert seeds, "the cypher corpus is empty; run fuzz/build_corpus.py"
    return seeds


def _cypher_seed(name: str) -> bytes:
    seed = FUZZ_DIR / "corpus" / "fuzz_cypher_guard" / f"{name}.bin"
    assert seed.exists(), f"missing seed {seed.name}"
    return seed.read_bytes()


def test_cypher_harness_runs_every_seed(cypher_harness: ModuleType) -> None:
    for seed in _cypher_seeds():
        cypher_harness.fuzz_cypher_guard(seed.read_bytes())


def test_the_cypher_builder_agrees_with_the_harness_on_the_layout(
    cypher_harness: ModuleType, corpus_builder: ModuleType
) -> None:
    """A silent mismatch would point every seed at the wrong token or table."""
    h, b = cypher_harness, corpus_builder
    assert (b.CYPHER_HEADER, b.CYPHER_MAX_PAYLOAD) == (h.HEADER, h.MAX_PAYLOAD)
    assert b.CYPHER_MODES == {
        "raw": h.MODE_RAW,
        "query": h.MODE_QUERY,
        "plan": h.MODE_PLAN,
    }
    assert b.CYPHER_TOKEN_KINDS == tuple(f.__name__[1:] for f in h.TOKEN_KINDS)
    assert b.CYPHER_PLAN_CHOICES == {
        "read": h.PLAN_READ,
        "write": h.PLAN_WRITE,
        "procedure": h.PLAN_PROCEDURE,
        "unknown": h.PLAN_UNKNOWN,
    }


def test_the_cypher_builder_refuses_a_payload_the_harness_would_cut(
    corpus_builder: ModuleType,
) -> None:
    payload = b"x" * (corpus_builder.CYPHER_MAX_PAYLOAD + 1)
    with pytest.raises(ValueError, match="payload"):
        corpus_builder.cypher_record(0, 0, 0, payload)


def _query_verdict(h: ModuleType, name: str, query: str, tokens: list[Any]) -> bool:
    def rejects(validator: Any) -> bool:
        return bool(h._rejects(validator, query))

    procedures = [t.procedure for t in tokens if t.procedure is not None]
    ranges = [t for t in tokens if t.raw.startswith("-[") and "*" in t.raw]
    if name.startswith("query_inert_"):
        return all(t.inert for t in tokens) and not any(
            rejects(v) for v in h.VALIDATORS
        )
    if name.startswith("query_keyword_"):
        return any(t.keyword for t in tokens) and rejects(h._validate_cypher_read_only)
    if name.startswith("query_procedure_denied_"):
        return any(not h.is_allowed_procedure(p) for p in procedures) and rejects(
            h._validate_call_procedures
        )
    if name.startswith("query_procedure_allowed_"):
        return (
            bool(procedures)
            and all(h.is_allowed_procedure(p) for p in procedures)
            and not rejects(h._validate_call_procedures)
        )
    if name.startswith("query_unbounded_"):
        return any(t.unbounded for t in tokens) and rejects(
            h._validate_no_unbounded_paths
        )
    if name.startswith("query_bounded_"):
        return (
            bool(ranges)
            and not any(t.unbounded for t in tokens)
            and not rejects(h._validate_no_unbounded_paths)
        )
    if name.startswith("query_identifier_"):
        return any(h.cs.CYPHER_BACKTICK in t.raw for t in tokens)
    raise AssertionError(f"{name}: no verdict is defined for this prefix")


def _plan_verdict(h: ModuleType, name: str, body: bytes) -> bool:
    planned, rows, pairs = h.build_plan(body)
    refused = [
        h._refused(h.memgraph_plan_operators(rows)),
        h._refused(h.neo4j_plan_operators(pairs)),
    ]
    if name.startswith("plan_reads_"):
        return bool(planned) and all(p.reads for p in planned) and not any(refused)
    if name.startswith("plan_writes_"):
        return any(p.writes for p in planned) and all(refused)
    if name.startswith("plan_refused_"):
        return not any(p.writes for p in planned) and all(refused)
    raise AssertionError(f"{name}: no verdict is defined for this prefix")


def _raw_verdict(h: ModuleType, name: str, body: bytes) -> bool:
    text = body.decode(errors="replace")
    if name.startswith("raw_writes_"):
        lines = text.splitlines()
        pairs = [tuple((line.split("\t", 1) + [""])[:2]) for line in lines]
        found = [
            operators
            for operators in (
                h.memgraph_plan_operators(lines),
                h.neo4j_plan_operators(pairs),
            )
            if any(o.name in h.WRITE_OPERATORS for o in operators)
        ]
        return bool(found) and all(h._refused(o) for o in found)
    if name.startswith("raw_unbounded_"):
        return h._rejects(
            h._validate_no_unbounded_paths, h._clean_cypher_response(text)
        )
    return name.startswith("raw_")


def test_cypher_seeds_reach_the_verdict_their_prefix_names(
    cypher_harness: ModuleType,
) -> None:
    """Each seed is named for the mode it selects and the verdict it reaches.

    A seed decoding to something else -- a renumbered table, a mode byte off
    by one -- still runs green, so only this catches it.
    """
    h = cypher_harness
    modes = {"raw": h.MODE_RAW, "query": h.MODE_QUERY, "plan": h.MODE_PLAN}
    for seed in _cypher_seeds():
        name, data = seed.stem, seed.read_bytes()
        prefix = name.split("_", 1)[0]
        assert data[0] % h.MODES == modes[prefix], f"{name} selects another mode"
        body = data[1:]
        if prefix == "query":
            query, _, tokens = h.build_query(body)
            reached = _query_verdict(h, name, query, tokens)
        elif prefix == "plan":
            reached = _plan_verdict(h, name, body)
        else:
            reached = _raw_verdict(h, name, body)
        assert reached, f"{name} does not reach the verdict its name states"


def test_the_bypass_seeds_hold_a_bracket_or_star_inside_a_backtick_name(
    cypher_harness: ModuleType,
) -> None:
    h = cypher_harness
    for name, inside in (
        ("query_unbounded_bracket_in_backtick_name", "]"),
        ("query_bounded_star_in_backtick_name", "*"),
    ):
        query, _, _ = h.build_query(_cypher_seed(name)[1:])
        names = query.split(h.cs.CYPHER_BACKTICK)[1::2]
        assert any(inside in n for n in names), f"{name} built {query!r}"


def _accept(*_: Any) -> None:
    return None


def _refuse_every_plan(cypher_harness: ModuleType) -> Any:
    def refuse(operators: Any, query: str) -> None:
        raise cypher_harness.ex.ReadOnlyQueryError(query)

    return refuse


@pytest.mark.parametrize(
    ("seed", "target", "broken", "message"),
    [
        (
            "query_inert_literals_and_comments",
            "mask_literals_and_comments",
            lambda query, **_: query,
            "masked",
        ),
        (
            "query_keyword_split_by_block_comment",
            "_validate_cypher_read_only",
            _accept,
            "write keyword accepted",
        ),
        (
            "query_procedure_denied_backticked",
            "_validate_call_procedures",
            _accept,
            "disallowed procedure accepted",
        ),
        (
            "query_unbounded_bracket_in_backtick_name",
            "_validate_no_unbounded_paths",
            _accept,
            "unbounded path accepted",
        ),
        (
            "plan_writes_create_and_detach_delete",
            "check_plan",
            _accept,
            "writing plan was accepted",
        ),
        ("plan_refused_empty", "check_plan", _accept, "empty plan was accepted"),
        (
            "plan_reads_scans_and_allowed_procedure",
            "memgraph_plan_operators",
            lambda rows: [],
            "memgraph parsed",
        ),
        (
            "plan_reads_scans_and_allowed_procedure",
            "neo4j_plan_operators",
            lambda pairs: [],
            "neo4j parsed",
        ),
        (
            "raw_fenced_response",
            "_clean_cypher_response",
            lambda text: text,
            "terminator",
        ),
        (
            "raw_writes_memgraph_rows",
            "check_plan",
            _accept,
            "writing plan was accepted",
        ),
    ],
)
def test_the_cypher_harness_catches_a_broken_guard(
    cypher_harness: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    seed: str,
    target: str,
    broken: Any,
    message: str,
) -> None:
    data = _cypher_seed(seed)
    cypher_harness.fuzz_cypher_guard(data)
    monkeypatch.setattr(cypher_harness, target, broken)
    with pytest.raises(AssertionError, match=message):
        cypher_harness.fuzz_cypher_guard(data)


def test_the_cypher_harness_catches_a_plan_check_refusing_a_read(
    cypher_harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing closed on a read is a broken guard too: the query never runs."""
    data = _cypher_seed("plan_reads_scans_and_allowed_procedure")
    cypher_harness.fuzz_cypher_guard(data)
    monkeypatch.setattr(
        cypher_harness, "check_plan", _refuse_every_plan(cypher_harness)
    )
    with pytest.raises(AssertionError, match="read-only plan was refused"):
        cypher_harness.fuzz_cypher_guard(data)


def test_the_cypher_harness_catches_a_range_check_reading_names_as_syntax(
    cypher_harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The range check before backtick names became placeholders.

    Unquoted, ``-[`a]b`*1..]->`` hides its range behind the name's `]` and
    ``-[`*`*1..3]->`` reads the name's `*` as one; the harness must flag both
    directions.
    """
    from codebase_rag.services import llm

    unquoting = llm.mask_literals_and_comments
    monkeypatch.setattr(
        llm, "mask_literals_and_comments", lambda query, **_: unquoting(query)
    )
    with pytest.raises(AssertionError, match="unbounded path accepted"):
        cypher_harness.fuzz_cypher_guard(
            _cypher_seed("query_unbounded_bracket_in_backtick_name")
        )
    with pytest.raises(AssertionError, match="bounded path rejected"):
        cypher_harness.fuzz_cypher_guard(
            _cypher_seed("query_bounded_star_in_backtick_name")
        )


def test_the_cypher_harness_lets_an_undocumented_exception_escape(
    cypher_harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crashes(query: str) -> None:
        raise IndexError(query)

    data = _cypher_seed("raw_fenced_response")
    cypher_harness.fuzz_cypher_guard(data)
    monkeypatch.setattr(cypher_harness, "VALIDATORS", (crashes,))
    with pytest.raises(IndexError):
        cypher_harness.fuzz_cypher_guard(data)


def test_the_cypher_harness_is_quiet_on_an_empty_input(
    cypher_harness: ModuleType,
) -> None:
    cypher_harness.fuzz_cypher_guard(b"")


@pytest.fixture(scope="module")
def dependency_harness() -> ModuleType:
    return import_harness_module("fuzz_dependency_manifest")


def _dependency_seeds() -> list[Path]:
    seeds = sorted((FUZZ_DIR / "corpus" / "fuzz_dependency_manifest").glob("*.bin"))
    assert seeds, "the dependency corpus is empty; run fuzz/build_corpus.py"
    return seeds


def _dependency_seed(name: str) -> bytes:
    seed = FUZZ_DIR / "corpus" / "fuzz_dependency_manifest" / f"{name}.bin"
    assert seed.exists(), f"missing seed {seed.name}"
    return seed.read_bytes()


def test_dependency_harness_runs_every_seed(dependency_harness: ModuleType) -> None:
    for seed in _dependency_seeds():
        dependency_harness.fuzz_dependency_manifest(seed.read_bytes())


def test_the_dependency_builder_agrees_with_the_harness_on_the_layout(
    dependency_harness: ModuleType, corpus_builder: ModuleType
) -> None:
    """A silent mismatch would point every seed at the wrong manifest."""
    h, b = dependency_harness, corpus_builder
    assert (b.DEPENDENCY_HEADER, b.DEPENDENCY_MAX_PAYLOAD) == (
        h.HEADER,
        h.MAX_PAYLOAD,
    )
    assert b.DEPENDENCY_MODES == {"raw": h.MODE_RAW, "manifest": h.MODE_MANIFEST}
    assert [m for m, _ in b.DEPENDENCY_MANIFESTS.values()] == list(h.MANIFESTS)
    assert [n for _, n in b.DEPENDENCY_MANIFESTS.values()] == [n for n, _ in h.BUILDERS]
    assert b.DEPENDENCY_SHAPES == ("versioned", "bare", "rich", "wrong", "decoy")
    assert (h.SHAPE_VERSIONED, h.SHAPE_BARE, h.SHAPE_RICH) == (0, 1, 2)
    assert (h.SHAPE_WRONG, h.SHAPE_DECOY, h.SHAPES) == (3, 4, 5)
    assert b.DEPENDENCY_SECTION_SHAPES == (
        "well_formed",
        "string",
        "number",
        "container",
    )
    assert (h.SECTION_STRING, h.SECTION_NUMBER, h.SECTION_CONTAINER) == (1, 2, 3)
    assert b.DEPENDENCY_WRONG_VALUES == max(
        len(h.JSON_WRONG_VALUES), len(h.TOML_WRONG_VALUES)
    )


def test_the_dependency_builder_refuses_a_payload_the_harness_would_cut(
    corpus_builder: ModuleType,
) -> None:
    payload = b"x" * (corpus_builder.DEPENDENCY_MAX_PAYLOAD + 1)
    with pytest.raises(ValueError, match="payload"):
        corpus_builder.dependency_record(0, 0, 0, payload)


def test_dependency_seeds_reach_the_manifest_and_verdict_their_name_states(
    dependency_harness: ModuleType, corpus_builder: ModuleType
) -> None:
    """`<mode>_<manifest>_<what>`: a seed decoding to another manifest, or a
    `wrong_sections` seed whose sections all came out well formed, still runs
    green, so only this catches it."""
    h, b = dependency_harness, corpus_builder
    modes = {"raw": h.MODE_RAW, "manifest": h.MODE_MANIFEST}
    for seed in _dependency_seeds():
        mode, token, what = seed.stem.split("_", 2)
        data = seed.read_bytes()
        assert data[0] % h.MODES == modes[mode], f"{seed.stem} selects another mode"
        manifest = h.MANIFESTS[data[1] % len(h.MANIFESTS)]
        assert manifest == b.DEPENDENCY_MANIFESTS[token][0], seed.stem
        if mode == "raw":
            continue
        sections = b.DEPENDENCY_MANIFESTS[token][1]
        entries = h.read_entries(data[3:], sections)
        built = h.build_manifest(data[1], data[2:])
        declared = {t[0] for t in built.expected} | set(built.loose)
        for section in range(sections):
            shapes = {e.shape for e in entries if e.section == section}
            assert shapes == set(range(h.SHAPES)), f"{seed.stem} section {section}"
        if what == "every_shape":
            assert data[2] == 0, f"{seed.stem} has a malformed section"
            assert declared, f"{seed.stem} declares nothing"
        else:
            assert what == "wrong_sections", seed.stem
            dropped = [
                e
                for e in entries
                if e.shape != h.SHAPE_DECOY
                and h._section_shape(data[2], e.section) != h.SECTION_WELL_FORMED
                and e.name not in declared
            ]
            assert dropped, f"{seed.stem} drops no entry"


def _invent(found: list[Any]) -> list[Any]:
    from codebase_rag.models import Dependency

    return [*found, Dependency("x", "")]


def _lose_last(found: list[Any]) -> list[Any]:
    return found[:-1]


def _respell(found: list[Any]) -> list[Any]:
    from codebase_rag.models import Dependency

    return [Dependency(d.name, d.spec + "!", d.properties) for d in found]


def _number_spec(found: list[Any]) -> list[Any]:
    from codebase_rag.models import Dependency

    return [Dependency(d.name, 7, d.properties) for d in found]  # type: ignore[arg-type]


def _number_group(found: list[Any]) -> list[Any]:
    from codebase_rag.models import Dependency

    return [Dependency(d.name, d.spec, {"group": 1}) for d in found]  # type: ignore[dict-item]


def _swallow(found: list[Any]) -> list[Any]:
    from codebase_rag.parsers import dependency_parser

    dependency_parser.logger.error("boom")
    return found


@pytest.mark.parametrize(
    ("seed", "broken", "message"),
    [
        ("manifest_packagejson_every_shape", _invent, "reported"),
        ("manifest_gomod_every_shape", _lose_last, "reported"),
        ("manifest_csproj_every_shape", _respell, "reported"),
        ("manifest_cargo_every_shape", _number_spec, "wrong type"),
        ("raw_packagejson_number_version", _number_spec, "wrong type"),
        ("manifest_pyproject_every_shape", _number_group, "wrong type"),
        ("manifest_requirements_every_shape", _swallow, "well-formed manifest failed"),
    ],
    ids=[
        "invented",
        "missing",
        "respelled",
        "number-spec",
        "number-spec-raw",
        "number-property",
        "swallowed-failure",
    ],
)
def test_the_dependency_harness_catches_a_broken_parser(
    dependency_harness: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    seed: str,
    broken: Callable[[list[Any]], list[Any]],
    message: str,
) -> None:
    h = dependency_harness
    data = _dependency_seed(seed)
    h.fuzz_dependency_manifest(data)
    real = h.parse_dependencies
    monkeypatch.setattr(h, "parse_dependencies", lambda path: broken(real(path)))
    with pytest.raises(AssertionError, match=message):
        h.fuzz_dependency_manifest(data)


def test_the_dependency_harness_catches_a_lost_poetry_entry(
    dependency_harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poetry writes a table or a number as its Python text, so those entries
    are checked by name alone, apart from the exact ones."""
    h = dependency_harness
    data = _dependency_seed("manifest_pyproject_every_shape")
    loose = h.build_manifest(data[1], data[2:]).loose
    assert loose, "the seed holds no poetry table or number"
    real = h.parse_dependencies
    monkeypatch.setattr(
        h,
        "parse_dependencies",
        lambda path: [d for d in real(path) if d.name != loose[0]],
    )
    with pytest.raises(AssertionError, match="reported"):
        h.fuzz_dependency_manifest(data)


def test_the_dependency_harness_catches_a_string_read_as_a_list(
    dependency_harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect that read `dependencies = "requests>=2"` one package per
    character."""
    h = dependency_harness
    data = _dependency_seed("manifest_pyproject_wrong_sections")
    h.fuzz_dependency_manifest(data)
    monkeypatch.setattr(
        h.dependency_parser,
        "_lines",
        lambda value: list(value) if isinstance(value, list | str) else [],
    )
    with pytest.raises(AssertionError, match="reported"):
        h.fuzz_dependency_manifest(data)


def test_the_dependency_raw_mode_tolerates_a_manifest_that_fails_to_parse(
    dependency_harness: ModuleType,
) -> None:
    h = dependency_harness
    data = _dependency_seed("raw_requirements_undecodable")
    _, failures = h.parse(h._write(data[1], data[2:]))
    assert failures, "the seed parses, so it does not test the tolerance"
    h.fuzz_dependency_manifest(data)


def test_the_dependency_harness_restores_the_parser_logger(
    dependency_harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = dependency_harness
    real = h.dependency_parser.logger

    def crashes(path: Path) -> list[Any]:
        raise OSError(path)

    monkeypatch.setattr(h, "parse_dependencies", crashes)
    with pytest.raises(OSError):
        h.fuzz_dependency_manifest(_dependency_seed("manifest_gomod_every_shape"))
    assert h.dependency_parser.logger is real


def test_the_dependency_harness_is_quiet_on_a_short_input(
    dependency_harness: ModuleType,
) -> None:
    dependency_harness.fuzz_dependency_manifest(b"")
    dependency_harness.fuzz_dependency_manifest(b"\x01")
