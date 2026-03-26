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
CYPHER_API_KEY=your-api-key
```

Or programmatically:

```python
from cgr import settings

settings.set_cypher("google", "gemini-2.5-flash", api_key="your-key")
```

## Supported Providers

| Provider | Example Models |
|----------|---------------|
| Google | `gemini-2.5-pro`, `gemini-2.5-flash` |
| OpenAI | `gpt-4o`, `gpt-4o-mini` |
| Ollama | `codellama`, `llama3.2` |
