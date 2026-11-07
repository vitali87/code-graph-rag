"""Validation for `.github/labels.yml` (issue #1434).

The sync workflow only runs on push to main, so a malformed manifest fails
after merge rather than on the PR that broke it. These pin the checks that
move that failure onto the PR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_labels import (
    LabelError,
    main,
    validate_labels,
    validate_template_labels,
)

VALID = """
- name: bug
  color: d73a4a
  description: Something isn't working correctly

- name: claimed
  color: b60205
  description: An agent session is working this
"""


class TestAcceptsValid:
    def test_well_formed_manifest_passes(self) -> None:
        """A manifest meeting every rule raises nothing."""
        assert validate_labels(VALID) == 2

    def test_the_shipped_manifest_is_valid(self) -> None:
        """The real file must satisfy its own validator."""
        manifest = Path(__file__).parent.parent.parent / ".github" / "labels.yml"
        assert validate_labels(manifest.read_text(encoding="utf-8")) >= 1


class TestRejectsMalformed:
    def test_duplicate_name_is_rejected(self) -> None:
        """Two entries sharing a name make the sync order-dependent."""
        text = VALID + "\n- name: bug\n  color: 111111\n  description: dupe\n"
        with pytest.raises(LabelError, match="duplicate"):
            validate_labels(text)

    def test_uppercase_colour_is_rejected(self) -> None:
        """GitHub preserves case, but relying on that is undocumented."""
        with pytest.raises(LabelError, match="color"):
            validate_labels("- name: a\n  color: B60205\n  description: d\n")

    def test_hash_prefixed_colour_is_rejected(self) -> None:
        """The syncer wants six bare hex digits, not CSS notation."""
        with pytest.raises(LabelError, match="color"):
            validate_labels("- name: a\n  color: '#b60205'\n  description: d\n")

    def test_unquoted_hash_colour_explains_the_yaml_comment(self) -> None:
        """An unquoted '#rrggbb' parses as null, so say why rather than
        reporting the value as 'None'."""
        with pytest.raises(LabelError, match="YAML comment"):
            validate_labels("- name: a\n  color: #b60205\n  description: d\n")

    def test_short_colour_is_rejected(self) -> None:
        """Three-digit shorthand is not accepted by the API."""
        with pytest.raises(LabelError, match="color"):
            validate_labels("- name: a\n  color: fff\n  description: d\n")

    def test_missing_key_is_rejected(self) -> None:
        """Every entry needs name, color and description."""
        with pytest.raises(LabelError, match="description"):
            validate_labels("- name: a\n  color: b60205\n")

    def test_non_list_is_rejected(self) -> None:
        """The syncer iterates the document as a list of mappings."""
        with pytest.raises(LabelError, match="list"):
            validate_labels("name: a\ncolor: b60205\n")

    def test_invalid_yaml_is_rejected(self) -> None:
        """A parse failure must report as a label error, not a traceback."""
        with pytest.raises(LabelError, match="parse"):
            validate_labels("- name: [unclosed\n")

    def test_case_differing_duplicate_is_rejected(self) -> None:
        """GitHub matches label names case-insensitively (GET /labels/BUG
        returns `bug`), but the syncer keys a case-sensitive map on the exact
        name. Two such entries become two sync operations racing over one
        real label, so the outcome depends on goroutine ordering."""
        text = (
            "- name: Bug\n  color: b60205\n  description: d\n"
            "- name: bug\n  color: c60205\n  description: e\n"
        )
        with pytest.raises(LabelError, match="differing only in case"):
            validate_labels(text)

    def test_trailing_newline_colour_is_rejected(self) -> None:
        """`re.match` stops at `$`, which sits before a final newline, so a
        quoted "b60205\\n" would pass an anchored-looking pattern."""
        with pytest.raises(LabelError, match="color"):
            validate_labels('- name: a\n  color: "b60205\\n"\n  description: d\n')

    def test_non_ascii_case_collision_is_rejected(self) -> None:
        """`str.lower()` leaves 'Straße' and 'STRASSE' distinct; casefold is
        the comparison built for caseless matching."""
        text = (
            "- name: Straße\n  color: b60205\n  description: d\n"
            "- name: STRASSE\n  color: c60205\n  description: e\n"
        )
        with pytest.raises(LabelError, match="differing only in case"):
            validate_labels(text)

    def test_control_character_in_name_is_rejected(self) -> None:
        """A newline inside a name is invisible in review and makes the label
        ambiguous with a two-label list wherever names are passed as text."""
        with pytest.raises(LabelError, match="control character"):
            validate_labels('- name: "a\\nb"\n  color: b60205\n  description: d\n')

    def test_padded_name_is_rejected(self) -> None:
        """Surrounding whitespace survives into the label name and is
        invisible in review, so `--add-label` would then miss it."""
        with pytest.raises(LabelError, match="whitespace"):
            validate_labels("- name: '  a  '\n  color: b60205\n  description: d\n")

    def test_empty_list_is_rejected(self) -> None:
        """`[]` parses fine but would leave the sync managing no labels, so a
        PR could silently empty the configuration."""
        with pytest.raises(LabelError, match="no labels"):
            validate_labels("[]\n")

    def test_null_name_is_rejected(self) -> None:
        """The syncer consumes name as a string; null would become 'None'."""
        with pytest.raises(LabelError, match="name"):
            validate_labels("- name: null\n  color: b60205\n  description: d\n")

    def test_numeric_name_is_rejected(self) -> None:
        """An unquoted numeric name must not be coerced into a string."""
        with pytest.raises(LabelError, match="name"):
            validate_labels("- name: 123\n  color: b60205\n  description: d\n")

    def test_null_description_is_rejected(self) -> None:
        """Present-but-null is not a usable description."""
        with pytest.raises(LabelError, match="description"):
            validate_labels("- name: a\n  color: b60205\n  description: null\n")

    def test_collection_description_is_rejected(self) -> None:
        """A list where a string belongs would reach the API malformed."""
        with pytest.raises(LabelError, match="description"):
            validate_labels("- name: a\n  color: b60205\n  description: [a, b]\n")

    def test_unquoted_numeric_colour_explains_the_quoting(self) -> None:
        """`color: 000000` parses as the integer 0, so say why rather than
        reporting a value the file does not visibly contain."""
        with pytest.raises(LabelError, match="quote"):
            validate_labels("- name: a\n  color: 000000\n  description: d\n")

    def test_empty_manifest_is_rejected(self) -> None:
        """An empty file would silently sync nothing."""
        with pytest.raises(LabelError, match="empty"):
            validate_labels("\n")


class TestTemplateLabels:
    """GitHub applies an issue form's labels only when they already exist,
    and drops the others without an error, so every label a template names
    must be one the manifest creates."""

    def test_defined_labels_pass(self) -> None:
        """Each label a form applies counts as one checked reference."""
        templates = {"bug.yml": "name: Bug\nlabels:\n  - bug\n  - needs triage\n"}
        assert validate_template_labels(templates, ["bug", "needs triage"]) == 2

    def test_label_matching_ignores_case(self) -> None:
        """GitHub matches label names case-insensitively."""
        templates = {"bug.yml": "name: Bug\nlabels:\n  - Bug\n"}
        assert validate_template_labels(templates, ["bug"]) == 1

    def test_form_without_labels_passes(self) -> None:
        """config.yml sits beside the forms and applies no labels."""
        templates = {"config.yml": "blank_issues_enabled: false\n"}
        assert validate_template_labels(templates, ["bug"]) == 0

    def test_undefined_label_is_rejected(self) -> None:
        """A name the manifest only contains as part of a longer label is
        still undefined: `triage` is not `needs triage`."""
        templates = {"bug.yml": "name: Bug\nlabels:\n  - bug\n  - triage\n"}
        with pytest.raises(LabelError, match=r"bug\.yml.*'triage'"):
            validate_template_labels(templates, ["bug", "needs triage"])

    def test_undefined_label_in_a_comma_list_is_rejected(self) -> None:
        """Issue forms also accept labels as one comma-separated string."""
        templates = {"bug.yml": "name: Bug\nlabels: bug, triage\n"}
        with pytest.raises(LabelError, match="'triage'"):
            validate_template_labels(templates, ["bug"])

    def test_non_string_label_is_rejected(self) -> None:
        """An unquoted number is not a label name GitHub can match."""
        templates = {"bug.yml": "name: Bug\nlabels:\n  - 123\n"}
        with pytest.raises(LabelError, match="labels"):
            validate_template_labels(templates, ["123"])

    def test_main_checks_the_shipped_templates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CI entry point checks the real forms, and says so: a pass
        that never read a template would print nothing about them."""
        assert main() == 0
        assert "ISSUE_TEMPLATE" in capsys.readouterr().out
