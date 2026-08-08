---
description: "Configure Code-Graph-RAG with provider settings, environment variables, and model options."
---

# Configuration

Configuration is managed through environment variables in the `.env` file. The provider-explicit configuration supports mixing different providers for orchestrator and cypher models.

## Provider Examples

### All Ollama (Local Models)

```bash
ORCHESTRATOR_PROVIDER=ollama
ORCHESTRATOR_MODEL=qwen2.5-coder
ORCHESTRATOR_ENDPOINT=http://localhost:11434/v1

CYPHER_PROVIDER=ollama
CYPHER_MODEL=qwen2.5-coder
CYPHER_ENDPOINT=http://localhost:11434/v1
```

### All OpenAI Models

```bash
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-5.6-terra
ORCHESTRATOR_API_KEY=sk-your-openai-key

CYPHER_PROVIDER=openai
CYPHER_MODEL=gpt-5.6-luna
CYPHER_API_KEY=sk-your-openai-key
```

### All Google Models

```bash
ORCHESTRATOR_PROVIDER=google
ORCHESTRATOR_MODEL=gemini-3.6-flash
ORCHESTRATOR_API_KEY=your-google-api-key

CYPHER_PROVIDER=google
CYPHER_MODEL=gemini-3.5-flash-lite
CYPHER_API_KEY=your-google-api-key
```

Get your Google API key from [Google AI Studio](https://aistudio.google.com/app/apikey).

### Mixed Providers

```bash
ORCHESTRATOR_PROVIDER=google
ORCHESTRATOR_MODEL=gemini-3.6-flash
ORCHESTRATOR_API_KEY=your-google-api-key

CYPHER_PROVIDER=ollama
CYPHER_MODEL=qwen2.5-coder
CYPHER_ENDPOINT=http://localhost:11434/v1
```

### MiniMax Models

```bash
ORCHESTRATOR_PROVIDER=minimax
ORCHESTRATOR_MODEL=MiniMax-M3
ORCHESTRATOR_API_KEY=your-minimax-api-key
ORCHESTRATOR_ENDPOINT=https://api.minimax.io/v1

CYPHER_PROVIDER=minimax
CYPHER_MODEL=MiniMax-M2.7
CYPHER_API_KEY=your-minimax-api-key
CYPHER_ENDPOINT=https://api.minimax.io/anthropic
```

Both model IDs work with either compatible endpoint. For the China service, use
`https://api.minimaxi.com/v1` or `https://api.minimaxi.com/anthropic`.

Get your MiniMax API key from the [MiniMax Platform](https://platform.minimax.io/user-center/basic-information/interface-key).

## Orchestrator Model Settings

| Variable | Description |
|----------|-------------|
| `ORCHESTRATOR_PROVIDER` | Provider name (`google`, `openai`, `anthropic`, `azure`, `ollama`, `minimax`, `litellm_proxy`) |
| `ORCHESTRATOR_MODEL` | Model ID (e.g., `gemini-3.6-flash`, `gpt-5.6-terra`, `claude-sonnet-5`, `qwen2.5-coder`; `gemini-3.1-pro-preview` for a heavier option) |
| `ORCHESTRATOR_API_KEY` | API key for the provider (if required) |
| `ORCHESTRATOR_ENDPOINT` | Custom endpoint URL (if required) |
| `ORCHESTRATOR_PROJECT_ID` | Google Cloud project ID (for Vertex AI) |
| `ORCHESTRATOR_REGION` | Google Cloud region (default: `us-central1`) |
| `ORCHESTRATOR_PROVIDER_TYPE` | Google provider type (`gla` or `vertex`) |
| `ORCHESTRATOR_THINKING_BUDGET` | Thinking budget for reasoning models |
| `ORCHESTRATOR_SERVICE_ACCOUNT_FILE` | Path to service account file (for Vertex AI) |

## Cypher Model Settings

| Variable | Description |
|----------|-------------|
| `CYPHER_PROVIDER` | Provider name (`google`, `openai`, `anthropic`, `azure`, `ollama`, `minimax`, `litellm_proxy`) |
| `CYPHER_MODEL` | Model ID (e.g., `gemini-3.5-flash-lite`, `gpt-5.6-luna`, `qwen2.5-coder`) |
| `CYPHER_API_KEY` | API key for the provider (if required) |
| `CYPHER_ENDPOINT` | Custom endpoint URL (if required) |
| `CYPHER_PROJECT_ID` | Google Cloud project ID (for Vertex AI) |
| `CYPHER_REGION` | Google Cloud region (default: `us-central1`) |
| `CYPHER_PROVIDER_TYPE` | Google provider type (`gla` or `vertex`) |
| `CYPHER_THINKING_BUDGET` | Thinking budget for reasoning models |
| `CYPHER_SERVICE_ACCOUNT_FILE` | Path to service account file (for Vertex AI) |

## System Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMGRAPH_HOST` | `localhost` | Memgraph hostname |
| `MEMGRAPH_PORT` | `7687` | Memgraph port |
| `MEMGRAPH_HTTP_PORT` | `7444` | Memgraph HTTP port |
| `LAB_PORT` | `3000` | Memgraph Lab port |
| `MEMGRAPH_BATCH_SIZE` | `1000` | Batch size for Memgraph operations |
| `TARGET_REPO_PATH` | `.` | Default repository path |
| `CGR_CAPTURE_LOCAL_DEFINITIONS` | `true` | Capture methods of classes defined inside function bodies (function-local definitions). On by default for exhaustive structure capture; set to `false` to keep the graph free of throwaway helpers and test mocks. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Base URL for the local Ollama server (`/v1` is appended for the OpenAI-compatible endpoint) |

## Setting Up Ollama

```bash
curl -fsSL https://ollama.ai/install.sh | sh

ollama pull qwen2.5-coder
# Or try other models:
# ollama pull deepseek-r1
```

Ollama automatically starts serving on `localhost:11434`.

!!! note
    Local models provide privacy and no API costs, but may have lower accuracy compared to cloud models like Gemini or GPT-5.6.

## Programmatic Configuration

You can also configure providers programmatically via the Python SDK:

```python
from cgr import settings

settings.set_orchestrator("openai", "gpt-5.6-terra", api_key="sk-...")
settings.set_cypher("google", "gemini-3.5-flash-lite", api_key="your-key")
```
