"""Issue #2409: every image of the packaged stack is pinned by version and digest.

The compose file pinned Memgraph by digest, with a comment on why a floating
tag is dangerous, but ran `qdrant/qdrant` and `memgraph/lab` untagged, i.e. at
whatever `:latest` was on the day of the first pull. A new Qdrant major then
reached users with no test run against it, two machines on one cgr version
ran different servers, and resolving `:latest` needed a Docker Hub manifest
request that the pull rate limit refused while the pinned Memgraph pulled.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from loguru import logger

from codebase_rag.stack import constants as stack_cs
from codebase_rag.stack.manager import StackManager

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "codebase_rag" / "docker-compose.yaml"
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"
PINNED = re.compile(r"^(?P<name>[\w./-]+):(?P<tag>[\w.-]+)@sha256:[0-9a-f]{64}$")
MEMGRAPH_PIN = (
    "memgraph/memgraph-mage:3.7@sha256:"
    "91ab47cfee0eb0fa87d04bbd5b0352ef46374eef7dc4dbe676dbeec7d2facc72"
)


def _images(compose_file: Path) -> dict[str, str]:
    compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    return {name: spec["image"] for name, spec in compose["services"].items()}


@pytest.mark.parametrize(
    "service",
    [stack_cs.SERVICE_MEMGRAPH, stack_cs.SERVICE_QDRANT, stack_cs.SERVICE_LAB],
)
def test_every_stack_image_is_pinned_by_version_and_digest(service: str) -> None:
    image = _images(COMPOSE_PATH)[service]

    match = PINNED.match(image)
    assert match, image
    assert match.group("tag") != "latest", image


def test_the_memgraph_pin_is_unchanged() -> None:
    # Negative: engine upgrades change accepted Cypher (issue #1257); this
    # change pins the other two and must not move Memgraph.
    assert _images(COMPOSE_PATH)[stack_cs.SERVICE_MEMGRAPH] == MEMGRAPH_PIN


def test_dependabot_bumps_the_stack_images_deliberately() -> None:
    config = yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))
    watched = [
        update
        for update in config["updates"]
        if update["package-ecosystem"] == "docker-compose"
    ]

    assert any(
        "/codebase_rag" in [update.get("directory"), *update.get("directories", [])]
        for update in watched
    ), watched


@pytest.fixture
def stack_home(tmp_path: Path) -> Path:
    home = tmp_path / "cgr-home"
    home.mkdir()
    return home


def _warnings_for(stack_home: Path) -> list[str]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        StackManager(
            home=stack_home, package_compose=COMPOSE_PATH
        ).ensure_compose_file()
    finally:
        logger.remove(sink)
    return messages


def test_a_rendered_file_with_floating_images_is_told_the_pins(
    stack_home: Path,
) -> None:
    # The file is rendered once and never overwritten, so an existing install
    # keeps its floating tags; it is told which pinned images to use.
    rendered = COMPOSE_PATH.read_text(encoding="utf-8")
    for service in (stack_cs.SERVICE_QDRANT, stack_cs.SERVICE_LAB):
        rendered = rendered.replace(
            _images(COMPOSE_PATH)[service], _images(COMPOSE_PATH)[service].split(":")[0]
        )
    (stack_home / stack_cs.COMPOSE_FILENAME).write_text(rendered, encoding="utf-8")

    warnings = _warnings_for(stack_home)

    floating = [w for w in warnings if "qdrant/qdrant" in w and "memgraph/lab" in w]
    assert floating, warnings
    assert _images(COMPOSE_PATH)[stack_cs.SERVICE_QDRANT] in floating[0]


def test_a_freshly_rendered_file_warns_about_nothing(stack_home: Path) -> None:
    # Negative.
    _warnings_for(stack_home)

    assert _warnings_for(stack_home) == []


def test_a_user_pin_of_their_own_is_left_alone(stack_home: Path) -> None:
    # Negative: a digest the user chose (a mirror, another version) is a pin.
    rendered = COMPOSE_PATH.read_text(encoding="utf-8").replace(
        _images(COMPOSE_PATH)[stack_cs.SERVICE_QDRANT],
        "mirror.example/qdrant:v1.18.0@sha256:" + "a" * 64,
    )
    (stack_home / stack_cs.COMPOSE_FILENAME).write_text(rendered, encoding="utf-8")

    assert _warnings_for(stack_home) == []
