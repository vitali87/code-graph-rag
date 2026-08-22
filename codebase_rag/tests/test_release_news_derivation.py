# The release workflow once generated Latest News with a second model call
# whose prompt included the previous NEWS.md entries as dedup context. The
# model anchored on those old entries and paraphrased them into fake "news":
# v0.0.720 re-announced Ruby support, structural search and replace, and
# data-flow tracing for C#/Java/C/Go, none of which shipped in that window,
# while the Highlights generated in the same run from the same PR titles were
# accurate. News is therefore derived from the Highlights fragment, with all
# filtering, marker normalisation, dedup, and the three-entry cap handled
# deterministically by scripts/update_news.py. Guard against reintroducing a
# dedicated news generation or feeding old entries back to the model.

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"
)


def _steps() -> list[dict[str, str]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["bump-version"]["steps"]


def _news_update_script() -> str:
    step = next(
        step
        for step in _steps()
        if step.get("name") == "Update NEWS.md and regenerate README"
    )
    return step["run"]


def test_no_dedicated_news_generation_step() -> None:
    # A second model call for news is the regression: its dedup context of old
    # NEWS entries is what the model paraphrased into fake news.
    names = [step.get("name", "") for step in _steps()]
    assert "Generate news bullets" not in names


def test_news_is_derived_from_highlights() -> None:
    assert "scripts/update_news.py /tmp/highlights.md" in _news_update_script()


def test_no_step_feeds_existing_news_entries_to_a_model() -> None:
    # Grepping NEWS.md entries into a prompt payload is how old entries reached
    # the model's context in the first place.
    for step in _steps():
        script = step.get("run", "")
        assert "known.md" not in script
        assert "newsbullets" not in script
