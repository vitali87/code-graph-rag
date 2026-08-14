# The release workflow's "Generate news bullets" step once dropped a genuinely
# new feature (Runtime Call Tracing) from Latest News: its prompt told the model
# not to "repeat or rephrase" existing entries and to omit when in doubt, so it
# treated the new tracing feature as a rephrase of the existing Data-Flow
# Tracing entry. v0.0.639 shipped runtime tracing yet the README never mentioned
# it. Guard the prompt against regressing to that omission-biased wording.

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"
)


def _news_step_script() -> str:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["bump-version"]["steps"]
    news = next(step for step in steps if step.get("name") == "Generate news bullets")
    return news["run"]


def test_news_prompt_is_not_omission_biased():
    script = _news_step_script().lower()
    assert "repeat or rephrase" not in script
    assert "when in doubt, omit" not in script


def test_news_prompt_keeps_distinct_related_features():
    script = _news_step_script()
    assert "distinct new feature" in script
    assert "still a new entry" in script


def test_news_prompt_defines_a_top_three_selection_rule():
    # "Every distinct feature is eligible" must not contradict the three-item
    # cap: the prompt has to say how to choose when more than three qualify.
    script = _news_step_script()
    assert "three most significant" in script
