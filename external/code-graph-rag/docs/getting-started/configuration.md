---
description: "Configure Code-Graph-RAG with provider settings, environment variables, and model options."
---

# Configuration

Configuration is managed through environment variables in the `.env` file. The provider-explicit configuration supports mixing different providers for orchestrator and cypher models.

## Provider Examples

### All Ollama (Local Models)

```bash
ORCHESTRATOR_PROVIDER=ollama
ORCHESTRATOR_MODEL=llama3.2
ORCHESTRATOR_ENDPOINT=http://localhost:11434/v1

CYPHER_PROVIDER=ollama
CYPHER_MODEL=codellama
CYPHER_ENDPOINT=http://localhost:11434/v1
```

### All OpenAI Models

```bash
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-4o
ORCHESTRATOR_API_KEY=sk-your-openai-key

CYPHER_PROVIDER=openai
CYPHER_MODEL=gpt-4o-mini
CYPHER_API_KEY=sk-your-openai-key
```

### All Google Models

```bash
ORCHESTRATOR_PROVIDER=google
ORCHESTRATOR_MODEL=gemini-2.5-pro
ORCHESTRATOR_API_KEY=your-google-api-key

CYPHER_PROVIDER=google
CYPHER_MODEL=gemini-2.5-flash
CYPHER_API_KEY=your-google-api-key
```

Get your Google API key from [Google AI Studio](https://aistudio.google.com/app/apikey).

### Mixed Providers

```bash
ORCHESTRATOR_PROVIDER=google
ORCHESTRATOR_MODEL=gemini-2.5-pro
ORCHESTRATOR_API_KEY=your-google-api-key

CYPHER_PROVIDER=ollama
CYPHER_MODEL=codellama
CYPHER_ENDPOINT=http://localhost:11434/v1
```

## Orchestrator Model Settings

| Variable | Description |
|----------|-------------|
| `ORCHESTRATOR_PROVIDER` | Provider name (`google`, `openai`, `ollama`) |
| `ORCHESTRATOR_MODEL` | Model ID (e.g., `gemini-2.5-pro`, `gpt-4o`, `llama3.2`) |
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
| `CYPHER_PROVIDER` | Provider name (`google`, `openai`, `ollama`) |
| `CYPHER_MODEL` | Model ID (e.g., `gemini-2.5-flash`, `gpt-4o-mini`, `codellama`) |
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
| `LOCAL_MODEL_ENDPOINT` | `http://localhost:11434/v1` | Fallback endpoint for Ollama |

## Setting Up Ollama

```bash
curl -fsSL https://ollama.ai/install.sh | sh

ollama pull llama3.2
# Or try other models:
# ollama pull llama3
# ollama pull mistral
# ollama pull codellama
```

Ollama automatically starts serving on `localhost:11434`.

!!! note
    Local models provide privacy and no API costs, but may have lower accuracy compared to cloud models like Gemini or GPT-4o.

## Programmatic Configuration

You can also configure providers programmatically via the Python SDK:

```python
from cgr import settings

settings.set_orchestrator("openai", "gpt-4o", api_key="sk-...")
settings.set_cypher("google", "gemini-2.5-flash", api_key="your-key")
```
