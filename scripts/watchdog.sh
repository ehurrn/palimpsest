#!/usr/bin/env bash
# Watchdog for M4 local workers.
# Checks every CHECK_INTERVAL seconds whether each managed process is alive
# AND has written to its log recently. Kills+restarts anything that is dead
# or has been silent for STALE_SECS seconds.
#
# Usage:
#   bash scripts/watchdog.sh          # run in foreground
#   nohup bash scripts/watchdog.sh </dev/null >>/tmp/watchdog.log 2>&1 &

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
CHECK_INTERVAL=90   # seconds between checks
STALE_SECS=300      # seconds of log silence → considered stuck

GEMINI_WORKERS=3
GEMINI_BATCH=15

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

# Find PIDs matching a grep pattern
pids_of() { pgrep -f "$1" 2>/dev/null || true; }

# Seconds since a file was last modified (9999 if missing)
age_of() {
    local f="$1"
    if [ ! -f "$f" ]; then echo 9999; return; fi
    echo $(( $(date +%s) - $(stat -f %m "$f") ))
}

# Kill all processes matching a grep pattern
kill_matching() {
    local pattern="$1"
    local pids
    pids=$(pids_of "$pattern")
    if [ -n "$pids" ]; then
        log "  killing: $pids"
        echo "$pids" | xargs kill 2>/dev/null || true
        sleep 2
        # SIGKILL anything still alive
        pids=$(pids_of "$pattern")
        [ -n "$pids" ] && echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
}

# --------------------------------------------------------------------------
# Worker definitions
# --------------------------------------------------------------------------

start_m4_worker() {
    log "Starting M4 palimpsest worker..."
    source "$HOME/.zprofile" 2>/dev/null || true
    PYTHONUNBUFFERED=1 nohup "$UV" run python -u -m palimpsest.worker --node m4 \
        </dev/null >>/tmp/palimpsest-worker-m4.log 2>&1 &
    log "  PID $!"
}

start_gemini_workers() {
    log "Starting $GEMINI_WORKERS Gemini feature workers (batch=$GEMINI_BATCH)..."
    source "$HOME/.zprofile" 2>/dev/null || true
    for i in $(seq 1 "$GEMINI_WORKERS"); do
        PYTHONUNBUFFERED=1 nohup "$UV" run python -u \
            "$REPO/scripts/gemini_features_worker.py" \
            --concurrency 1 --batch-size "$GEMINI_BATCH" --loop \
            </dev/null >>/tmp/gemini-f${i}.log 2>&1 &
        log "  Gemini worker $i PID $!"
    done
}

# --------------------------------------------------------------------------
# Check functions — return 0 if healthy, 1 if needs restart
# --------------------------------------------------------------------------

check_m4_worker() {
    local pids
    pids=$(pids_of "palimpsest.worker.*m4")
    if [ -z "$pids" ]; then
        log "M4 worker: NOT RUNNING"
        return 1
    fi
    local age
    age=$(age_of /tmp/palimpsest-worker-m4.log)
    if [ "$age" -gt "$STALE_SECS" ]; then
        log "M4 worker: STALE (log silent ${age}s)"
        return 1
    fi
    log "M4 worker: ok (PIDs $pids, log ${age}s ago)"
    return 0
}

check_gemini_workers() {
    local pids count
    pids=$(pids_of "gemini_features_worker")
    if [ -z "$pids" ]; then count=0
    else count=$(echo "$pids" | wc -l | tr -d ' '); fi

    # Check staleness of any live log
    local worst_age=0
    for i in $(seq 1 "$GEMINI_WORKERS"); do
        local age
        age=$(age_of "/tmp/gemini-f${i}.log")
        [ "$age" -gt "$worst_age" ] && worst_age=$age
    done

    if [ "$count" -eq 0 ]; then
        log "Gemini workers: NONE RUNNING"
        return 1
    fi
    if [ "$count" -lt "$GEMINI_WORKERS" ]; then
        log "Gemini workers: only $count/$GEMINI_WORKERS alive — restarting all"
        return 1
    fi
    if [ "$worst_age" -gt "$STALE_SECS" ]; then
        log "Gemini workers: STALE (worst log silent ${worst_age}s)"
        return 1
    fi
    log "Gemini workers: ok ($count running, worst log ${worst_age}s ago)"
    return 0
}

# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

cd "$REPO"
log "=== Watchdog started (check every ${CHECK_INTERVAL}s, stale after ${STALE_SECS}s) ==="

while true; do
    log "--- check ---"

    if ! check_m4_worker; then
        kill_matching "palimpsest.worker.*m4"
        start_m4_worker
    fi

    if ! check_gemini_workers; then
        kill_matching "gemini_features_worker"
        sleep 2
        start_gemini_workers
    fi

    sleep "$CHECK_INTERVAL"
done
