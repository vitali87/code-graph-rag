import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from codebase_rag.utils.claude_settings import (
    get_anthropic_config_from_claude_settings,
    get_claude_settings_path,
    read_claude_settings,
)


def test_get_claude_settings_path():
    """Test getting Claude Code settings path."""
    path = get_claude_settings_path()
    assert path == Path.home() / ".claude" / "settings.json"


def test_read_claude_settings_not_exists():
    """Test reading settings when file doesn't exist."""
    with patch(
        "codebase_rag.utils.claude_settings.get_claude_settings_path"
    ) as mock_path:
        mock_path.return_value = Path("/nonexistent/settings.json")
        result = read_claude_settings()
        assert result is None


def test_read_claude_settings_valid():
    """Test reading valid Claude Code settings."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        settings = {
            "env": {
                "ANTHROPIC_BASE_URL": "https://proxy.example.com",
                "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: test\nx-config: value",
            },
            "alwaysThinkingEnabled": True,
        }
        json.dump(settings, f)
        temp_path = Path(f.name)

    try:
        with patch(
            "codebase_rag.utils.claude_settings.get_claude_settings_path"
        ) as mock_path:
            mock_path.return_value = temp_path
            result = read_claude_settings()
            assert result is not None
            assert result["env"]["ANTHROPIC_BASE_URL"] == "https://proxy.example.com"
    finally:
        temp_path.unlink()


def test_get_anthropic_config_no_settings():
    """Test extracting Anthropic config when settings don't exist."""
    with patch("codebase_rag.utils.claude_settings.read_claude_settings") as mock_read:
        mock_read.return_value = None
        base_url, headers = get_anthropic_config_from_claude_settings()
        assert base_url is None
        assert headers == {}


def test_get_anthropic_config_with_headers():
    """Test extracting Anthropic config with custom headers."""
    with patch("codebase_rag.utils.claude_settings.read_claude_settings") as mock_read:
        mock_read.return_value = {
            "env": {
                "ANTHROPIC_BASE_URL": "https://portkey.example.com",
                "ANTHROPIC_CUSTOM_HEADERS": "x-portkey-api-key: pk-test123\nx-portkey-config: pc-config456",
            }
        }
        base_url, headers = get_anthropic_config_from_claude_settings()
        assert base_url == "https://portkey.example.com"
        assert headers == {
            "x-portkey-api-key": "pk-test123",
            "x-portkey-config": "pc-config456",
        }


def test_get_anthropic_config_empty_headers():
    """Test extracting config with empty custom headers."""
    with patch("codebase_rag.utils.claude_settings.read_claude_settings") as mock_read:
        mock_read.return_value = {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com/v1",
            }
        }
        base_url, headers = get_anthropic_config_from_claude_settings()
        assert base_url == "https://api.anthropic.com/v1"
        assert headers == {}


def test_get_anthropic_config_no_env():
    """Test extracting config when env section is missing."""
    with patch("codebase_rag.utils.claude_settings.read_claude_settings") as mock_read:
        mock_read.return_value = {"alwaysThinkingEnabled": True}
        base_url, headers = get_anthropic_config_from_claude_settings()
        assert base_url is None
        assert headers == {}
