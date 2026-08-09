---
description: "Generate Cypher queries from natural language using Code-Graph-RAG's CypherGenerator."
---

# Cypher Generator

The `CypherGenerator` translates natural language questions into Cypher queries for the knowledge graph.

## Usage

```python
import asyncio
from cgr import CypherGenerator

async def main():
    gen = CypherGenerator()
    cypher = await gen.generate("Find all classes that inherit from BaseModel")
    print(cypher)

asyncio.run(main())
```

## Configuration

The Cypher generator uses the configured Cypher provider. Set it via environment variables:

```bash
CYPHER_PROVIDER=google
CYPHER_MODEL=gemini-3.5-flash-lite
CYPHER_API_KEY=your-google-api-key
```

Or with Anthropic:

```bash
CYPHER_PROVIDER=anthropic
CYPHER_MODEL=claude-haiku-4-5
CYPHER_API_KEY=sk-ant-your-anthropic-key
```

Or programmatically:

```python
from cgr import settings

settings.set_cypher("google", "gemini-3.5-flash-lite", api_key="your-google-api-key")
```

Or with Anthropic:

```python
from cgr import settings

settings.set_cypher("anthropic", "claude-haiku-4-5", api_key="sk-ant-your-key")
```

## Supported Providers

| Provider | Example Models |
|----------|---------------|
| Anthropic | `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5` |
| Google | `gemini-3.6-flash`, `gemini-3.5-flash-lite` |
| OpenAI | `gpt-5.6-terra`, `gpt-5.6-luna` |
| Ollama | `qwen2.5-coder`, `llama3.2` |
