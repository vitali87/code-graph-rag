---
description: "Troubleshoot common Code-Graph-RAG issues with Memgraph, Ollama, and model configuration."
---

# Troubleshooting

## pymgclient Build Fails on macOS

When pip has no prebuilt `pymgclient` wheel for your platform it builds from
source, and the build can fail first on a missing CMake and then on OpenSSL
headers CMake cannot locate (Homebrew does not put OpenSSL on the default
search paths). Install the build dependencies and point CMake at Homebrew's
OpenSSL:

```bash
brew install cmake pkg-config openssl
export OPENSSL_ROOT_DIR="$(brew --prefix openssl)"
export PKG_CONFIG_PATH="$(brew --prefix openssl)/lib/pkgconfig:$PKG_CONFIG_PATH"
```

Then rerun the install in the same shell. Verified recipe: with those
variables set, `pip install --no-binary pymgclient pymgclient` builds and
imports cleanly.

## Check Memgraph Connection

- Ensure Docker containers are running: `docker compose ps`
- Verify Memgraph is accessible on port 7687

## View Database in Memgraph Lab

- Open [http://localhost:3000](http://localhost:3000)
- Connect to `memgraph:7687`

## Local Model Issues (Ollama)

- Verify Ollama is running: `ollama list`
- Check if models are downloaded: `ollama pull qwen2.5-coder`
- Test Ollama API: `curl http://localhost:11434/v1/models`
- Check Ollama logs: `ollama logs`

## General Checklist

1. Check the logs for error details
2. Verify Memgraph connection
3. Ensure all environment variables are set
4. Review the graph schema matches your expectations
5. Run `cgr doctor` to validate your setup

## Language Grammar Issues

**Grammar not found**: Use a custom URL:

```bash
cgr language add-grammar --grammar-url https://github.com/custom/tree-sitter-mylang
```

**Version incompatibility**: Update tree-sitter:

```bash
uv add tree-sitter@latest
```

**Missing node types**: Manually adjust the configuration in `codebase_rag/language_spec.py`.
