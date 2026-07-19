---
description: "Semantic code search with UniXcoder embeddings in Code-Graph-RAG."
---

# Semantic Search

Code-Graph-RAG supports intent-based code search using UniXcoder embeddings. Find functions by describing what they do rather than by exact names.

## Installation

Semantic search requires the `semantic` extra:

```bash
pip install 'code-graph-rag[semantic]'
```

Qdrant is the default vector store. To use Milvus Lite for semantic vectors,
install the `milvus` extra and set:

```bash
pip install 'code-graph-rag[semantic,milvus]'
export CGR_VECTOR_STORE_BACKEND=milvus
export MILVUS_URI="./.milvus_code_embeddings.db"
```

You can also point `MILVUS_URI` at a self-hosted open-source Milvus endpoint,
such as `http://localhost:19530`.

## OpenAI-Compatible Embedding Providers

By default embeddings are computed locally with UniXcoder (requires the
`semantic` extra's torch/transformers). Alternatively, any OpenAI-compatible
embeddings endpoint (OpenAI, Ollama, vLLM, LM Studio, and others) can compute
them server-side, so torch and transformers are not needed locally; only the
vector store dependency (`qdrant-client` or the `milvus` extra) is required:

```bash
pip install 'code-graph-rag' qdrant-client
export CGR_EMBEDDING_PROVIDER=openai
export OPENAI_EMBEDDING_BASE_URL="http://localhost:11434/v1"  # default: https://api.openai.com/v1
export OPENAI_EMBEDDING_MODEL="nomic-embed-text"              # default: text-embedding-3-small
export OPENAI_EMBEDDING_API_KEY="sk-..."                      # optional; falls back to OPENAI_API_KEY
```

Additional settings:

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_EMBEDDING_DIMENSIONS` | unset | Forwarded as the `dimensions` request parameter for models that support truncated output |
| `OPENAI_EMBEDDING_BATCH_SIZE` | `128` | Snippets per HTTP request |
| `OPENAI_EMBEDDING_TIMEOUT` | `60` | Request timeout in seconds |

The vector store dimension must match the embedding model's output. UniXcoder
produces 768-dimensional vectors (the default), while `text-embedding-3-small`
produces 1536; set `QDRANT_VECTOR_DIM` (or `MILVUS_VECTOR_DIM`) accordingly, or
use `OPENAI_EMBEDDING_DIMENSIONS` to request 768-dimensional output. Cached
embeddings are keyed per provider and model, so switching models never replays
vectors from another embedding space.

## Usage

### Generate Code Embeddings

```python
from cgr import embed_code

embedding = embed_code("def authenticate(user, password): ...")
print(f"Embedding dimension: {len(embedding)}")
```

### Search by Description

In the interactive CLI, you can search semantically:

- "error handling functions"
- "authentication code"
- "database connection setup"

The system returns potential matches with similarity scores.

## How It Works

UniXcoder is a unified cross-modal pre-trained model that supports both code understanding and generation. Code-Graph-RAG uses it to create embeddings that capture the semantic meaning of code, enabling searches based on what code does rather than what it's named.
