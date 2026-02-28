FROM ghcr.io/astral-sh/uv:0.6 AS uv

FROM python:3.12-slim AS builder

COPY --from=uv /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        cmake build-essential libssl-dev zlib1g-dev libzstd-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra treesitter-full --no-install-project --no-binary-package pymgclient

COPY . .
RUN uv sync --frozen --no-dev --extra treesitter-full --no-binary-package pymgclient

FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ripgrep libssl3 zlib1g libzstd1 && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home appuser
USER appuser
WORKDIR /app

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
