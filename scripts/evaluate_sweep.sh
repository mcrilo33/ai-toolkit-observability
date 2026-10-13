#!/usr/bin/env bash
# Keep evaluation scores current: evaluate_sweep.sh [install|uninstall]
#   (no argument)  one sweep: evaluate.py over the sessions of the last 7 days of this repo and
#                  ai-toolkit, logged to ~/.ai-toolkit/evaluate-sweep.log; exits with its status
#   install        load the daily launchd agent (launchd/<label>.plist) for this checkout
#   uninstall      unload and remove it
# evaluate.py upserts, so a re-run, an overlap or a missed day is harmless. It reads the Langfuse
# keys from the Keychain itself and bounds every network call, so a stack that is down fails the
# sweep quickly instead of hanging it. Nothing secret is written to the agent or the log.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo="$(dirname "$here")"
die() { printf '%s: %s\n' "${0##*/}" "$*" >&2; exit 1; }

label=com.ai-toolkit.evaluate-sweep
window_days=7
# The host repo scored besides this one: telemetry name=owner/repo=checkout (see evaluate.py --repo).
toolkit_repo="ai-toolkit=mcrilo33/ai-toolkit=$HOME/Repos/ai-toolkit"
log="$HOME/.ai-toolkit/evaluate-sweep.log"
agent="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

usage() { echo "usage: ${0##*/} [install|uninstall]" >&2; exit 2; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# The date `window_days` ago, UTC (evaluate.py reads a bare date as midnight UTC): BSD date, then GNU.
since_date() {
  date -u -v-"${window_days}"d +%Y-%m-%d 2> /dev/null || date -u -d "$window_days days ago" +%Y-%m-%d
}

sweep() {
  local since status=0
  since="$(since_date)"
  echo "$(stamp) sweep start since=$since"
  python3 "$here/evaluate.py" --since "$since" --repo "$toolkit_repo" || status=$?
  if [ "$status" -eq 0 ]; then
    echo "$(stamp) sweep done"
  else
    echo "$(stamp) sweep failed (exit $status)"
  fi
  return "$status"
}

# bootout can return before the service is gone, and a bootstrap right after it fails with an I/O error.
wait_unloaded() {
  local tries=0
  while launchctl print "$domain/$label" > /dev/null 2>&1 && [ "$tries" -lt 20 ]; do
    tries=$((tries + 1))
    sleep 0.5
  done
}

install() {
  case "$repo" in
    *[\&\<\>\|\\\"]*) die "the checkout path contains a character the agent file cannot hold: $repo" ;;
  esac
  mkdir -p "$(dirname "$agent")" "$(dirname "$log")"
  # A loaded agent must be unloaded before its file is replaced; "not loaded" is not an error.
  launchctl bootout "$domain/$label" 2> /dev/null || true
  wait_unloaded
  sed -e "s|__SCRIPT__|$here/evaluate_sweep.sh|" -e "s|__REPO__|$repo|" \
    "$repo/launchd/$label.plist" > "$agent"
  launchctl bootstrap "$domain" "$agent" || die "launchctl could not load $agent"
  launchctl print "$domain/$label" > /dev/null 2>&1 || die "$label is not loaded after install"
  echo "installed $label: daily sweep, log $log"
}

uninstall() {
  launchctl bootout "$domain/$label" 2> /dev/null || true
  wait_unloaded
  rm -f "$agent"
  ! launchctl print "$domain/$label" > /dev/null 2>&1 || die "$label is still loaded after uninstall"
  echo "uninstalled $label"
}

case "${1:-}" in
  install | uninstall) [ "$#" -eq 1 ] || usage; "$1"; exit ;;
  "") ;;
  *) usage ;;
esac

export USER="${USER:-$(id -un)}" # the Keychain lookup in evaluate.py needs it; launchd may not set it
mkdir -p "$(dirname "$log")"
cd "$repo"
sweep 2>&1 | tee -a "$log"
