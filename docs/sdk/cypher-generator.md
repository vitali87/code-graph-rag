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
CYPHER_MODEL=gemini-2.5-flash

# or Anthropic
CYPHER_PROVIDER=anthropic
CYPHER_MODEL=claude-haiku-4-5
CYPHER_API_KEY=sk-ant-your-anthropic-key
CYPHER_API_KEY=your-api-key
```

Or programmatically:

```python
from cgr import settings

settings.set_cypher("google", "gemini-2.5-flash", api_key="your-key")

# or Anthropic
settings.set_cypher("anthropic", "claude-haiku-4-5", api_key="sk-ant-...")
```

## Supported Providers

| Provider | Example Models |
|----------|---------------|
| Anthropic | `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5` |
| Google | `gemini-2.5-pro`, `gemini-2.5-flash` |
| OpenAI | `gpt-4o`, `gpt-4o-mini` |
| Ollama | `codellama`, `llama3.2` |
