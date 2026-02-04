import json
from pathlib import Path
from typing import TypedDict

from loguru import logger

from .. import exceptions as ex
from .. import logs


class ClaudeSettings(TypedDict, total=False):
    """Claude Code settings structure."""

    env: dict[str, str]
    alwaysThinkingEnabled: bool


def get_claude_settings_path() -> Path:
    """Get path to Claude Code settings file."""
    return Path.home() / ".claude" / "settings.json"


def read_claude_settings() -> ClaudeSettings | None:
    """Read Claude Code settings from ~/.claude/settings.json.

    Returns:
        Dict with settings if file exists and is valid JSON, None otherwise.
    """
    settings_path = get_claude_settings_path()

    if not settings_path.exists():
        return None

    try:
        with open(settings_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def parse_custom_headers(headers_str: str | None) -> dict[str, str]:
    if headers_str is None:
        return {}
    if not headers_str.strip():
        logger.info(logs.CUSTOM_HEADERS_EMPTY)
        return {}
    result: dict[str, str] = {}
    for line in headers_str.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(ex.ANTHROPIC_MALFORMED_HEADER.format(line=line))
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def get_anthropic_config_from_claude_settings() -> tuple[str | None, dict[str, str]]:
    """Extract Anthropic configuration from Claude Code settings.

    Returns:
        Tuple of (base_url, custom_headers_dict).
        Both can be None if settings don't exist or don't contain Anthropic config.
    """
    settings = read_claude_settings()

    if not settings or "env" not in settings:
        return None, {}

    env = settings["env"]
    base_url = env.get("ANTHROPIC_BASE_URL")

    custom_headers_str = env.get("ANTHROPIC_CUSTOM_HEADERS")
    custom_headers = parse_custom_headers(custom_headers_str)

    return base_url, custom_headers
