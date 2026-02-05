# Quick Start: LLM Setup

Get running in 30 seconds. Choose one provider and start querying.

---

## Step 1: Choose a Provider

| Provider | Free? | Best For |
|----------|-------|----------|
| **Ollama** | ✅ Local, 100% free | Privacy, offline work |
| **Google Gemini** | ✅ Free tier | Testing, large codebases |
| **OpenAI** | ❌ Paid | Best quality |
| **Anthropic** | ❌ Paid | Code analysis |

---

## Step 2: Configure

Create `.env` file in your project root with ONE of these:

### Ollama (Free, Local)
```bash
# Install: https://ollama.com
# Then: ollama pull llama3

ORCHESTRATOR_PROVIDER=ollama
ORCHESTRATOR_MODEL=llama3
```

### Google Gemini (Free Tier)
```bash
# Get key: https://aistudio.google.com/app/apikey

GEMINI_API_KEY=your-key-here
ORCHESTRATOR_PROVIDER=gemini
ORCHESTRATOR_MODEL=gemini-1.5-flash
```

### OpenAI
```bash
# Get key: https://platform.openai.com/api-keys

OPENAI_API_KEY=sk-proj-your-key
ORCHESTRATOR_PROVIDER=openai
ORCHESTRATOR_MODEL=gpt-4o-mini
```

### Anthropic Claude
```bash
# Get key: https://console.anthropic.com/settings/keys

ANTHROPIC_API_KEY=sk-ant-your-key
ORCHESTRATOR_PROVIDER=anthropic
ORCHESTRATOR_MODEL=claude-3-5-haiku-latest
```

---

## Step 3: Run

```bash
graph-code start
```

Then ask questions like:
- "Explain the architecture of this codebase"
- "How does authentication work?"
- "Show me all API endpoints"

**That's it!** ✅

---

## Next Steps

- **More providers?** See [Complete Configuration Guide](../llm/configuration.md)
- **Advanced features?** See [Configuration Guide](../llm/configuration.md#advanced-features)
- **Troubleshooting?** See [Configuration Guide](../llm/configuration.md#troubleshooting)
- **Use as MCP server?** See [Claude Code Setup](./claude-code-setup.md)
- **Full model list?** See [Supported Providers](../llm/supported-providers.md)
