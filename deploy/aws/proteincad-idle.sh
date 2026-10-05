#!/bin/sh
# Power this machine off when nothing has needed it for a while.
#
# The app has an idle timer of its own, and in the ordinary course of things
# that one fires first. This is the backstop for every case where it cannot:
# the app crashed, the laptop closed, the browser tab was shut mid-job, the
# network went away. Those are exactly the situations where a GPU instance
# quietly bills for a weekend, so the machine has to be able to decide for
# itself.
#
# `poweroff` here stops the instance rather than destroying it, provided its
# instance-initiated-shutdown-behavior is `stop` -- which is the EC2 default,
# and which proteinCAD checks and complains about at the first wake. Nothing on
# this box is disposable: the root volume holds about 12 GB of downloaded model
# weights.
#
# Installed at /usr/local/bin/proteincad-idle and run every minute by
# proteincad-idle.timer. To stop it while debugging:
#
#     sudo systemctl stop proteincad-idle.timer
#
# though logging in over ssh already suspends it, see below.

set -eu

: "${PROTEINCAD_WORKER_PORT:=8000}"
: "${PROTEINCAD_IDLE_LIMIT:=1800}"
: "${PROTEINCAD_BOOT_GRACE:=1800}"

# Somebody is logged in. They are looking at something, and pulling the machine
# out from under an ssh session is the least useful thing this could do.
if who | grep -q .; then
  exit 0
fi

health=$(curl -fsS --max-time 5 \
  "http://127.0.0.1:${PROTEINCAD_WORKER_PORT}/health" 2>/dev/null || true)

if [ -n "$health" ]; then
  # -1 for busy: a job on the card is never idle, however long it has been
  # since anyone asked about it.
  idle=$(printf '%s' "$health" | python3 -c '
import json, sys
try:
    health = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
print(-1 if health.get("busy") else int(float(health.get("idle", 0))))
' 2>/dev/null || echo 0)
else
  # Not answering. That is a broken worker, not a busy one, and a broken worker
  # is the case this script exists for -- but give a fresh boot time to finish
  # installing or downloading before drawing that conclusion.
  uptime_seconds=$(cut -d. -f1 /proc/uptime)
  if [ "$uptime_seconds" -lt "$PROTEINCAD_BOOT_GRACE" ]; then
    exit 0
  fi
  logger -t proteincad-idle "worker not answering on port ${PROTEINCAD_WORKER_PORT}"
  idle=$PROTEINCAD_IDLE_LIMIT
fi

[ "$idle" -ge 0 ] || exit 0
[ "$idle" -ge "$PROTEINCAD_IDLE_LIMIT" ] || exit 0

logger -t proteincad-idle "idle ${idle}s >= ${PROTEINCAD_IDLE_LIMIT}s, powering off"
exec systemctl poweroff
