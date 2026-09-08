# ClusterFuzzLite build image.
#
# The default base-builder-python ships Python 3.11, but this project declares
# `requires-python = ">=3.12"`, so `pip install .` inside the stock image fails
# with "Package 'code-graph-rag' requires a different Python: 3.11.13 not in
# '>=3.12'" and no fuzzer is ever built. Relaxing the constraint is not an
# option: CI tests 3.12 and 3.13 only.
#
# So take the ubuntu-24-04 tag, which has 3.12 in its archives, make it the
# default `python3`, and reinstall the fuzzing toolchain against it -- the base
# image's atheris and pyinstaller belong to 3.11 and are invisible to 3.12.
# This is the pattern oss-fuzz's own `ipython` project uses for the same
# reason.
FROM gcr.io/oss-fuzz-base/base-builder-python:ubuntu-24-04

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        python3.12 \
        python3.12-dev \
        python3.12-venv && \
    rm -rf /var/lib/apt/lists/*

# `EXTERNALLY-MANAGED` makes pip refuse to install into the system 3.12.
RUN rm -f /usr/lib/python3.12/EXTERNALLY-MANAGED && \
    update-alternatives --install /usr/local/bin/python3 python3 /usr/bin/python3.12 1 && \
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3

# atheris must link the image's libFuzzer rather than build its own, the same
# way base-builder's install_python.sh does it.
RUN LIBFUZZER_LIB=$(echo /usr/local/lib/clang/*/lib/x86_64-unknown-linux-gnu/libclang_rt.fuzzer_no_main.a) \
    PYI_STATIC_ZLIB=1 \
    python3 -m pip install --no-cache-dir "atheris>=2.3.0" "pyinstaller==6.10.0"

COPY . $SRC/code-graph-rag
WORKDIR $SRC/code-graph-rag
COPY .clusterfuzzlite/build.sh $SRC/build.sh
