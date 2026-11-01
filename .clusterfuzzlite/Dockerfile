# ClusterFuzzLite build image.
#
# Two constraints pull in opposite directions:
#
# * The project requires Python >= 3.12 and means it: `parser_loader.py` uses
#   PEP 695 generics (`class _LazyLanguageView[V]`), which is a SyntaxError on
#   3.11, so the stock base-builder-python interpreter cannot import the
#   package at all.
# * ClusterFuzzLite BUILDS in one image and RUNS the frozen targets in
#   another, and the runner (`clusterfuzzlite-run-fuzzers:v1`) is pinned to
#   glibc 2.31 inside the action, so it cannot be swapped.
#
# Building on the ubuntu-24-04 base solves the first and breaks the second:
# PyInstaller bundles that image's libpython/libstdc++, which demand
# GLIBC_2.32-2.38, and every target dies on the runner with
#
#   version `GLIBC_2.38' not found (required by .../libpython3.12.so.1.0)
#
# while passing a local build-and-run, because locally one container does both.
#
# So: stay on the 20.04 base (glibc 2.31, matching the runner) and get 3.12
# from uv's standalone CPython, which is built for an ancient glibc floor --
# `objdump -T` on its libpython reports a maximum requirement of GLIBC_2.9.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:21f6fab17ace4dc76e1c56de91ee95d3d03da021df94b41056ba036f6c6f55e9

# pip trusts this store as well as its own certifi, and Ubuntu 20.04's newest
# ca-certificates (20240203~20.04.1) still carries every distrusted eMudhra
# root, so they are deselected and the store rebuilt before anything is fetched.
COPY --chmod=755 scripts/distrust_ca_roots.sh /usr/local/sbin/
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git && \
    rm -rf /var/lib/apt/lists/* && \
    distrust_ca_roots.sh

# uv is fetched as a pinned release artifact and checked against the SHA-256
# its publisher ships, rather than by piping https://astral.sh/uv/install.sh
# into a root shell: that URL is mutable, and the image build runs as root, so
# a compromised script would get root code execution during CI.
ARG UV_VERSION=0.13.5
ARG UV_SHA256=f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f
RUN curl -fsSLO "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" && \
    echo "${UV_SHA256}  uv-x86_64-unknown-linux-gnu.tar.gz" | sha256sum -c - && \
    tar -xzf uv-x86_64-unknown-linux-gnu.tar.gz && \
    install -m 0755 uv-x86_64-unknown-linux-gnu/uv /usr/local/bin/uv && \
    rm -rf uv-x86_64-unknown-linux-gnu uv-x86_64-unknown-linux-gnu.tar.gz

RUN uv python install 3.12

# code-graph-rag requires cgr-graph-rs, its Rust extension, and a PR has to be
# fuzzed with the crate it carries, which PyPI may not have yet; build.sh
# builds it from rust/cgr_graph_rs. base-builder-python ships OSS-Fuzz's cargo
# wrapper but no toolchain behind it, so install one where the wrapper looks
# (/rust/bin), checked against the SHA-256 rustup publishes, as uv is above.
# That wrapper sits at /usr/local/bin/cargo, which rustup-init would otherwise
# take for another Rust install. cargo refuses a toolchain older than any
# locked dependency's rust-version; tree-sitter-language, which the tree-sitter
# crate pulls in, needs 1.90.
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUST_TOOLCHAIN=1.97.0
ENV CARGO_HOME=/rust RUSTUP_HOME=/rust/rustup PATH=$PATH:/rust/bin
RUN curl -fsSLO "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" && \
    echo "${RUSTUP_SHA256}  rustup-init" | sha256sum -c - && \
    chmod +x rustup-init && \
    RUSTUP_INIT_SKIP_PATH_CHECK=yes ./rustup-init -y --no-modify-path --profile minimal \
        --default-toolchain "${RUST_TOOLCHAIN}" && \
    rm rustup-init && \
    cargo --version

# The graph-rs fuzz targets (rust/cgr_graph_rs/fuzz) are cargo-fuzz targets,
# built by the `language: rust` job. OSS-Fuzz's compile hands Rust builds
# -Zsanitizer through RUSTFLAGS, which only a nightly accepts, so they get the
# nightly and the cargo-fuzz release base-builder-rust pins, the pair OSS-Fuzz
# builds against this image's clang. The nightly must be new enough for the
# crate's dependencies, as RUST_TOOLCHAIN above must. A rust build also copies
# the DEFAULT toolchain's std sources (for coverage reports) and stops if they
# are missing, so the stable toolchain carries rust-src.
ARG CARGO_FUZZ_VERSION=0.13.2
ENV RUST_FUZZ_TOOLCHAIN=nightly-2025-09-05
RUN rustup toolchain install "${RUST_FUZZ_TOOLCHAIN}" --profile minimal && \
    rustup component add rust-src --toolchain "${RUST_TOOLCHAIN}" && \
    cargo install cargo-fuzz --locked --version "${CARGO_FUZZ_VERSION}" && \
    rm -rf /rust/registry && \
    cargo fuzz --version

# Make that interpreter the default `python3` so compile_python_fuzzer and
# build.sh use it without either knowing where it came from.
RUN ln -sf "$(uv python find 3.12)" /usr/local/bin/python3 && \
    python3 --version

# The interpreter's bundled pip may predate the emSign distrust (pip 26.2.2),
# so the first thing it fetches is pip itself, the release
# scripts/pip-requirements.txt pins, checked against its SHA-256s. The installs
# below and build.sh's then run on that pip.
COPY scripts/pip-requirements.txt /tmp/
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        --require-hashes -r /tmp/pip-requirements.txt

# atheris and pyinstaller belong to the base image's 3.11 and are invisible to
# 3.12, so install them for the interpreter that will actually freeze the
# targets, from requirements.txt, which pins them and what they pull in by
# hash. atheris links the image's libFuzzer rather than building its own,
# the same way base-builder's install_python.sh does it.
# uv's standalone interpreters ship an EXTERNALLY-MANAGED marker; this image
# has no other Python for 3.12, so installing into it directly is intended.
COPY .clusterfuzzlite/requirements.txt /tmp/fuzz-requirements.txt
RUN LIBFUZZER_LIB=$(echo /usr/local/lib/clang/*/lib/x86_64-unknown-linux-gnu/libclang_rt.fuzzer_no_main.a) \
    PYI_STATIC_ZLIB=1 \
    python3 -m pip install --no-cache-dir --break-system-packages \
        --require-hashes -r /tmp/fuzz-requirements.txt

COPY . $SRC/code-graph-rag
WORKDIR $SRC/code-graph-rag
COPY .clusterfuzzlite/build.sh $SRC/build.sh
