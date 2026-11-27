"""`CGR_INDEX_ENGINE`: which engine indexes each language.

Phase 1 of the Rust rewrite (docs/reports/PHASE1_SEAMS.md, section 1). The
setting is parsed and validated at start-up, and resolved to an engine per
language. No language can run on Rust yet, so every value resolves to Python
everywhere; the tests that patch `rust_engine_languages()` or the defaults
show what each value will do once one can.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger
from pydantic import ValidationError

from codebase_rag import config, index_engine
from codebase_rag import constants as cs
from codebase_rag.index_engine import (
    IndexEngineSelection,
    default_index_engine,
    parse_index_engine,
    resolve_index_engines,
    rust_engine_languages,
)

L = cs.SupportedLanguage
E = cs.IndexEngine
REPO_ROOT = Path(__file__).resolve().parents[2]
_JS_FAMILY = (L.JS, L.TS, L.TSX)
_C_FAMILY = (L.C, L.CPP)
_ENGINES = "auto, python, rust"
_LANGUAGES = ", ".join(L)


def _selection(
    default: cs.IndexEngine, **languages: cs.IndexEngine
) -> IndexEngineSelection:
    return IndexEngineSelection(
        default=default,
        languages={L(name): engine for name, engine in languages.items()},
    )


def _refusal(spec: str) -> str:
    with pytest.raises(ValueError) as caught:
        parse_index_engine(spec)
    return str(caught.value)


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    # Each test starts with no language warned about yet, as a new process does.
    monkeypatch.setattr(index_engine, "_warned", set())
    seen: list[tuple[str, str]] = []
    sink = logger.add(
        lambda message: seen.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="DEBUG",
        filter=lambda record: record["name"] == index_engine.__name__,
    )
    yield seen
    logger.remove(sink)


@pytest.fixture
def go_on_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    # A build that can index Go on Rust, as phase 3's will.
    monkeypatch.setattr(
        index_engine, "rust_engine_languages", lambda: frozenset({L.GO})
    )


def _warnings(seen: list[tuple[str, str]]) -> list[str]:
    return [message for level, message in seen if level == "WARNING"]


class TestTheValueForms:
    def test_the_default_is_auto(self) -> None:
        assert config.AppConfig.model_fields["INDEX_ENGINE"].default == "auto"
        assert parse_index_engine("auto") == _selection(E.AUTO)

    @pytest.mark.parametrize("engine", list(E))
    def test_an_engine_on_its_own_sets_every_language(
        self, engine: cs.IndexEngine
    ) -> None:
        assert parse_index_engine(engine) == _selection(engine)

    def test_a_language_set_alone_leaves_the_rest_on_auto(self) -> None:
        assert parse_index_engine("go=rust") == _selection(E.AUTO, go=E.RUST)

    def test_a_default_and_a_language(self) -> None:
        assert parse_index_engine("python,go=rust") == _selection(E.PYTHON, go=E.RUST)

    def test_everything_on_rust_but_one_family(self) -> None:
        assert parse_index_engine("rust,c=python,cpp=python") == _selection(
            E.RUST, c=E.PYTHON, cpp=E.PYTHON
        )

    def test_the_order_of_tokens_does_not_matter(self) -> None:
        assert parse_index_engine("go=rust,python") == parse_index_engine(
            "python,go=rust"
        )

    def test_a_language_may_ask_for_the_releases_choice(self) -> None:
        assert parse_index_engine("python,go=auto") == _selection(E.PYTHON, go=E.AUTO)

    def test_python_and_rust_are_languages_after_a_name_and_engines_alone(
        self,
    ) -> None:
        assert parse_index_engine("python=rust,rust=python") == _selection(
            E.AUTO, python=E.RUST, rust=E.PYTHON
        )

    @pytest.mark.parametrize(
        "spec",
        [
            "python,go=rust,java=rust",
            "python;go=rust;java=rust",
            "python go=rust java=rust",
            " python ;go=rust,, java=rust ",
        ],
    )
    def test_tokens_split_on_commas_semicolons_or_spaces(self, spec: str) -> None:
        # The same splitting as CGR_CAPTURE.
        assert parse_index_engine(spec) == _selection(E.PYTHON, go=E.RUST, java=E.RUST)

    def test_any_case_is_read(self) -> None:
        assert parse_index_engine("PYTHON,Go=Rust") == _selection(E.PYTHON, go=E.RUST)

    @pytest.mark.parametrize("spec", ["", " ", ",;"])
    def test_a_value_with_no_tokens_is_auto(self, spec: str) -> None:
        assert parse_index_engine(spec) == _selection(E.AUTO)

    @pytest.mark.parametrize(
        "unit",
        sorted(cs.INDEX_ENGINE_DEFAULTS, key=sorted),
        ids=lambda unit: "+".join(sorted(unit)),
    )
    def test_every_language_is_named_as_supported_language_names_it(
        self, unit: frozenset[cs.SupportedLanguage]
    ) -> None:
        spec = ",".join(f"{language}=rust" for language in sorted(unit))

        selection = parse_index_engine(spec)

        assert selection.languages == dict.fromkeys(unit, E.RUST)


class TestTheRefusals:
    def test_an_unknown_language(self) -> None:
        assert _refusal("fortran=rust") == (
            f"'fortran' is not a language; use one of {_LANGUAGES}."
        )

    def test_an_unknown_engine_for_a_language(self) -> None:
        assert _refusal("go=fortran") == (
            f"'fortran' is not an engine; use one of {_ENGINES}."
        )

    def test_an_unknown_engine_on_its_own(self) -> None:
        assert _refusal("python,fast") == (
            f"'fast' is not an engine; use one of {_ENGINES}."
        )

    def test_a_language_without_an_engine_is_not_an_engine(self) -> None:
        assert _refusal("go") == f"'go' is not an engine; use one of {_ENGINES}."

    def test_a_language_named_twice(self) -> None:
        assert _refusal("go=rust,go=python") == (
            "'go' is named twice; name each language once."
        )

    def test_a_language_named_twice_in_different_case(self) -> None:
        assert _refusal("go=rust GO=rust") == (
            "'go' is named twice; name each language once."
        )

    @pytest.mark.parametrize(
        ("spec", "first", "second"),
        [("python,rust", "python", "rust"), ("auto;Auto", "auto", "auto")],
    )
    def test_two_bare_engines(self, spec: str, first: str, second: str) -> None:
        assert _refusal(spec) == (
            f"'{first}' and '{second}' are both engines on their own; give at most one."
        )

    def test_nothing_after_the_equals_sign(self) -> None:
        assert _refusal("python,go=") == (
            f"'go=' names no engine after '='; use one of {_ENGINES}."
        )

    def test_nothing_before_the_equals_sign(self) -> None:
        assert _refusal("=rust") == f"'' is not a language; use one of {_LANGUAGES}."

    def test_the_settings_refuse_it_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CGR_INDEX_ENGINE", raising=False)

        with pytest.raises(ValidationError, match="CGR_INDEX_ENGINE"):
            config.AppConfig(CGR_INDEX_ENGINE="go=fortran")


class TestTheFamilyRule:
    def test_javascript_alone_would_split_its_family(self) -> None:
        assert _refusal("javascript=rust") == (
            "javascript, typescript, tsx take one engine together, as they "
            "resolve each other's imports and includes; this value gives them "
            "javascript=rust, typescript=auto, tsx=auto."
        )

    def test_c_alone_would_split_its_family(self) -> None:
        assert _refusal("rust,c=python") == (
            "c, cpp take one engine together, as they resolve each other's "
            "imports and includes; this value gives them c=python, cpp=rust."
        )

    def test_auto_and_python_are_different_requests(self) -> None:
        # Both are Python today, but `auto` follows the release: once the
        # family's default moves, the same value would split it. Refusing it
        # now keeps a value valid on every release or on none.
        assert "typescript=python" in _refusal("python,javascript=auto,tsx=auto")

    @pytest.mark.parametrize(
        "spec",
        [
            "javascript=rust,typescript=rust,tsx=rust",
            "rust,c=python,cpp=python",
            "python,typescript=python",
            "javascript=auto",
            "rust,tsx=rust",
        ],
    )
    def test_a_family_given_one_engine_is_accepted(self, spec: str) -> None:
        parse_index_engine(spec)

    def test_the_defaults_give_each_family_one_entry(self) -> None:
        units = list(cs.INDEX_ENGINE_DEFAULTS)

        assert frozenset(_JS_FAMILY) in units
        assert frozenset(_C_FAMILY) in units
        assert [unit for unit in units if len(unit) > 1] == [
            frozenset(_JS_FAMILY),
            frozenset(_C_FAMILY),
        ]

    def test_the_defaults_name_every_language_once(self) -> None:
        named = [language for unit in cs.INDEX_ENGINE_DEFAULTS for language in unit]

        assert sorted(named) == sorted(L)

    def test_the_rust_engine_lists_whole_families_only(self) -> None:
        available = rust_engine_languages()

        for unit in cs.INDEX_ENGINE_DEFAULTS:
            assert unit <= available or not unit & available


# What this build resolves without a preview, whatever the session previews.
@pytest.mark.usefixtures("no_engine_preview")
class TestTheResolution:
    def test_no_language_runs_on_rust_in_this_build(self) -> None:
        assert rust_engine_languages() == frozenset()

    def test_every_default_is_a_concrete_engine(self) -> None:
        assert set(cs.INDEX_ENGINE_DEFAULTS.values()) <= {E.PYTHON, E.RUST}

    def test_every_language_defaults_to_python_in_phase_one(self) -> None:
        assert set(cs.INDEX_ENGINE_DEFAULTS.values()) == {E.PYTHON}

    @pytest.mark.parametrize("spec", ["auto", "python", "rust"])
    def test_every_engine_on_its_own_gives_python_everywhere(
        self, spec: str, records: list[tuple[str, str]]
    ) -> None:
        engines = resolve_index_engines(parse_index_engine(spec))

        assert engines == dict.fromkeys(L, E.PYTHON)
        assert list(engines) == list(L)
        assert _warnings(records) == []

    def test_rust_by_default_falls_back_with_a_debug_line_only(
        self, records: list[tuple[str, str]]
    ) -> None:
        resolve_index_engines(parse_index_engine("rust"))

        assert [level for level, _ in records] == ["DEBUG"]

    def test_rust_by_name_gives_python_and_one_warning(
        self, records: list[tuple[str, str]]
    ) -> None:
        selection = parse_index_engine("go=rust")

        first = resolve_index_engines(selection)
        second = resolve_index_engines(selection)

        assert first == second == dict.fromkeys(L, E.PYTHON)
        assert _warnings(records) == [
            "CGR_INDEX_ENGINE asks for go on Rust, which this build cannot "
            "index it on; go indexes on Python"
        ]

    def test_each_language_named_on_rust_is_warned_about_once(
        self, records: list[tuple[str, str]]
    ) -> None:
        resolve_index_engines(parse_index_engine("python,go=rust"))
        resolve_index_engines(parse_index_engine("go=rust,java=rust"))

        warned = _warnings(records)
        assert len(warned) == 2
        assert re.search(r"\bgo\b", warned[0])
        assert re.search(r"\bjava\b", warned[1])

    def test_a_language_the_build_has_runs_on_rust_when_asked(
        self, go_on_rust: None, records: list[tuple[str, str]]
    ) -> None:
        engines = resolve_index_engines(parse_index_engine("python,go=rust"))

        assert engines == {**dict.fromkeys(L, E.PYTHON), L.GO: E.RUST}
        assert records == []

    def test_rust_on_its_own_moves_only_what_the_build_has(
        self, go_on_rust: None
    ) -> None:
        engines = resolve_index_engines(parse_index_engine("rust"))

        assert {language for language, e in engines.items() if e == E.RUST} == {L.GO}

    def test_auto_follows_the_defaults_table(
        self, go_on_rust: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Phase 3 moves Go by changing its entry; `go=python` is the way back.
        monkeypatch.setitem(cs.INDEX_ENGINE_DEFAULTS, frozenset({L.GO}), E.RUST)

        auto = resolve_index_engines(parse_index_engine("auto"))
        back = resolve_index_engines(parse_index_engine("go=python"))

        assert auto[L.GO] == E.RUST
        assert auto[L.JAVA] == E.PYTHON
        assert back[L.GO] == E.PYTHON

    def test_a_default_the_build_cannot_run_falls_back_quietly(
        self, monkeypatch: pytest.MonkeyPatch, records: list[tuple[str, str]]
    ) -> None:
        # A `.env` that works on one build must not break, or warn, on another.
        monkeypatch.setitem(cs.INDEX_ENGINE_DEFAULTS, frozenset({L.GO}), E.RUST)

        engines = resolve_index_engines(parse_index_engine("go=auto"))

        assert engines[L.GO] == E.PYTHON
        assert _warnings(records) == []


class TestTheDefaultSelection:
    # What `GraphUpdater` reads when it is given no selection, as `capture`
    # reads `CGR_CAPTURE` through `default_capture()`.
    def test_it_is_the_settings_value_parsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config.settings, "INDEX_ENGINE", "python,go=rust")

        assert default_index_engine() == _selection(E.PYTHON, go=E.RUST)

    def test_unset_it_is_auto_everywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config.settings, "INDEX_ENGINE", "auto")

        assert default_index_engine() == _selection(E.AUTO)

    def test_it_reads_the_setting_each_time_it_is_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The one read per updater is the updater's to make; this function
        # keeps no copy that a changed setting would leave stale.
        monkeypatch.setattr(config.settings, "INDEX_ENGINE", "python")
        first = default_index_engine()
        monkeypatch.setattr(config.settings, "INDEX_ENGINE", "rust")

        assert first == _selection(E.PYTHON)
        assert default_index_engine() == _selection(E.RUST)


class TestStartUpValidation:
    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        monkeypatch.delenv("CGR_INDEX_ENGINE", raising=False)
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_unset_is_auto(self, clean_env: Path) -> None:
        loaded, errors = config.load_settings(frozenset(os.environ))

        assert errors == ()
        assert loaded.INDEX_ENGINE == "auto"

    def test_it_is_read_from_cgr_index_engine_and_stored_lower_case(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CGR_INDEX_ENGINE", "Python,GO=Rust")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert errors == ()
        assert loaded.INDEX_ENGINE == "python,go=rust"

    def test_a_project_dotenv_may_set_it(self, clean_env: Path) -> None:
        (clean_env / ".env").write_text("CGR_INDEX_ENGINE=python\n", encoding="utf-8")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert errors == ()
        assert loaded.INDEX_ENGINE == "python"

    def test_an_invalid_value_is_refused_and_falls_back_to_auto(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CGR_INDEX_ENGINE", "go=fortran")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert loaded.INDEX_ENGINE == "auto"
        assert errors == (
            "Invalid value for CGR_INDEX_ENGINE in the environment: "
            f"'fortran' is not an engine; use one of {_ENGINES}.",
        )

    def test_a_split_family_is_refused_at_start_up(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CGR_INDEX_ENGINE", "cpp=rust")

        _, errors = config.load_settings(frozenset(os.environ))

        assert len(errors) == 1
        assert errors[0].startswith(
            "Invalid value for CGR_INDEX_ENGINE in the environment: c, cpp "
        )


class TestThePublicSurface:
    def test_the_snapshot_records_the_setting(self) -> None:
        snapshot = json.loads(
            (REPO_ROOT / "docs" / "reference" / "public-surface.json").read_text(
                encoding="utf-8"
            )
        )

        entry = next(s for s in snapshot["settings"] if s["name"] == "INDEX_ENGINE")

        assert entry == {
            "name": "INDEX_ENGINE",
            "env": ["CGR_INDEX_ENGINE"],
            "env_only": False,
            "type": "str",
            "constraints": {},
            "default": "auto",
        }

    def test_the_configuration_page_has_a_row(self) -> None:
        page = (REPO_ROOT / "docs" / "getting-started" / "configuration.md").read_text(
            encoding="utf-8"
        )

        row = next(
            line
            for line in page.splitlines()
            if line.startswith("| `CGR_INDEX_ENGINE` | `auto` |")
        )

        for language in L:
            assert f"`{language}`" in row

    def test_the_env_example_has_a_commented_line(self) -> None:
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

        assert re.search(r"^# CGR_INDEX_ENGINE=auto$", text, re.MULTILINE)


def test_index_engine_error_names_the_refusal_or_none() -> None:
    assert index_engine.index_engine_error("python,go=rust") is None
    assert index_engine.index_engine_error("go=fortran") is not None
