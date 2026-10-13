"""The JVM tracing guide's Maven recipe reaches the forked test JVM.

Surefire runs the tests in a forked JVM (`forkCount=1` by default), which
takes its flags from `argLine`. `MAVEN_OPTS` only sets the JVM running Maven
itself, so the guide's `MAVEN_OPTS='-javaagent:...' mvn test` attached the
agent to Maven and traced no test method (issue #2876).
"""

from __future__ import annotations

from pathlib import Path

_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "guide" / "dynamic-tracing.md"


def _jvm_recipe() -> str:
    """The attach recipe and the prose under it, up to the section's demo."""
    text = _GUIDE.read_text(encoding="utf-8")
    start = text.index("Attach it to any JVM workload")
    return text[start : text.index("\n![", start)]


def test_maven_passes_the_agent_through_surefire_arg_line() -> None:
    recipe = _jvm_recipe()
    assert "mvn test -DargLine='-javaagent:" in recipe, recipe
    # The options carry a `;`, which the quotes keep from ending the command.
    assert "include=com.example;repo=" in recipe.split("-DargLine='", 1)[1]


def test_maven_opts_is_not_offered_for_a_forked_test_run() -> None:
    recipe = _jvm_recipe()
    assert "MAVEN_OPTS='-javaagent" not in recipe, recipe
    assert "forkCount=0" in recipe, recipe


def test_a_pom_that_already_sets_arg_line_is_covered() -> None:
    # JaCoCo and others set argLine; replacing it would drop their agent.
    assert "@{argLine}" in _jvm_recipe()


def test_the_gradle_line_is_unchanged() -> None:
    assert "test { jvmArgs" in _jvm_recipe()
