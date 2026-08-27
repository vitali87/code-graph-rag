# A mis-keyed provider must not silently disable its model-id validation
# (issue #1504).
#
# `validate_model_id` enforces only where the catalogue is non-empty, which
# is correct: Ollama, Azure, LiteLLM and MiniMax have open model spaces and
# gating them would reject working configurations.
#
# But an empty set carries two meanings the code cannot tell apart:
#
#   - this provider genuinely has no enumerable catalogue  -> do not gate
#   - this provider is mis-keyed, so the lookup missed      -> DO NOT GATE,
#     silently, which is the hole
#
# Anthropic is absent from PROVIDER_CATALOGUE_PREFIXES and works by falling
# through to `(provider.lower(),)`. Add an entry for it with a wrong prefix
# -- a reasonable edit, since OpenAI and Google are both in that map -- and
# validation disables for Anthropic with no error and no failing test. The
# `opus-5` typo from #1492 would be accepted again.
from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.providers.base import _known_model_ids

# Providers whose catalogue pydantic-ai ships, so `validate_model_id` gates
# them. Each must keep resolving to a non-empty set: an empty one means the
# lookup missed and the check has silently stopped running.
_ENUMERABLE = (
    cs.Provider.ANTHROPIC,
    cs.Provider.OPENAI,
    cs.Provider.GOOGLE,
)

# Providers with an open model space -- whatever the user has pulled or
# deployed -- which must NOT be gated.
_OPEN_SPACE = (
    cs.Provider.OLLAMA,
    cs.Provider.AZURE,
    cs.Provider.LITELLM_PROXY,
    cs.Provider.MINIMAX,
)


@pytest.mark.parametrize("provider", _ENUMERABLE)
def test_an_enumerable_provider_still_resolves_to_a_catalogue(
    provider: str,
) -> None:
    """An empty set here means validation is off for this provider.

    This is the failure the function's own docstring describes -- "empty
    for any provider whose catalogue pydantic-ai does not ship, which is
    how `validate_model_id` decides to stay silent" -- reached by a
    mis-key rather than by a genuinely open model space.
    """
    assert _known_model_ids(provider), (
        f"{provider} resolves to an empty catalogue, so model-id validation "
        "is silently disabled for it. Either the PROVIDER_CATALOGUE_PREFIXES "
        "entry is wrong, or pydantic-ai stopped shipping its models."
    )


@pytest.mark.parametrize("provider", _OPEN_SPACE)
def test_an_open_space_provider_is_not_gated(provider: str) -> None:
    """The control, and the reason the exemption exists at all.

    Without it, a "fix" that gated every provider would satisfy the test
    above while rejecting every legitimate Ollama, Azure, LiteLLM and
    MiniMax configuration -- the expensive error direction.
    """
    assert not _known_model_ids(provider)


def test_a_mis_keyed_prefix_is_what_this_guards_against() -> None:
    """The mechanism, demonstrated rather than described.

    Documents WHY the parametrised test above matters: a wrong prefix
    yields an empty set, which the caller reads as "no catalogue" and
    skips. Asserting it here means the guard's premise is checked rather
    than assumed, so nobody later reads the test as arbitrary.
    """
    assert _known_model_ids("anthropic-typo") == frozenset()


def test_the_reported_typo_is_still_rejected() -> None:
    """End to end: the defect this protects is #1492's `opus-5`.

    A guard on set sizes could pass while validation was broken for some
    other reason, so this asserts the behaviour the sizes exist to
    support.
    """
    from codebase_rag.config import ModelConfig
    from codebase_rag.providers.base import validate_model_id

    config = ModelConfig(provider=cs.Provider.ANTHROPIC, model_id="opus-5", api_key="k")

    with pytest.raises(ValueError):
        validate_model_id(config)
