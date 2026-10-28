# LiteLLM registers here rather than at the bottom of base.py: litellm.py
# imports base, so registering from base made `import providers.litellm`
# fail on a partially initialised module. Any import of either module runs
# this package first, which loads base completely before litellm needs it.
import importlib.util

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from .base import PROVIDER_REGISTRY


def _litellm_importable() -> bool:
    # `LiteLLMProvider` imports pydantic-ai's LiteLLM support only when it
    # builds a model, so importing it cannot tell whether that support exists.
    # Ask the import system instead, without loading the module.
    try:
        return importlib.util.find_spec(cs.PYDANTIC_AI_LITELLM_MODULE) is not None
    except ImportError:
        return False


if _litellm_importable():
    from .litellm import LiteLLMProvider

    PROVIDER_REGISTRY[cs.Provider.LITELLM_PROXY] = LiteLLMProvider
else:
    logger.debug(ls.LITELLM_PROVIDER_UNAVAILABLE)
