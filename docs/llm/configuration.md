# LLM Configuration Guide

Complete guide to configuring LLM providers for Graph-Code RAG.

**New user?** Start with the [Quick Start Guide](../getting-started/quick-start.md) instead.

---

## Table of Contents

- [Detailed Provider Setup](#detailed-provider-setup)
- [Advanced Features](#advanced-features)
- [Troubleshooting](#troubleshooting)
- [Model Recommendations](#model-recommendations)

---

## Detailed Provider Setup

### OpenAI

**Models:** `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `o1`, `o1-mini`, `o3-mini`

```bash
# .env
OPENAI_API_KEY=sk-proj-...
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-4o
```

**Pricing (per 1M tokens):**
- GPT-4o: $5 input / $15 output
- GPT-4o-mini: $0.15 input / $0.60 output

**Get API Key:** [platform.openai.com/api-keys](https://platform.openai.com/api-keys)

---

### Anthropic Claude

**Models:** `claude-3-5-sonnet-latest`, `claude-3-5-haiku-latest`, `claude-3-opus-latest`, `claude-4-sonnet`, `claude-opus-4-5`

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
ORCHESTRATOR_PROVIDER=anthropic
ORCHESTRATOR_MODEL=claude-3-5-sonnet-latest
```

**Pricing (per 1M tokens):**
- Sonnet: $3 input / $15 output
- Haiku: $0.25 input / $1.25 output
- Opus: $15 input / $75 output

**Context:** 200K tokens (excellent for large codebases)

**Get API Key:** [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)

---

### Google Gemini

**Models:** `gemini-1.5-flash`, `gemini-1.5-pro`, `gemini-2.5-flash`, `gemini-2.5-pro`

```bash
# .env
GEMINI_API_KEY=AIza...
ORCHESTRATOR_PROVIDER=gemini
ORCHESTRATOR_MODEL=gemini-1.5-flash
```

**Free Tier:** 15 requests/min, 1M tokens/min

**Context:** Up to 1M tokens (largest context window)

**Get API Key:** [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)

---

### Ollama (Local Models)

**Models:** `llama3`, `codellama`, `deepseek-coder`, `mistral`, `qwen`

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull llama3

# .env
ORCHESTRATOR_PROVIDER=ollama
ORCHESTRATOR_MODEL=llama3
```

**No API key needed** - runs 100% locally

**Requirements:** 16GB RAM recommended, 8GB minimum

---

### DeepSeek

**Models:** `deepseek-coder`, `deepseek-chat`, `deepseek-r1`, `deepseek-v3`

```bash
# .env
DEEPSEEK_API_KEY=sk-...
ORCHESTRATOR_PROVIDER=deepseek
ORCHESTRATOR_MODEL=deepseek-coder
```

**Best for:** Code-specific tasks, competitive pricing

**Get API Key:** [platform.deepseek.com](https://platform.deepseek.com)

---

### Groq (Ultra-Fast)

**Models:** `llama3-70b-8192`, `mixtral-8x7b-32768`, `gemma-7b-it`

```bash
# .env
GROQ_API_KEY=gsk-...
ORCHESTRATOR_PROVIDER=groq
ORCHESTRATOR_MODEL=llama3-70b-8192
```

**Best for:** Speed-critical applications, real-time responses

**Free tier available**

**Get API Key:** [console.groq.com](https://console.groq.com)

---

### Azure OpenAI

```bash
# .env
AZURE_API_KEY=your-key
AZURE_API_BASE=https://your-resource.openai.azure.com
AZURE_API_VERSION=2024-02-15-preview
ORCHESTRATOR_PROVIDER=azure
ORCHESTRATOR_MODEL=my-gpt4-deployment  # Your deployment name
```

**Important:** Use your Azure deployment name, not the base model name

**Best for:** Enterprise, compliance requirements

---

### Google Vertex AI (GCP)

```bash
# .env
ORCHESTRATOR_PROVIDER=vertex_ai
ORCHESTRATOR_MODEL=gemini-1.5-pro
ORCHESTRATOR_PROJECT_ID=your-gcp-project
ORCHESTRATOR_REGION=us-central1
ORCHESTRATOR_SERVICE_ACCOUNT_FILE=/path/to/service-account.json
```

**Requirements:**
- GCP project with Vertex AI API enabled
- Service account with appropriate permissions

**Best for:** Enterprise GCP deployments

---

## Advanced Features

### Multi-Model Strategy (Cost Optimization)

Use different models for different tasks:

```bash
# .env
# Powerful model for complex orchestration
ORCHESTRATOR_PROVIDER=anthropic
ORCHESTRATOR_MODEL=claude-3-5-sonnet-latest
ANTHROPIC_API_KEY=sk-ant-...

# Fast/cheap model for database queries
CYPHER_PROVIDER=groq
CYPHER_MODEL=llama3-70b-8192
GROQ_API_KEY=gsk-...
```

**Cost savings:** 50-70% vs premium models for everything

---

### API Gateway Integration

Route through gateways for monitoring, caching, analytics.

**Portkey Example:**

```bash
# .env
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-4o
ORCHESTRATOR_ENDPOINT=https://api.portkey.ai/v1
ORCHESTRATOR_EXTRA_HEADERS={"x-portkey-api-key":"pk-key","x-portkey-provider":"openai"}
```

**Helicone Example:**

```bash
# .env
OPENAI_API_KEY=sk-...
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-4o
ORCHESTRATOR_ENDPOINT=https://oai.hconeai.com/v1
ORCHESTRATOR_EXTRA_HEADERS={"Helicone-Auth":"Bearer sk-helicone-key"}
```

**Benefits:**
- Request logging
- Cost tracking
- Caching (reduce costs)
- Load balancing

---

### Thinking Budget (Reasoning Models)

Control reasoning depth for o1, o3, DeepSeek-R1:

```bash
# .env
ORCHESTRATOR_THINKING_BUDGET=10000  # Higher = more thorough
```

---

### CLI Override

Test without modifying `.env`:

```bash
# Override for one session
graph-code start \
  --orchestrator anthropic/claude-3-5-haiku-latest \
  --cypher anthropic/claude-3-5-haiku-latest
```

---

## Troubleshooting

### Authentication Error

**Problem:** `Invalid API Key` or `Authentication failed`

**Solutions:**
1. Verify key is correct (no extra spaces/quotes)
2. Check environment variable name:
   - OpenAI: `OPENAI_API_KEY`
   - Anthropic: `ANTHROPIC_API_KEY`
   - Google: `GEMINI_API_KEY` or `GOOGLE_API_KEY`
3. Try exporting directly:
   ```bash
   export OPENAI_API_KEY="sk-..."
   ```

---

### Provider Not Found

**Problem:** `Provider 'xyz' not recognized`

**Solutions:**
1. Use correct format: `provider/model` (e.g., `openai/gpt-4o`)
2. Check provider name matches:
   ```bash
   # ✅ Correct
   ORCHESTRATOR_MODEL=openai/gpt-4o

   # ❌ Wrong
   ORCHESTRATOR_MODEL=gpt-4o
   ```

---

### Ollama Connection Error

**Problem:** `Connection refused` or `Cannot connect to Ollama`

**Solutions:**
1. Start Ollama server:
   ```bash
   ollama serve
   ```
2. Verify model is downloaded:
   ```bash
   ollama list
   ollama pull llama3
   ```
3. Check endpoint:
   ```bash
   curl http://localhost:11434/api/tags
   ```

---

### Rate Limits

**Problem:** `Rate limit exceeded` or `429 error`

**Solutions:**
1. Check provider dashboard for limits
2. Switch to cheaper model:
   ```bash
   # Instead of gpt-4o
   ORCHESTRATOR_MODEL=gpt-4o-mini
   ```
3. Use multi-model strategy (fast model for Cypher)
4. Consider Ollama (no rate limits)

---

### Slow Responses

**Solutions:**
1. Switch to faster provider (Groq)
2. Use smaller models (`gpt-4o-mini` vs `gpt-4o`)
3. Use Ollama locally (no network latency)

---

## Model Recommendations

### By Use Case

| Use Case | Recommended Model | Why |
|----------|------------------|-----|
| **Getting Started** | `gemini/gemini-1.5-flash` | Free tier, fast |
| **Local/Privacy** | `ollama/llama3` | 100% free, offline |
| **Best Quality** | `openai/gpt-4o` | Industry-leading |
| **Code Analysis** | `anthropic/claude-3-5-sonnet-latest` | 200K context |
| **Cost Optimized** | `openai/gpt-4o-mini` | 60% cheaper |
| **Speed** | `groq/llama3-70b-8192` | Ultra-fast |
| **Large Codebases** | `gemini/gemini-1.5-pro` | 1M context |
| **Code-Specific** | `deepseek/deepseek-coder` | Code-optimized |

---

### By Context Size

| Model | Context Window | Best For |
|-------|---------------|----------|
| Gemini 1.5 Pro | 1M tokens | Entire large repositories |
| Claude 3.5 Sonnet | 200K tokens | Full application context |
| GPT-4o | 128K tokens | Standard projects |
| Groq Llama3 | 8K tokens | Quick queries |

---

### By Cost

| Tier | Model | Input Cost | Output Cost |
|------|-------|-----------|-------------|
| **Free** | Ollama (local) | $0 | $0 |
| **Free Tier** | Gemini Flash | Free tier | Free tier |
| **Budget** | GPT-4o-mini | $0.15/1M | $0.60/1M |
| **Mid** | Claude Haiku | $0.25/1M | $1.25/1M |
| **Premium** | GPT-4o | $5/1M | $15/1M |
| **Expensive** | Claude Opus | $15/1M | $75/1M |

---

## Environment Variable Reference

```bash
# Required
ORCHESTRATOR_PROVIDER=<provider>
ORCHESTRATOR_MODEL=<model-id>

# Optional: Separate model for database queries
CYPHER_PROVIDER=<provider>
CYPHER_MODEL=<model-id>

# Provider API Keys
OPENAI_API_KEY=sk-proj-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
GOOGLE_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
GROQ_API_KEY=gsk-...
MISTRAL_API_KEY=...
COHERE_API_KEY=...

# Azure-specific
AZURE_API_KEY=...
AZURE_API_BASE=https://your-resource.openai.azure.com
AZURE_API_VERSION=2024-02-15-preview

# Vertex AI-specific
ORCHESTRATOR_PROJECT_ID=your-gcp-project
ORCHESTRATOR_REGION=us-central1
ORCHESTRATOR_SERVICE_ACCOUNT_FILE=/path/to/service-account.json

# Advanced
ORCHESTRATOR_ENDPOINT=<custom-endpoint>
ORCHESTRATOR_EXTRA_HEADERS={"header":"value"}
ORCHESTRATOR_THINKING_BUDGET=10000
OLLAMA_BASE_URL=http://localhost:11434
```

---

## Related Documentation

- **[Quick Start](../getting-started/quick-start.md)** - Get running in 30 seconds
- **[Supported Providers](./supported-providers.md)** - Complete list of 100+ models
- **[.env.example](../../.env.example)** - Configuration examples
- **[LiteLLM Docs](https://docs.litellm.ai/docs/providers)** - Official LiteLLM documentation
