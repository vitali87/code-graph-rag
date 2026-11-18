#!/usr/bin/env bash
# Makes this server part of the runner pool, or brings it in line with
# /etc/runner-pool/pool.env after a change. Run it as root from a copy of
# this folder:
#
#   sudo ./install.sh            set up, or apply a changed pool.env
#   sudo ./install.sh --rebuild  the same, and rebuild the job image now
#
# Re-running it restarts the slots, which cancels any job running here.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export POOL_ETC=/etc/runner-pool
export POOL_LIB=/usr/local/lib/runner-pool
units=/etc/systemd/system
rebuild=no
[[ ${1-} == --rebuild ]] && rebuild=yes

if ((EUID != 0)); then
  echo "Run this as root: sudo $0" >&2
  exit 1
fi
# The job image, the runner and the workflows' labels are all x86-64.
if [[ $(uname -m) != x86_64 ]]; then
  echo "The runner pool needs an x86-64 server; this one is $(uname -m)." >&2
  exit 1
fi
if ! command -v apt-get >/dev/null || ! command -v systemctl >/dev/null; then
  echo "This installer expects Ubuntu or Debian with systemd." >&2
  exit 1
fi

need=()
command -v docker >/dev/null || need+=(docker.io)
command -v jq >/dev/null || need+=(jq)
command -v curl >/dev/null || need+=(curl ca-certificates)
if ((${#need[@]})); then
  apt-get update
  apt-get install --yes --no-install-recommends "${need[@]}"
fi
systemctl enable --now docker

install --mode 0700 --directory "$POOL_ETC" "$POOL_ETC/slots"
if [[ ! -f $POOL_ETC/pool.env ]]; then
  install --mode 0600 "$here/pool.env.example" "$POOL_ETC/pool.env"
  echo "Wrote $POOL_ETC/pool.env. Put your token in it, check REPOS, then run this again."
  exit 0
fi

rm -rf "$POOL_LIB"
install --directory "$POOL_LIB" "$POOL_LIB/image"
install --mode 0644 "$here/bin/lib.sh" "$POOL_LIB/"
install --mode 0755 "$here/bin/runner-pool-slot" "$here/bin/runner-pool-switch" \
  "$here/bin/runner-pool-build" "$POOL_LIB/"
install --mode 0644 "$here/image/Dockerfile" "$POOL_LIB/image/"
install --mode 0755 "$here/image/entrypoint.sh" "$POOL_LIB/image/"
install --mode 0644 "$here"/systemd/* "$units/"

# shellcheck source=infra/runner-pool/bin/lib.sh
. "$POOL_LIB/lib.sh"
load_config

# A token without the right permissions fails here, with a clear message,
# rather than later in a slot's log.
while read -r repo; do
  if ! gh_api GET "/repos/$repo/actions/runners?per_page=1" >/dev/null; then
    echo "The token cannot manage runners on $repo: it needs \"Administration: Read and write\" there." >&2
    exit 1
  fi
  if ! gh_api GET "/repos/$repo/actions/variables?per_page=1" >/dev/null; then
    echo "The token cannot change variables on $repo: it needs \"Variables: Read and write\" there." >&2
    exit 1
  fi
done < <(unique_repos)

cores=$(nproc)
memory_gb=$(awk '/^MemTotal:/ { print int($2 / 1048576) }' /proc/meminfo)
if [[ ${SLOTS:-auto} == auto ]]; then
  slots=$((cores / 2 < memory_gb / 4 ? cores / 2 : memory_gb / 4))
  ((slots >= 1)) || slots=1
else
  slots=$SLOTS
fi
if [[ ! $slots =~ ^[1-9][0-9]*$ ]]; then
  echo "SLOTS must be auto or a whole number, not \"$slots\"." >&2
  exit 1
fi
# Each job is promised its share of memory and may borrow what the others
# leave idle, up to all but 1 GB of the server, so one hungry job cannot
# take the whole server down.
slot_memory=$((memory_gb / slots))
((slot_memory >= 1)) || slot_memory=1
memory_cap=$((memory_gb - 1))
((memory_cap >= slot_memory)) || memory_cap=$slot_memory

bridge=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
umask 077
cat >"$POOL_ETC/generated.env" <<EOF
# Written by install.sh from pool.env and this server's size; edit pool.env.
SLOT_MEMORY_GB=$slot_memory
MEMORY_CAP_GB=$memory_cap
MIRROR_ADDR=$bridge:5000
EOF
: >"$POOL_ETC/mirror.env"
if [[ -n ${DOCKERHUB_USERNAME-} && -n ${DOCKERHUB_TOKEN-} ]]; then
  printf 'REGISTRY_PROXY_USERNAME=%s\nREGISTRY_PROXY_PASSWORD=%s\n' \
    "$DOCKERHUB_USERNAME" "$DOCKERHUB_TOKEN" >"$POOL_ETC/mirror.env"
fi

systemctl daemon-reload
systemctl stop runner-pool.target 2>/dev/null || true
rm -f "$units/runner-pool.target.wants/runner-pool-slot@"*.service "$POOL_ETC/slots/"*.env
declare -A share=()
for ((slot = 1; slot <= slots; slot++)); do
  repo=${REPO_LIST[(slot - 1) % ${#REPO_LIST[@]}]}
  echo "SLOT_REPO=$repo" >"$POOL_ETC/slots/$slot.env"
  share[$repo]=$((${share[$repo]-0} + 1))
  systemctl enable --quiet "runner-pool-slot@$slot.service"
done
systemctl enable --quiet runner-pool.target runner-pool-mirror.service runner-pool-build.timer
if [[ ${RUN_SWITCH:-yes} == yes ]]; then
  systemctl enable --quiet runner-pool-switch.service
else
  systemctl disable --quiet runner-pool-switch.service 2>/dev/null || true
fi

if [[ $rebuild == yes ]] || ! docker image inspect "$POOL_IMAGE" >/dev/null 2>&1; then
  echo "Building the job image. The first build takes a while."
  "$POOL_LIB/runner-pool-build"
fi

systemctl start runner-pool-build.timer runner-pool.target

echo
echo "This server now runs up to $slots jobs at once:"
for repo in "${!share[@]}"; do
  echo "  ${share[$repo]} for $repo"
done
echo "Switch: ${RUN_SWITCH:-yes}. Follow along with: journalctl --follow --unit 'runner-pool-*'"
