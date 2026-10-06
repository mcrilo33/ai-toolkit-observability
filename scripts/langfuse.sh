#!/usr/bin/env bash
# Self-hosted Langfuse lifecycle: langfuse.sh up|down|status (docker compose, ../compose.yaml).
# Every secret is generated once and kept in the macOS Keychain (service ai-toolkit-langfuse-<NAME>);
# it reaches docker through the environment only: never a file, a command line or this script's output.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
die() { printf '%s: %s\n' "${0##*/}" "$*" >&2; exit 1; }
warn() { printf '%s: warning: %s\n' "${0##*/}" "$*" >&2; }

project=ai-toolkit-langfuse
account="${USER:-$(id -un)}"
health_url=http://127.0.0.1:3000/api/public/health
# Docker's VM reports a bit less than it is given (a 16 GB setting reads 15.6 GiB), so warn below 15 GiB
min_mem_bytes=$((15 * 1024 * 1024 * 1024))
secrets=(
  LANGFUSE_INIT_PROJECT_PUBLIC_KEY LANGFUSE_INIT_PROJECT_SECRET_KEY LANGFUSE_INIT_USER_PASSWORD
  POSTGRES_PASSWORD CLICKHOUSE_PASSWORD REDIS_AUTH MINIO_ROOT_PASSWORD ENCRYPTION_KEY SALT NEXTAUTH_SECRET
)
dc() { docker compose -p "$project" -f "$here/../compose.yaml" "$@"; }

read_secret() { security find-generic-password -a "$account" -s "$project-$1" -w 2> /dev/null || true; }

# `security -i` reads the command from stdin (printf is a builtin), so the value never shows in a process list.
save_secret() {
  printf 'add-generic-password -a %s -s %s -w %s\n' "$account" "$project-$1" "$2" | security -i > /dev/null ||
    die "could not store $project-$1 in the Keychain"
  [ "$(read_secret "$1")" = "$2" ] || die "could not store $project-$1 in the Keychain"
}

generate_secret() {
  case "$1" in
    LANGFUSE_INIT_PROJECT_PUBLIC_KEY) echo "pk-lf-$(uuidgen | tr '[:upper:]' '[:lower:]')" ;;
    LANGFUSE_INIT_PROJECT_SECRET_KEY) echo "sk-lf-$(uuidgen | tr '[:upper:]' '[:lower:]')" ;;
    ENCRYPTION_KEY | NEXTAUTH_SECRET) openssl rand -hex 32 ;;
    SALT) openssl rand -hex 16 ;;
    *) openssl rand -hex 24 ;;
  esac
}

# $1 = create: fill in missing secrets (first `up`). $1 = placeholder: `down` only needs compose to interpolate.
export_secrets() {
  local name value volumes missing=()
  for name in "${secrets[@]}"; do [ -n "$(read_secret "$name")" ] || missing+=("$name"); done
  if [ "${#missing[@]}" -gt 0 ] && [ "$1" = create ]; then
    volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=$project")" || die "docker is not reachable"
    if [ -n "$volumes" ]; then
      die "Langfuse data volumes exist but ${#missing[@]} secret(s) are missing from the Keychain (service $project-*);" \
        "the datastores would reject new passwords. Restore the Keychain items, or remove the volumes to start over."
    fi
    for name in "${missing[@]}"; do save_secret "$name" "$(generate_secret "$name")"; done
  fi
  for name in "${secrets[@]}"; do
    value="$(read_secret "$name")"
    export "$name=${value:-unused}"
  done
}

warn_if_low_memory() {
  local mem
  mem="$(docker info --format '{{.MemTotal}}' 2> /dev/null || true)"
  case "$mem" in '' | *[!0-9]*) return 0 ;; esac
  [ "$mem" -ge "$min_mem_bytes" ] ||
    warn "Docker has $((mem / 1024 / 1024 / 1024)) GB of memory; the Langfuse docs recommend 16 GB for this stack"
}

is_up() { curl -fs --max-time 5 -o /dev/null "$health_url"; }

case "${1:-}" in
  up)
    warn_if_low_memory
    export_secrets create
    dc up -d
    for _ in $(seq 1 90); do
      if is_up; then echo up; exit 0; fi
      sleep 5
    done
    die "the UI did not answer on $health_url within 7.5 minutes; see: docker compose -p $project logs langfuse-web" ;;
  down)
    export_secrets placeholder
    dc down ;;
  status)
    if is_up; then echo up; else echo down; exit 1; fi ;;
  *) echo "usage: ${0##*/} up|down|status" >&2; exit 2 ;;
esac
