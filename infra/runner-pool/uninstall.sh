#!/usr/bin/env bash
# Takes this server out of the runner pool: stops its slots (cancelling any
# job running here), removes its runners from GitHub and deletes what
# install.sh put in place. /etc/runner-pool, which holds the token, stays
# unless you pass --purge.
set -euo pipefail

units=/etc/systemd/system

if ((EUID != 0)); then
  echo "Run this as root: sudo $0" >&2
  exit 1
fi

# Stopping the slots removes their runners from GitHub; stopping the switch
# leaves "false" behind if no other server has a runner free.
systemctl stop runner-pool.target runner-pool-build.timer 2>/dev/null || true
systemctl disable --quiet runner-pool.target runner-pool-switch.service \
  runner-pool-mirror.service runner-pool-build.timer 2>/dev/null || true
rm -f "$units/runner-pool.target.wants/"*.service
rmdir "$units/runner-pool.target.wants" 2>/dev/null || true
rm -f "$units"/runner-pool-slot@.service "$units"/runner-pool-switch.service \
  "$units"/runner-pool-mirror.service "$units"/runner-pool-build.service \
  "$units"/runner-pool-build.timer "$units"/runner-pool.target
systemctl daemon-reload

docker rm --force runner-pool-mirror >/dev/null 2>&1 || true
docker volume rm runner-pool-mirror >/dev/null 2>&1 || true
docker image rm runner-pool:latest >/dev/null 2>&1 || true
rm -rf /usr/local/lib/runner-pool

if [[ ${1-} == --purge ]]; then
  rm -rf /etc/runner-pool
  echo "Removed the runner pool and its settings from this server."
else
  echo "Removed the runner pool from this server. Settings remain in /etc/runner-pool."
fi
