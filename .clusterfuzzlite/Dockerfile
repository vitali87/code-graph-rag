# ClusterFuzzLite build image. The base image supplies a Clang with libFuzzer,
# which is why the harnesses build here and not against Apple Clang locally.
FROM gcr.io/oss-fuzz-base/base-builder-python

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

COPY . $SRC/code-graph-rag
WORKDIR $SRC/code-graph-rag
COPY .clusterfuzzlite/build.sh $SRC/build.sh
