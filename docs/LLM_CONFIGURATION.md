# LLM Configuration Guide

This project uses a **Universal LiteLLM Backend**, enabling seamless integration with 100+ LLM providers including OpenAI, Anthropic, Google, DeepSeek, Ollama, Azure, and more—all without code changes.



## 🚀 Quick Start

### Basic Setup (2 Steps)

**1. Set your API key in `.env`:**

```bash
OPENAI_API_KEY=sk-your-key-here
```

**2. Run a query:**

```bash
graph-code query "Explain the architecture" --model-id openai/gpt-4o
```

That's it! Switch providers anytime by changing the model ID and corresponding API key.


## ⚙️ Configuration Methods

### Method 1: Environment Variables (Recommended)

Best for persistent configuration across sessions. Create a `.env` file in your project root:

```ini
# Main Agent (Orchestrator)
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-4o
ORCHESTRATOR_API_KEY=sk-your-openai-key

# Graph Query Generator (Optional: use a different model)
CYPHER_PROVIDER=groq
CYPHER_MODEL=llama3-70b-8192
CYPHER_API_KEY=gsk-your-groq-key
```

**Why separate models?** Use a powerful model for orchestration and a faster/cheaper model for Cypher query generation to optimize cost and performance.

### Method 2: CLI Flags

Best for quick testing or one-off runs:

```bash
graph-code query "Your question here" \
  --provider openai \
  --model-id gpt-4o \
  --api-key sk-your-key
```

**Note:** CLI flags override environment variables.



## 📚 Provider Setup

### 🤖 OpenAI

| Setting | Value |
|---------|-------|
| **API Key Variable** | `OPENAI_API_KEY` |
| **Model Format** | `openai/<model>` |
| **Popular Models** | `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo` |

**Setup:**

```bash
export OPENAI_API_KEY="sk-proj-..."
graph-code query "Analyze the codebase" --model-id openai/gpt-4o
```



### 🧠 Anthropic (Claude)

| Setting | Value |
|---------|-------|
| **API Key Variable** | `ANTHROPIC_API_KEY` |
| **Model Format** | `anthropic/<model>` |
| **Popular Models** | `claude-3-5-sonnet-latest`, `claude-3-5-haiku-latest`, `claude-3-opus-latest` |

**Setup:**

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
graph-code query "Review code quality" --model-id anthropic/claude-3-5-sonnet-latest
```

**Why Claude?** Excellent for code analysis, long context windows (200K+ tokens), and detailed explanations.



### 💎 Google Gemini

| Setting | Value |
|---------|-------|
| **API Key Variable** | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| **Model Format** | `gemini/<model>` |
| **Popular Models** | `gemini-1.5-pro`, `gemini-1.5-flash`, `gemini-2.0-flash-exp` |

**Setup:**

```bash
export GEMINI_API_KEY="AIza..."
graph-code query "Find security issues" --model-id gemini/gemini-1.5-pro
```

**Pro Tip:** Gemini offers generous free tier limits—great for experimentation.


### 🦙 Ollama (Local/Offline Models)

| Setting | Value |
|---------|-------|
| **API Key Variable** | None (runs locally) |
| **Model Format** | `ollama/<model>` |
| **Popular Models** | `llama3`, `codellama`, `mistral`, `deepseek-coder` |
| **Default Endpoint** | `http://localhost:11434` |

**Setup:**

1. **Install Ollama:** [ollama.com](https://ollama.com)
2. **Pull a model:**
   ```bash
   ollama pull llama3
   ```
3. **Start the server (if not running):**
   ```bash
   ollama serve
   ```
4. **Run Graph-Code:**
   ```bash
   graph-code query "Explain this function" --model-id ollama/llama3
   ```

**Benefits:** Free, private, no internet required after model download.



### 🐋 DeepSeek

| Setting | Value |
|---------|-------|
| **API Key Variable** | `DEEPSEEK_API_KEY` |
| **Model Format** | `deepseek/<model>` |
| **Popular Models** | `deepseek-coder`, `deepseek-chat` |

**Setup:**

```bash
export DEEPSEEK_API_KEY="sk-..."
graph-code query "Optimize this algorithm" --model-id deepseek/deepseek-coder
```

**Why DeepSeek?** Specialized in code understanding and competitive pricing.



### ☁️ Azure OpenAI

| Setting | Value |
|---------|-------|
| **API Key Variable** | `AZURE_API_KEY` |
| **Additional Variables** | `AZURE_API_BASE`, `AZURE_API_VERSION` |
| **Model Format** | `azure/<deployment-name>` |

**Setup:**

```bash
export AZURE_API_KEY="your-azure-key"
export AZURE_API_BASE="https://your-resource.openai.azure.com"
export AZURE_API_VERSION="2024-02-15-preview"

graph-code query "..." --model-id azure/my-gpt4-deployment
```

**Note:** Use your Azure deployment name, not the base model name (e.g., `azure/my-gpt4-deployment` not `azure/gpt-4`).



### 🚀 Groq (Fast Inference)

| Setting | Value |
|---------|-------|
| **API Key Variable** | `GROQ_API_KEY` |
| **Model Format** | `groq/<model>` |
| **Popular Models** | `llama3-70b-8192`, `mixtral-8x7b-32768` |

**Setup:**

```bash
export GROQ_API_KEY="gsk-..."
graph-code query "..." --model-id groq/llama3-70b-8192
```

**Why Groq?** Blazing-fast inference speeds, great for Cypher query generation.


## 🔌 Advanced Configuration

### Using API Gateways (Portkey, Helicone, etc.)

Route requests through third-party gateways for monitoring, caching, or load balancing.

**Example with Portkey:**

```ini
# .env
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-4o
ORCHESTRATOR_ENDPOINT=https://api.portkey.ai/v1
ORCHESTRATOR_EXTRA_HEADERS='{"x-portkey-api-key": "pk-...", "x-portkey-provider": "openai"}'
```

**Example with Helicone:**

```bash
export OPENAI_API_KEY="sk-..."
export ORCHESTRATOR_ENDPOINT="https://oai.hconeai.com/v1"
export ORCHESTRATOR_EXTRA_HEADERS='{"Helicone-Auth": "Bearer sk-helicone-..."}'
```



### Multi-Model Strategy

Optimize cost and performance by using different models for different tasks:

```ini
# .env - Hybrid Setup
# Use powerful model for complex orchestration
ORCHESTRATOR_PROVIDER=anthropic
ORCHESTRATOR_MODEL=claude-3-5-sonnet-latest
ORCHESTRATOR_API_KEY=sk-ant-...

# Use fast, cheap model for Cypher generation
CYPHER_PROVIDER=groq
CYPHER_MODEL=llama3-70b-8192
CYPHER_API_KEY=gsk-...
```

**Cost Savings Example:**
- Claude Sonnet: $3/M input tokens
- Groq Llama3: Free tier available
- Potential savings: 50-70% on total API costs



### Custom/Unsupported Providers

LiteLLM supports many providers not explicitly listed here. To use them:

1. **Find the provider prefix:** Check [LiteLLM Providers](https://docs.litellm.ai/docs/providers)
2. **Set the API key:** Use the provider's required environment variable
3. **Use the model:** `<prefix>/<model-name>`

**Example with Mistral AI:**

```bash
export MISTRAL_API_KEY="your-key"
graph-code query "..." --model-id mistral/mistral-large-latest
```



## 🛠️ Troubleshooting

### Common Issues

**Problem:** `Authentication Error` or `Invalid API Key`

**Solutions:**
- Verify your API key is correct and active
- Check the environment variable name matches the provider (e.g., `ANTHROPIC_API_KEY` not `CLAUDE_API_KEY`)
- Ensure no extra spaces or quotes in your `.env` file
- Try exporting directly: `export OPENAI_API_KEY="sk-..."`



**Problem:** `Provider not found` or `Model not recognized`

**Solutions:**
- Use the correct format: `provider/model` (e.g., `openai/gpt-4o`)
- Verify the model name is exact (check provider's documentation)
- Some models require specific API versions or endpoints



**Problem:** `Connection Error` (Ollama)

**Solutions:**
- Ensure Ollama is running: `ollama serve`
- Check the model is downloaded: `ollama list`
- Verify the endpoint: default is `http://localhost:11434`
- Try pulling the model again: `ollama pull llama3`



**Problem:** Rate limits or quota exceeded

**Solutions:**
- Check your provider's dashboard for usage limits
- Consider using a cheaper model for high-volume tasks
- Implement the multi-model strategy (use fast models for simple queries)
- Add retry logic with exponential backoff



**Problem:** Slow responses

**Solutions:**
- Switch to a faster provider (e.g., Groq for Llama models)
- Use smaller models (e.g., `gpt-4o-mini` instead of `gpt-4o`)
- Enable streaming if supported: `--stream`
- Consider using Ollama locally to eliminate network latency



## 📊 Model Comparison

| Provider | Speed | Cost | Context | Best For |
|----------|-------|------|---------|----------|
| **OpenAI GPT-4o** | Fast | $$$ | 128K | General purpose, high quality |
| **Claude Sonnet 3.5** | Medium | $$$ | 200K | Code analysis, long docs |
| **Gemini 1.5 Pro** | Fast | $$ | 1M | Large codebases, free tier |
| **Groq Llama3** | Very Fast | $ | 8K | Quick queries, Cypher gen |
| **Ollama** | Fast* | Free | Varies | Privacy, offline work |
| **DeepSeek Coder** | Medium | $ | 16K | Code-specific tasks |

*Local hardware dependent


## 💡 Best Practices

### 1. **Start with a Known Provider**
Begin with OpenAI or Anthropic to ensure everything works, then experiment with alternatives.

### 2. **Use Environment Variables**
Keep API keys out of version control. Add `.env` to your `.gitignore`.

### 3. **Test Before Production**
Always test with a small query before running expensive operations.

### 4. **Monitor Usage**
Set up billing alerts in your provider's dashboard to avoid surprises.

### 5. **Optimize Costs**
Use the multi-model strategy: expensive models for orchestration, cheap/fast models for simple tasks.

### 6. **Keep Models Updated**
Provider model names change (e.g., `-latest` versions). Check documentation regularly.