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
FROM gcr.io/oss-fuzz-base/base-builder-python

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# uv is installed by its own script; the interpreter it fetches is the point.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
RUN uv python install 3.12

# Make that interpreter the default `python3` so compile_python_fuzzer and
# build.sh use it without either knowing where it came from.
RUN ln -sf "$(uv python find 3.12)" /usr/local/bin/python3 && \
    python3 --version

# atheris and pyinstaller belong to the base image's 3.11 and are invisible to
# 3.12, so install them for the interpreter that will actually freeze the
# targets. atheris links the image's libFuzzer rather than building its own,
# the same way base-builder's install_python.sh does it.
# uv's standalone interpreters ship an EXTERNALLY-MANAGED marker; this image
# has no other Python for 3.12, so installing into it directly is intended.
RUN LIBFUZZER_LIB=$(echo /usr/local/lib/clang/*/lib/x86_64-unknown-linux-gnu/libclang_rt.fuzzer_no_main.a) \
    PYI_STATIC_ZLIB=1 \
    python3 -m pip install --no-cache-dir --break-system-packages \
        "atheris>=2.3.0" "pyinstaller==6.10.0"

COPY . $SRC/code-graph-rag
WORKDIR $SRC/code-graph-rag
COPY .clusterfuzzlite/build.sh $SRC/build.sh
