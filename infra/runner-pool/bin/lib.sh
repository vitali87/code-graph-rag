# shellcheck shell=bash
# Shared by the runner-pool commands: where things live and how they talk to
# GitHub. Sourced, never run.

# The label every pool runner carries and the repository variable the switch
# writes. Workflows name both, so changing either here means changing every
# workflow that sends jobs to the pool. Used by the scripts that source this.
# shellcheck disable=SC2034
POOL_LABEL=own-linux
# shellcheck disable=SC2034
SWITCH_VARIABLE=OWN_RUNNERS_FREE

POOL_ETC=${POOL_ETC:-/etc/runner-pool}
POOL_LIB=${POOL_LIB:-/usr/local/lib/runner-pool}
POOL_IMAGE=${POOL_IMAGE:-runner-pool:latest}
GITHUB_API=${GITHUB_API:-https://api.github.com}

log() { printf '%s\n' "$*" >&2; }

load_config() {
  # shellcheck source=/dev/null
  . "$POOL_ETC/pool.env"
  if [[ -f $POOL_ETC/generated.env ]]; then
    # shellcheck source=/dev/null
    . "$POOL_ETC/generated.env"
  fi
  if [[ -z ${GITHUB_TOKEN-} ]]; then
    log "GITHUB_TOKEN is empty in $POOL_ETC/pool.env"
    return 1
  fi
  read -r -a REPO_LIST <<<"${REPOS-}"
  if ((${#REPO_LIST[@]} == 0)); then
    log "REPOS is empty in $POOL_ETC/pool.env"
    return 1
  fi
}

# Each repository once, in the order REPOS first names it.
unique_repos() {
  local -A seen=()
  local repo
  for repo in "${REPO_LIST[@]}"; do
    if [[ -z ${seen[$repo]-} ]]; then
      seen[$repo]=1
      printf '%s\n' "$repo"
    fi
  done
}

# gh_api METHOD PATH [JSON]: prints the response body. Returns 0 below HTTP
# 400, 4 on 404 (callers that create-on-missing test for it) and 1 otherwise.
# The token goes to curl on stdin, so it never shows in the process list.
gh_api() {
  local method=$1 path=$2 body=${3-}
  local args=(--silent --show-error --max-time 30 --request "$method"
    --header "Accept: application/vnd.github+json"
    --header "X-GitHub-Api-Version: 2022-11-28"
    --write-out '\n%{http_code}' --config -)
  if [[ -n $body ]]; then
    args+=(--header "Content-Type: application/json" --data "$body")
  fi
  local out code
  out=$(printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" |
    curl "${args[@]}" "$GITHUB_API$path") || return 1
  code=${out##*$'\n'}
  out=${out%$'\n'*}
  printf '%s' "$out"
  if ((code == 404)); then
    return 4
  fi
  if ((code >= 400)); then
    log "GitHub answered $code to $method $path: $out"
    return 1
  fi
}
