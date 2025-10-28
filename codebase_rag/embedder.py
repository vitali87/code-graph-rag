# codebase_rag/embedder.py
import functools
from typing import List
from .utils.dependencies import has_torch, has_transformers

if has_torch() and has_transformers():
    from .unixcoder import UniXcoder
    import torch

    @functools.lru_cache(maxsize=1)
    def get_model():
        """Get or create UniXcoder model instance with singleton pattern via LRU cache.
        
        This approach provides:
        - Singleton behavior without global variables
        - Thread-safe lazy initialization
        - Easy testability with cache_clear() method
        - Memory efficient with maxsize=1
        
        Returns:
            UniXcoder model instance configured for inference
        """
        model = UniXcoder("microsoft/unixcoder-base")
        model.eval()
        if torch.cuda.is_available():
            model = model.cuda()
        return model

    def embed_code(code: str, max_length: int = 512) -> List[float]:
        """Generate code embedding using UniXcoder.
        
        Args:
            code: Source code to embed
            max_length: Maximum token length for input
            
        Returns:
            768-dimensional embedding as list of floats
        """
        model = get_model()
        device = next(model.parameters()).device
        tokens = model.tokenize([code], max_length=max_length)
        tokens_tensor = torch.tensor(tokens).to(device)
        with torch.no_grad():
            # Forward returns (token_embeddings, sentence_embeddings)
            _, sentence_embeddings = model(tokens_tensor)
            embedding = sentence_embeddings.cpu().numpy()
        return embedding[0].tolist()  # (768,) list

else:
    def embed_code(code: str, max_length: int = 512) -> List[float]:
        raise RuntimeError(
            "Semantic search requires 'semantic' extra: uv sync --extra semantic"
        )