"""Validation for `.coderabbit.yaml` (issue #1823).

Nothing checked this file before: `check-yaml` in pre-commit covers syntax
only, and the vendor schema sets `additionalProperties: false` at the root
object alone, so a key misspelt under `reviews.auto_review` validates with
zero errors. Both settings the file exists for fail silently, so a test that
merely asserts the shipped file is valid would not discriminate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.validate_coderabbit_config import (
    ROOT_KEY_SCHEMA,
    VENDOR_ROOT_KEYS,
    ConfigError,
    validate_coderabbit_config,
)

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = REPO_ROOT / ".coderabbit.yaml"
# Note the 301: this URL needs redirect-following if fetched by hand.
SCHEMA_URL = "https://coderabbit.ai/integrations/schema.v2.json"

VALID = """
reviews:
  pre_merge_checks:
    docstrings:
      mode: "off"
  auto_review:
    base_branches:
      - "^feat/.*"
      - "^fix/.*"
"""


def shipped() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


class TestAcceptsValid:
    def test_well_formed_config_passes(self) -> None:
        """A config meeting every rule raises nothing."""
        assert validate_coderabbit_config(VALID) == 2

    def test_the_shipped_config_is_valid(self) -> None:
        """The real file must satisfy its own validator."""
        assert validate_coderabbit_config(shipped()) >= 1

    def test_extra_unknown_settings_are_not_rejected(self) -> None:
        """Without the vendor schema, unrecognised keys are not our business.

        The script guards two specific settings; failing on anything else
        would make every upstream feature addition a local build break.
        """
        assert validate_coderabbit_config(VALID + "\n  poem: false\n") == 2


class TestDocstringsMode:
    """`mode` must be the string 'off', not the YAML boolean (#1617)."""

    def test_unquoted_off_is_rejected_naming_the_cause(self) -> None:
        """Bare `off` is a YAML boolean, so the exemption stops applying."""
        text = VALID.replace('mode: "off"', "mode: off")
        with pytest.raises(ConfigError, match="boolean False"):
            validate_coderabbit_config(text)

    def test_unquoted_off_really_does_parse_as_false(self) -> None:
        """Pin the premise, so the test above cannot pass for another reason."""
        import yaml

        data = yaml.safe_load(VALID.replace('mode: "off"', "mode: off"))
        assert data["reviews"]["pre_merge_checks"]["docstrings"]["mode"] is False

    def test_a_non_string_mode_names_the_type(self) -> None:
        """A YAML typing accident gets its own message, not the value one.

        Without this the `isinstance` branch is unreachable under mutation:
        the value comparison below catches the same inputs, so deleting the
        type check leaves the suite green.
        """
        text = VALID.replace('mode: "off"', "mode: 3")
        with pytest.raises(ConfigError, match="got int"):
            validate_coderabbit_config(text)

    def test_a_null_mode_names_the_type(self) -> None:
        """`mode:` with nothing after it parses as None, not as absent."""
        text = VALID.replace('mode: "off"', "mode:")
        with pytest.raises(ConfigError, match="got NoneType"):
            validate_coderabbit_config(text)

    def test_a_different_mode_is_rejected(self) -> None:
        """Only 'off' clears the check; 'warning' silently restores #1617."""
        text = VALID.replace('mode: "off"', 'mode: "warning"')
        with pytest.raises(ConfigError, match="must be"):
            validate_coderabbit_config(text)

    def test_a_misspelt_docstrings_key_is_rejected(self) -> None:
        """The block is ignored rather than rejected, so it must be named."""
        text = VALID.replace("docstrings:", "docstringz:")
        with pytest.raises(ConfigError, match="no 'docstrings' key"):
            validate_coderabbit_config(text)

    def test_a_missing_pre_merge_checks_block_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="pre_merge_checks"):
            validate_coderabbit_config(
                "reviews:\n  auto_review:\n    base_branches: ['^feat/.*']\n"
            )


class TestBaseBranches:
    """`base_branches` drives whether stacked PRs get reviewed at all (#1581)."""

    def test_the_misspelt_key_from_the_issue_is_rejected(self) -> None:
        """`base_branchez` is the exact typo the vendor schema accepts."""
        text = VALID.replace("base_branches:", "base_branchez:")
        with pytest.raises(ConfigError, match="no 'base_branches' key"):
            validate_coderabbit_config(text)

    def test_an_empty_list_is_rejected(self) -> None:
        """Empty parses cleanly and reviews only the default branch."""
        text = VALID.replace('      - "^feat/.*"\n      - "^fix/.*"\n', "      []\n")
        with pytest.raises(ConfigError, match="empty"):
            validate_coderabbit_config(text)

    def test_a_scalar_instead_of_a_list_is_rejected(self) -> None:
        text = VALID.replace(
            '      - "^feat/.*"\n      - "^fix/.*"\n', '    base_branches: "^feat/.*"\n'
        ).replace("    base_branches:\n", "")
        with pytest.raises(ConfigError, match="must be a list"):
            validate_coderabbit_config(text)

    def test_an_uncompilable_regex_is_rejected(self) -> None:
        """CodeRabbit matches these as regexes; a broken one is dropped."""
        text = VALID.replace('"^feat/.*"', '"^feat/([.*"')
        with pytest.raises(ConfigError, match="invalid regex"):
            validate_coderabbit_config(text)

    def test_an_empty_pattern_string_is_rejected(self) -> None:
        text = VALID.replace('"^feat/.*"', '""')
        with pytest.raises(ConfigError, match="non-empty string"):
            validate_coderabbit_config(text)


class TestMalformedDocument:
    def test_empty_file_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="empty"):
            validate_coderabbit_config("")

    def test_a_list_document_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            validate_coderabbit_config("- a\n- b\n")

    def test_unparseable_yaml_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="could not parse"):
            validate_coderabbit_config("reviews:\n  - [unclosed\n")

    def test_a_scalar_where_a_mapping_belongs_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="expected a mapping"):
            validate_coderabbit_config("reviews: 3\n")


class TestSchemaAloneIsInsufficient:
    """The reason this script exists rather than a plain schema check.

    `additionalProperties: false` is set on the ROOT object only, so an
    unknown key nested under `reviews.auto_review` validates clean. These
    pin that, so if the vendor ever tightens the schema the redundancy is
    reported rather than assumed.
    """

    @staticmethod
    def _schema() -> Any:
        """The vendor schema, reduced to the structure these tests assert.

        Deliberately not the real 93KB document: vendoring it would freeze a
        copy that drifts silently, and fetching it would make the suite need
        the network. What is reproduced here is the shape the script depends
        on -- `additionalProperties: false` at the root and absent below it --
        which is the property under test. `test_the_real_schema_is_still_root_only`
        checks the live document when it is reachable.
        """
        pytest.importorskip("jsonschema")
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "reviews": {
                    "type": "object",
                    "properties": {
                        "pre_merge_checks": {
                            "type": "object",
                            "properties": {
                                "docstrings": {
                                    "type": "object",
                                    "properties": {"mode": {"type": "string"}},
                                }
                            },
                        },
                        "auto_review": {
                            "type": "object",
                            "properties": {
                                "base_branches": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                }
                            },
                        },
                    },
                }
            },
        }

    def test_additional_properties_is_root_only(self) -> None:
        """The structural fact the whole script rests on."""
        schema = self._schema()
        assert schema.get("additionalProperties") is False
        auto_review = schema["properties"]["reviews"]["properties"]["auto_review"]
        assert "additionalProperties" not in auto_review

    @pytest.mark.slow
    def test_the_real_schema_is_still_root_only(self) -> None:
        """Guard the reduced fixture above against vendor drift.

        Skipped without network. If this ever fails, the vendor has tightened
        the schema and the key checks may have become redundant.
        """
        pytest.importorskip("jsonschema")
        urlopen = pytest.importorskip("urllib.request").urlopen
        try:
            with urlopen(SCHEMA_URL, timeout=10) as response:
                live = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            # JSONDecodeError subclasses ValueError, not OSError: a proxy or
            # captive portal returning HTML must skip, not redden the build.
            pytest.skip(f"vendor schema unreachable: {exc}")
        assert live.get("additionalProperties") is False
        auto_review = live["properties"]["reviews"]["properties"]["auto_review"]
        assert "additionalProperties" not in auto_review
        assert set(live["properties"]) == set(VENDOR_ROOT_KEYS), (
            "the vendor's root key set has changed; update VENDOR_ROOT_KEYS "
            "or the shipped check will reject a newly valid setting"
        )

    def test_the_typo_passes_the_vendor_schema(self) -> None:
        """A schema check alone would report `base_branchez` as valid."""
        import yaml
        from jsonschema import Draft202012Validator

        broken = yaml.safe_load(shipped().replace("base_branches:", "base_branchez:"))
        errors = list(Draft202012Validator(self._schema()).iter_errors(broken))
        assert errors == [], (
            "the vendor schema now rejects the typo; if that is permanent, "
            "this script's key checks are redundant and should be revisited"
        )

    def test_the_validator_catches_what_the_schema_misses(self) -> None:
        """The discriminating assertion: same input, opposite verdicts."""
        # Both built before the raises block, so only the call under test can
        # throw: otherwise a failure in the fixture would satisfy the
        # assertion and the test would pass for the wrong reason.
        schema = self._schema()
        text = shipped().replace("base_branches:", "base_branchez:")
        with pytest.raises(ConfigError):
            validate_coderabbit_config(text, schema=schema)

    def test_the_shipped_config_passes_the_vendor_schema_too(self) -> None:
        assert validate_coderabbit_config(shipped(), schema=self._schema()) >= 1

    def test_a_schema_violation_is_reported_when_a_schema_is_given(self) -> None:
        """An unknown ROOT key is what the schema does catch."""
        schema = self._schema()
        text = VALID + "\nnonsense_root_key: 1\n"
        with pytest.raises(ConfigError, match="schema violation"):
            validate_coderabbit_config(text, schema)


class TestWiring:
    """The script must actually run, or it is a check that never fires."""

    def test_pre_commit_runs_it_on_the_config(self) -> None:
        text = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        assert "scripts/validate_coderabbit_config.py" in text
        assert re.search(r"files:\s*\^\\\.coderabbit\\\.yaml\$", text), (
            "the hook must be scoped to .coderabbit.yaml"
        )

    def test_ci_runs_it(self) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        assert "scripts/validate_coderabbit_config.py" in text


class TestShippedEntrypointEnforcesTheSchema:
    """The schema check must run in pre-commit and CI, not only in tests.

    It was reachable only from tests in the first cut of this script: `main()`
    passed no schema, so an unknown root key that CodeRabbit itself rejects
    went through the hook and CI clean. A check that exists but is never
    called is the same defect as a check that cannot fail.
    """

    def test_an_unknown_root_key_is_rejected_by_the_shipped_schema(self) -> None:
        with pytest.raises(ConfigError, match="schema violation"):
            validate_coderabbit_config(
                VALID + "\nnonsense_root_key: 1\n", schema=ROOT_KEY_SCHEMA
            )

    # Written out rather than derived from VENDOR_ROOT_KEYS on purpose. A test
    # that iterates the same tuple it is checking cannot see a key being
    # dropped from it: the deletion removes the case as well as the schema
    # entry, and the loop stays green over a shorter list. Measured -- deleting
    # `knowledge_base` from the shipped set left the derived version passing.
    EXPECTED_ROOT_KEYS = (
        "chat",
        "code_generation",
        "early_access",
        "enable_free_tier",
        "inheritance",
        "issue_enrichment",
        "knowledge_base",
        "language",
        "reviews",
        "tone_instructions",
    )

    def test_the_shipped_root_key_set_is_complete(self) -> None:
        """An independent copy, so dropping a key from the real set reddens."""
        assert set(VENDOR_ROOT_KEYS) == set(self.EXPECTED_ROOT_KEYS)

    @pytest.mark.parametrize("key", [k for k in EXPECTED_ROOT_KEYS if k != "reviews"])
    def test_every_known_root_key_is_accepted(self, key: str) -> None:
        """The check must not reject a setting the vendor allows."""
        assert (
            validate_coderabbit_config(f"{VALID}\n{key}: {{}}\n", ROOT_KEY_SCHEMA) == 2
        ), f"root key {key!r} should be accepted"

    def test_main_passes_a_schema(self) -> None:
        """Pin the wiring: the entrypoint must supply the schema."""
        source = (REPO_ROOT / "scripts" / "validate_coderabbit_config.py").read_text(
            encoding="utf-8"
        )
        assert "schema=ROOT_KEY_SCHEMA" in source

    def test_the_shipped_config_passes_the_shipped_schema(self) -> None:
        assert validate_coderabbit_config(shipped(), ROOT_KEY_SCHEMA) >= 1
