FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# The crate the project depends on, cgr-graph-rs, is built from rust/ during
# uv sync. Debian's cargo trails its locked dependencies (trixie ships rustc
# 1.85; tree-sitter-language needs 1.90), so the toolchain comes from here,
# no older than RUST_TOOLCHAIN in .clusterfuzzlite/Dockerfile, which the fuzz
# build proves the crate with on every pull request.
FROM rust:1.99.1-slim-trixie@sha256:b8e062b1c3a80183df83d9ba187cadd2b5ab84bc78d453dbfeb2fccd5f982247 AS rust

FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS builder

COPY --from=uv /uv /uvx /bin/
COPY --chmod=755 scripts/distrust_ca_roots.sh /usr/local/sbin/

# Naming ca-certificates upgrades it to the archive's current release: the
# pinned base is a snapshot whose CA store can predate a root's removal, as
# with emSign ECC Root CA - G3 in October 2026. The eMudhra roots the archive
# still ships are then deselected. cargo fetches through this store.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates cmake build-essential git libssl-dev zlib1g-dev libzstd-dev && \
    rm -rf /var/lib/apt/lists/* && \
    distrust_ca_roots.sh

COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo /usr/local/cargo
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH

WORKDIR /app

# uv installs its managed Python interpreter outside the project directory by
# default ($HOME/.local/share/uv/python, i.e. /root/.local when run as root).
# .venv only symlinks to that interpreter, so pin the install dir under /app
# and copy it into the final stage too, or the venv's python symlink dangles
# once /root is left behind.
#
# uv only populates this directory when it actually downloads a managed
# interpreter -- if the pinned Python version ever matched this image's
# system Python, uv would use that instead and never create the
# directory, and the unconditional COPY below would fail the build
# outright. only-managed rules that out: uv always uses (and downloads
# if needed) its own interpreter, never the system one.
ENV UV_PYTHON_INSTALL_DIR=/app/.uv-python
ENV UV_PYTHON_PREFERENCE=only-managed

# .python-version must arrive with the manifests: without it this first sync
# falls back to the newest managed Python allowed by requires-python (which
# moves with each uv release), and the later sync, once COPY . . brings in the
# pin, rebuilds .venv on the pinned version. --no-install-local leaves out
# the project and cgr-graph-rs, a path dependency built from rust/ (with the
# cargo above), as neither source is here until COPY . . brings it.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --extra treesitter-full --extra memgraph --no-install-local --no-binary-package pymgclient

COPY . .
RUN uv sync --frozen --no-dev --extra treesitter-full --extra memgraph --no-binary-package pymgclient

FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

COPY --chmod=755 scripts/distrust_ca_roots.sh /usr/local/sbin/

# The CA store as in the builder: the LLM clients verify TLS against it
# through httpx2's truststore context, not certifi.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates ripgrep libssl3 zlib1g libzstd1 && \
    rm -rf /var/lib/apt/lists/* && \
    distrust_ca_roots.sh

RUN useradd --create-home appuser
USER appuser
WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.uv-python /app/.uv-python
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/codebase_rag /app/codebase_rag
COPY --from=builder --chown=appuser:appuser /app/codec /app/codec
COPY --from=builder --chown=appuser:appuser /app/cgr /app/cgr
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:$PATH"

COPY --chmod=755 <<'EOF' /app/entrypoint.sh
#!/bin/sh
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  LIBDIR="/lib/x86_64-linux-gnu" ;;
    aarch64) LIBDIR="/lib/aarch64-linux-gnu" ;;
    *)       LIBDIR="/lib" ;;
esac
export LD_PRELOAD="$LIBDIR/libz.so.1:$LIBDIR/libzstd.so.1"
exec code-graph-rag "$@"
EOF

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["mcp-server"]
