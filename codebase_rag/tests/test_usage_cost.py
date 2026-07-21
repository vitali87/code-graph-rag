# Per-run USD cost pricing (issue #80). These are pure unit tests: they price a
# RunUsage against the bundled genai-prices data with no DB or LLM call.
from __future__ import annotations

from pydantic_ai.usage import RunUsage

from codebase_rag.services.usage_cost import price_run


def test_prices_known_proprietary_model() -> None:
    cost = price_run(
        RunUsage(input_tokens=1000, output_tokens=500),
        "anthropic",
        "anthropic:claude-sonnet-4-5",
    )
    assert cost is not None
    assert cost > 0


def test_strips_provider_prefix_from_model_id() -> None:
    usage = RunUsage(input_tokens=1000, output_tokens=0)
    with_prefix = price_run(usage, "openai", "openai:gpt-4o")
    without_prefix = price_run(usage, "openai", "gpt-4o")
    assert with_prefix is not None
    assert with_prefix == without_prefix


def test_unknown_local_model_returns_none() -> None:
    cost = price_run(
        RunUsage(input_tokens=1000, output_tokens=500),
        "ollama",
        "ollama:llama3",
    )
    assert cost is None


def test_mismatched_provider_falls_back_to_autodetect() -> None:
    # A provider string genai-prices does not know (e.g. a proxy) still prices
    # when the model id alone is recognizable.
    cost = price_run(
        RunUsage(input_tokens=1000, output_tokens=500),
        "litellm",
        "claude-sonnet-4-5",
    )
    assert cost is not None
    assert cost > 0
