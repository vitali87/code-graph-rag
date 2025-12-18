# ┌────────────────────────────────────────────────────────────────────────┐
# │ UniXcoder Model Singleton via LRU Cache                              │
# ├────────────────────────────────────────────────────────────────────────┤
# │ get_model() provides:                                                 │
# │   - Singleton behavior without global variables                       │
# │   - Thread-safe lazy initialization                                   │
# │   - Easy testability with cache_clear() method                        │
# │   - Memory efficient with maxsize=1                                   │
# └────────────────────────────────────────────────────────────────────────┘
from functools import lru_cache

from .constants import DEFAULT_MAX_LENGTH, SEMANTIC_EXTRA_ERROR, UNIXCODER_MODEL
from .utils.dependencies import has_torch, has_transformers

if has_torch() and has_transformers():
    import numpy as np
    import torch
    from numpy.typing import NDArray

    from .unixcoder import UniXcoder

    @lru_cache(maxsize=1)
    def get_model() -> UniXcoder:
        model = UniXcoder(UNIXCODER_MODEL)
        model.eval()
        if torch.cuda.is_available():
            model = model.cuda()
        return model

    def embed_code(code: str, max_length: int = DEFAULT_MAX_LENGTH) -> list[float]:
        model = get_model()
        device = next(model.parameters()).device
        tokens = model.tokenize([code], max_length=max_length)
        tokens_tensor = torch.tensor(tokens).to(device)
        with torch.no_grad():
            _, sentence_embeddings = model(tokens_tensor)
            embedding: NDArray[np.float32] = sentence_embeddings.cpu().numpy()
        result: list[float] = embedding[0].tolist()
        return result

else:

    def embed_code(code: str, max_length: int = DEFAULT_MAX_LENGTH) -> list[float]:
        raise RuntimeError(SEMANTIC_EXTRA_ERROR)
