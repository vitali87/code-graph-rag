#!/usr/bin/env bash
# Starts this job's own Docker daemon, then the runner as the unprivileged
# runner user. The runner takes one job and exits, and the container with it.
set -euo pipefail

jit_config=${RUNNER_JIT_CONFIG:?RUNNER_JIT_CONFIG is not set}
unset RUNNER_JIT_CONFIG

# Nested containers need cgroup controllers delegated to them, which the
# kernel refuses while processes sit in the root cgroup: move them all into
# a child first (what docker:dind's own entrypoint does).
if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
  mkdir -p /sys/fs/cgroup/init
  xargs --no-run-if-empty --max-args 1 </sys/fs/cgroup/cgroup.procs \
    >/sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
  sed -e 's/ / +/g' -e 's/^/+/' </sys/fs/cgroup/cgroup.controllers \
    >/sys/fs/cgroup/cgroup.subtree_control
fi
mount --make-rshared / 2>/dev/null || true

dockerd_args=(--host=unix:///var/run/docker.sock --group=docker)
if [[ -n ${DOCKER_MIRROR-} ]]; then
  dockerd_args+=(--registry-mirror="$DOCKER_MIRROR" --insecure-registry="${DOCKER_MIRROR#http://}")
fi
dockerd "${dockerd_args[@]}" >/var/log/dockerd.log 2>&1 &
for _ in $(seq 60); do
  docker version >/dev/null 2>&1 && break
  sleep 1
done
if ! docker version >/dev/null 2>&1; then
  echo "Docker did not start:" >&2
  cat /var/log/dockerd.log >&2
  exit 1
fi

cd /home/runner/actions-runner
exec setpriv --reuid=runner --regid=runner --init-groups \
  env HOME=/home/runner USER=runner LOGNAME=runner \
  ./run.sh --jitconfig "$jit_config"
