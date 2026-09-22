---
description: "Build a standalone binary of Code-Graph-RAG using PyInstaller."
---

# Building Binaries

You can build a standalone binary of Code-Graph-RAG using the `build_binary.py` script. This uses PyInstaller to package the application and its dependencies into a single executable.

## Build

```bash
python build_binary.py
```

The resulting binary will be located in the `dist` directory.

## Third-party notices

The binary bundles every runtime dependency into one file, and the permissive
licences those packages carry (MIT, Apache-2.0, BSD, MPL-2.0, ...) require their
copyright notices and licence texts to accompany any redistribution. PyInstaller
does not copy the wheels' licence files, so generate the notices as a sidecar to
ship next to the binary:

```bash
python scripts/generate_third_party_notices.py \
  --output dist/code-graph-rag-<platform>-<arch>.THIRD_PARTY_NOTICES.txt
```

The file lists the runtime dependency closure of the installed `code-graph-rag`
distribution for the current platform, with each package's licence expression
and licence text. Developer tooling present in the same virtualenv is not
reachable from the project's own requirements and is not listed. The release
workflow runs this for every platform it builds and attaches the result to the
GitHub release beside the binary.
