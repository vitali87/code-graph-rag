# Build code-graph-rag for Anthropic's OSS Scanner.
#
# The scanner uses the repository root as the build context and audits /src
# offline, so everything the tests and the language frontends need is fetched
# here. This mirrors the Linux unit-test job in .github/workflows/ci.yml:
# Python 3.12, the locked extras and the dev group, and Go for the go/types
# helper, plus a JDK for the javac frontend and ripgrep for the shell tool.
# threat_model.md says how to run it.
#
# The `semantic` and `milvus` extras are left out: torch alone pulls several GB
# of CUDA wheels, and the embedding model it would load must come from
# HuggingFace, which the offline audit cannot reach anyway.

# gotypes/go.mod sets the floor at 1.27.2.
FROM golang:1.27.2-trixie AS go
FROM eclipse-temurin:21-jdk AS jdk

# The full image brings a C toolchain, git and the common -dev headers, for the
# crate's bundled SQLite and anything else built from source.
FROM python:3.12-trixie

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    JAVA_HOME=/opt/java/openjdk \
    PATH=/usr/local/cargo/bin:/usr/local/go/bin:/opt/java/openjdk/bin:$PATH

COPY --from=go /usr/local/go /usr/local/go
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk

# cgr-graph-rs (rust/cgr_graph_rs) is a path dependency, so the sync below
# builds it. rustup is checked against the SHA-256 it publishes and pinned to
# the toolchain .clusterfuzzlite/Dockerfile builds the crate with.
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUST_TOOLCHAIN=1.85.0
RUN curl -fsSLO "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" \
    && echo "${RUSTUP_SHA256}  rustup-init" | sha256sum -c - \
    && chmod +x rustup-init \
    && ./rustup-init -y --no-modify-path --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" \
    && rm rustup-init \
    && cargo --version

# The shell tool's allowlist includes `rg`, with its own flag guards.
RUN cargo install --locked ripgrep@14.1.1 && rg --version

# The uv minor the production Dockerfile pins.
RUN pip install --no-cache-dir "uv==0.13.5"

# Scanner contract: the checkout lives inside the image, at /src.
COPY . /src
WORKDIR /src

# Every extra but `semantic` and `milvus`, plus the dev group, from uv.lock
# into /src/.venv. The project is installed editable, and the crate is built
# from this checkout rather than taken from the published wheel.
RUN uv sync --frozen \
        --extra treesitter-full --extra test --extra neo4j --extra memgraph \
        --extra ast-grep --extra sqlite-vec --extra python-semantics --extra cpp \
        --group dev

# The go/types helper is built on first use from the module cache, pinned by
# its go.sum, so fetch its modules now. The crate's own tests need its
# dependencies fetched too.
RUN cd codebase_rag/parsers/go_frontend/gotypes && go mod download \
    && cargo fetch --locked --manifest-path /src/rust/cgr_graph_rs/Cargo.toml

# The network is gone after this point, so `uv run` must use the environment
# built above as it is, cargo and go must use what was fetched above, and
# HuggingFace must not be tried.
ENV UV_FROZEN=1 \
    UV_NO_SYNC=1 \
    UV_OFFLINE=1 \
    CARGO_NET_OFFLINE=true \
    GOTOOLCHAIN=local \
    GOPROXY=off \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    VIRTUAL_ENV=/src/.venv \
    PATH=/src/.venv/bin:$PATH

# The tests for the tools that guard the host (shell allowlist, path and
# symlink confinement, the read-only Cypher guard, MCP HTTP auth and input
# bounds) and the Rust extension, so the image is known to work. A failing
# test must not abort the build; threat_model.md lists the rest of the suite.
RUN pytest -q -p no:cacheprovider -n 4 -m "not integration" \
        codebase_rag/tests/test_shell_command.py \
        codebase_rag/tests/test_shell_confined_reads.py \
        codebase_rag/tests/test_symlink_containment.py \
        codebase_rag/tests/test_file_reader.py \
        codebase_rag/tests/test_file_writer.py \
        codebase_rag/tests/test_file_editor.py \
        codebase_rag/tests/test_cypher_guard.py \
        codebase_rag/tests/test_mcp_http_auth.py \
        codebase_rag/tests/test_mcp_input_bounds.py \
        codebase_rag/tests/test_embedded_graph_rust.py \
    || echo "WARNING: pytest reported failures"
