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

Qdrant is the default vector store. Where the vectors go is decided in this
order:

1. `QDRANT_URL` set: that Qdrant server.
2. `QDRANT_DB_PATH` set: an embedded, file-based Qdrant in that folder. Set
   means named in the environment, `.env` or code, even with the default
   value `./.qdrant_code_embeddings`.
3. Neither set, and the stack `cgr daemon up` starts is running: its Qdrant,
   on the address and port `docker compose config` resolves for it (default
   `127.0.0.1:6333`; `CGR_STACK_BIND_HOST`, `QDRANT_HTTP_PORT`, the `.env`
   beside the compose file, `COMPOSE_ENV_FILES` and edits to the compose file
   all count). It is used only while `docker compose ps` reports the stack's
   own Qdrant container running and that address answers as Qdrant, so a
   stopped stack never hands the embeddings to another process on its port.
   No API key is sent to it; a stack Qdrant that requires one is left alone
   with a warning.
4. Otherwise: an embedded Qdrant in `./.qdrant_code_embeddings`, relative to
   the directory cgr runs in. The log says why the stack's Qdrant was not
   used.

The embedding cache (`.embedding_cache.json`) sits in the embedded store's
folder, and moves to the stack's folder (`CGR_HOME`, default `~/.cgr`) along
with the vectors in case 3, so nothing is written into the indexed repository.
Its keys carry the embedding model, so one cache serves every project.

For a server that requires an API key
(Qdrant Cloud, or a self-hosted server started with `QDRANT__SERVICE__API_KEY`),
also set `QDRANT_API_KEY`, over an `https://` URL:

```bash
export QDRANT_URL="https://your-cluster.cloud.qdrant.io:6333"
export QDRANT_API_KEY="your-qdrant-api-key"
```

The key is refused over a plain `http://` URL, where it would travel
unencrypted. If that connection is protected another way, for example it never
leaves the machine, set `QDRANT_ALLOW_INSECURE_API_KEY=true`.

To use Milvus Lite for semantic vectors,
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
