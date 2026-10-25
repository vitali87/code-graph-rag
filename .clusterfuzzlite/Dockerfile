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
# ca-certificates (20240203~20.04.1) still carries emSign ECC Root CA - G3,
# so that root is deselected and the store rebuilt before anything is fetched.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git && \
    rm -rf /var/lib/apt/lists/* && \
    sed -i 's|^mozilla/emSign_ECC_Root_CA_-_G3\.crt$|!&|' /etc/ca-certificates.conf && \
    update-ca-certificates --fresh > /dev/null && \
    test ! -e /etc/ssl/certs/emSign_ECC_Root_CA_-_G3.pem

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
# take for another Rust install.
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUST_TOOLCHAIN=1.85.0
ENV CARGO_HOME=/rust RUSTUP_HOME=/rust/rustup PATH=$PATH:/rust/bin
RUN curl -fsSLO "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" && \
    echo "${RUSTUP_SHA256}  rustup-init" | sha256sum -c - && \
    chmod +x rustup-init && \
    RUSTUP_INIT_SKIP_PATH_CHECK=yes ./rustup-init -y --no-modify-path --profile minimal \
        --default-toolchain "${RUST_TOOLCHAIN}" && \
    rm rustup-init && \
    cargo --version

# Make that interpreter the default `python3` so compile_python_fuzzer and
# build.sh use it without either knowing where it came from.
RUN ln -sf "$(uv python find 3.12)" /usr/local/bin/python3 && \
    python3 --version

# The interpreter's bundled pip may predate the emSign distrust (pip 26.2.2),
# so the first thing it fetches is pip itself, from a wheel it checks against
# a pinned SHA-256. The installs below and build.sh's then run on that pip.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

# atheris and pyinstaller belong to the base image's 3.11 and are invisible to
# 3.12, so install them for the interpreter that will actually freeze the
# targets. atheris 3.1.0 is published only as manylinux wheels carrying their
# own libFuzzer, so nothing here is compiled against the image's.
# uv's standalone interpreters ship an EXTERNALLY-MANAGED marker; this image
# has no other Python for 3.12, so installing into it directly is intended.
#
# Every tool is pinned: an unpinned atheris is a different fuzzing engine
# from one day to the next. coverage is here for the coverage build, whose
# stub runs `import coverage` inside the frozen target; it is a dev
# dependency, so the lockfile install in build.sh does not bring it, and the
# pin matches the version uv.lock resolves for development.
RUN PYI_STATIC_ZLIB=1 \
    python3 -m pip install --no-cache-dir --break-system-packages \
        "atheris==3.1.0" "pyinstaller==6.10.0" "coverage==7.16.2"

COPY . $SRC/code-graph-rag
WORKDIR $SRC/code-graph-rag
COPY .clusterfuzzlite/build.sh $SRC/build.sh
