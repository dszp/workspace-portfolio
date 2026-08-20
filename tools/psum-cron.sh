#!/usr/bin/env bash
# Nightly psum refresh — example wrapper for cron.
#
# Ordering matters: scan produces the facts describe reads, and describe writes
# the descriptions index renders. Running index before describe publishes a
# report missing the descriptions generated seconds later.
#
# Cost: scan and index make no model calls at all. describe is gated on the
# prompt's own inputs, so an untouched workspace makes zero calls too — which is
# the property that makes this safe to run every night.
#
# Install:
#   cp tools/psum-cron.sh ~/.local/bin/psum-cron && chmod +x ~/.local/bin/psum-cron
#   crontab -e
#   30 5,17 * * * $HOME/.local/bin/psum-cron      # twice daily, 12 hours apart
#
# Twice a day rather than nightly because the question this answers — what did I
# leave half-finished — has a half-life measured in hours, not days: a morning
# run reflects yesterday, an evening run reflects today. It costs no more, since
# a second pass over an unchanged workspace makes no model calls at all.
set -uo pipefail

# cron gets a minimal PATH and none of your shell profile. psum and claude both
# live in ~/.local/bin on a normal install; git and python3 come from /usr/bin.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Point this at your data directory. Unset, psum uses the code root.
export PSUM_HOME="${PSUM_HOME:-$HOME/workspace/MISC/project-summary}"

LOG="${PSUM_CRON_LOG:-$HOME/.local/state/psum/cron.log}"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "=== $(date -Is) ==="

# A busy lock (exit 75) means a run is already in progress — not an error worth
# shouting about, and tomorrow's run picks it up.
psum scan     || { echo "scan failed ($?), stopping"; exit 0; }
psum describe || echo "describe failed ($?), continuing"
psum index    || echo "index failed ($?)"

# Keep the log from growing without bound.
if [ "$(wc -l < "$LOG")" -gt 2000 ]; then
  tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
