# The machine a pool job runs on: Ubuntu 24.04 like GitHub's ubuntu-latest,
# carrying the tools the workflows expect to find there rather than install,
# and a Docker daemon of its own. runner-pool-build puts the runner download
# (actions-runner.tar.gz) next to this file before building.
FROM ubuntu:24.04@sha256:73b999393d07f2bf65a40373778a44dc9a6d54ad7718d29247d8ff9413c4d6a8

ARG NODE_MAJOR=24
ARG NDK_VERSION=r27d
ARG NDK_SHA256=601246087a682d1944e1e16dd85bc6e49560fe8b6d61255be2829178c8ed15d9

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    ImageOS=ubuntu24 \
    AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache \
    RUNNER_TOOL_CACHE=/opt/hostedtoolcache \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    JAVA_HOME_17_X64=/usr/lib/jvm/java-17-openjdk-amd64 \
    ANDROID_NDK=/usr/local/lib/android/ndk/${NDK_VERSION} \
    ANDROID_NDK_HOME=/usr/local/lib/android/ndk/${NDK_VERSION} \
    ANDROID_NDK_ROOT=/usr/local/lib/android/ndk/${NDK_VERSION} \
    ANDROID_NDK_LATEST_HOME=/usr/local/lib/android/ndk/${NDK_VERSION}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The libraries are the ones setup-python's prebuilt CPython links against.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends \
      ca-certificates curl wget gnupg git git-lfs jq unzip zip xz-utils zstd file \
      sudo locales tzdata openssh-client rsync patchelf parallel time \
      build-essential pkg-config cmake clang lld musl-tools \
      libssl-dev libffi-dev libsqlite3-dev zlib1g-dev libbz2-dev liblzma-dev \
      libreadline-dev libncurses-dev tk-dev \
      python3 python3-pip python3-venv python3-dev \
      openjdk-17-jdk-headless \
      docker.io docker-buildx docker-compose-v2 iptables \
 && install --mode 0755 --directory /etc/apt/keyrings \
 && curl --fail --silent --show-error --location \
      --output /etc/apt/keyrings/githubcli.gpg \
      https://cli.github.com/packages/githubcli-archive-keyring.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install --yes --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN base=https://nodejs.org/dist/latest-v${NODE_MAJOR}.x \
 && cd /tmp \
 && curl --fail --silent --show-error --location --remote-name "$base/SHASUMS256.txt" \
 && tarball=$(grep --only-matching --extended-regexp 'node-v[0-9.]+-linux-x64\.tar\.xz' SHASUMS256.txt | head -n 1) \
 && curl --fail --silent --show-error --location --remote-name "$base/$tarball" \
 && grep " $tarball\$" SHASUMS256.txt | sha256sum --check --quiet \
 && tar --extract --xz --file "$tarball" --directory /usr/local --strip-components 1 --no-same-owner \
 && rm -f "$tarball" SHASUMS256.txt

RUN cd /tmp \
 && curl --fail --silent --show-error --location --output ndk.zip \
      "https://dl.google.com/android/repository/android-ndk-${NDK_VERSION}-linux.zip" \
 && echo "${NDK_SHA256}  ndk.zip" | sha256sum --check --quiet \
 && mkdir -p /usr/local/lib/android/ndk \
 && unzip -q ndk.zip -d /usr/local/lib/android/ndk \
 && mv "/usr/local/lib/android/ndk/android-ndk-${NDK_VERSION}" "${ANDROID_NDK_HOME}" \
 && rm ndk.zip

# uid 1001 is the runner user on GitHub's machines too; files a job leaves in
# a mounted volume keep the same owner either way.
RUN useradd --create-home --uid 1001 --shell /bin/bash runner \
 && usermod --append --groups docker runner \
 && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner \
 && chmod 0440 /etc/sudoers.d/runner \
 && install --directory --owner runner --group runner /opt/hostedtoolcache

COPY actions-runner.tar.gz /tmp/actions-runner.tar.gz
RUN install --directory --owner runner --group runner /home/runner/actions-runner \
 && tar --extract --gzip --file /tmp/actions-runner.tar.gz --directory /home/runner/actions-runner \
 && chown -R runner:runner /home/runner/actions-runner \
 && apt-get update \
 && /home/runner/actions-runner/bin/installdependencies.sh \
 && rm /tmp/actions-runner.tar.gz \
 && rm -rf /var/lib/apt/lists/*

USER runner
ENV PATH=/home/runner/.cargo/bin:${PATH}
RUN cd /tmp \
 && curl --fail --silent --show-error --location --remote-name \
      https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init \
 && echo "$(curl --fail --silent --show-error --location \
      https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init.sha256 \
      | cut -d ' ' -f 1)  rustup-init" | sha256sum --check --quiet \
 && chmod +x rustup-init \
 && ./rustup-init -y --no-modify-path --profile minimal --default-toolchain stable \
      --component clippy,rustfmt \
 && rm rustup-init

USER root
COPY --chmod=0755 entrypoint.sh /usr/local/bin/runner-pool-entrypoint
# Docker's storage must not sit on the container's own layered filesystem.
# Being an anonymous volume, it is deleted with the container after each job.
VOLUME /var/lib/docker
ENTRYPOINT ["/usr/local/bin/runner-pool-entrypoint"]
