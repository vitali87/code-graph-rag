# Per-run USD cost from token usage (issue #80). Prices a run's RunUsage with
# the genai-prices data bundled via pydantic-ai, so no price table is hand
# maintained here. Local or unknown models have no public price, so pricing
# returns None and callers show token counts without a cost, which keeps cost
# reporting to proprietary models only.
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

    from pydantic_ai.usage import RunUsage


def price_run(usage: RunUsage, provider: str, model_id: str) -> Decimal | None:
    """USD cost for one run, or None when the model has no known public price.

    ``model_id`` may carry a ``provider:model`` prefix; only the model part is
    priced. The given provider is tried first, then genai-prices auto-detection
    from the model id, so a proxy provider name (e.g. litellm) still prices a
    recognizable model.
    """
    # Outside the try: a missing package is a fault to see, not an unpriced
    # model.
    from genai_prices import calc_price

    model_ref = model_id.split(":", 1)[-1]
    for provider_id in (provider or None, None):
        try:
            return calc_price(usage, model_ref, provider_id=provider_id).total_price
        except (LookupError, ValueError):
            # An unknown provider or model (LookupError), or usage the
            # table cannot price (ValueError): pricing is display-only, so
            # the next provider is tried and the run is left unpriced.
            continue
    return None
