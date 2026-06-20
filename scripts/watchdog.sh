#!/usr/bin/env bash
# Fleet watchdog — monitors all three machines on the LAN.
# Runs on M4 (local). Checks gonktop and M5 via SSH.
# Restarts any process that is dead or whose log has been silent for STALE_SECS.
# Designed to be invoked by cron every 15 minutes — runs one check cycle and exits.
#
# Cron entry (crontab -e):
#   */15 * * * * bash /Users/herren/dev/palimpsest/scripts/watchdog.sh >>/tmp/watchdog.log 2>&1

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"

STALE_SECS=900      # seconds of log silence → considered stuck (1 check cycle = 15 min)

GEMINI_WORKERS=0  # disabled — agy quota exhausted
GEMINI_BATCH=15
GEMINI_CONCURRENCY=4

GONKTOP="herren@192.168.0.58"
M5="herren@192.168.0.63"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Seconds since a LOCAL file was last modified (9999 if missing)
local_age() {
    local f="$1"
    [ -f "$f" ] || { echo 9999; return; }
    echo $(( $(date +%s) - $(stat -f %m "$f") ))
}

# Seconds since a REMOTE file was last modified (9999 if missing/unreachable)
remote_age() {
    local host="$1" f="$2"
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" \
        "python3 -c \"import os,time; print(int(time.time()-os.path.getmtime('$f')))\" 2>/dev/null || echo 9999" \
        2>/dev/null || echo 9999
}

# PIDs of processes matching a pattern on a remote host (empty if none)
remote_pids() {
    local host="$1" pattern="$2"
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" \
        "pgrep -f '$pattern' 2>/dev/null || true" 2>/dev/null || true
}

kill_local() {
    local pattern="$1"
    local pids
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        log "  killing local: $pids"
        echo "$pids" | xargs kill 2>/dev/null || true
        sleep 2
        pids=$(pgrep -f "$pattern" 2>/dev/null || true)
        [ -n "$pids" ] && echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
}

kill_remote() {
    local host="$1" pattern="$2"
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" \
        "pids=\$(pgrep -f '$pattern' 2>/dev/null || true); [ -n \"\$pids\" ] && echo \"\$pids\" | xargs kill 2>/dev/null; sleep 2; pids=\$(pgrep -f '$pattern' 2>/dev/null || true); [ -n \"\$pids\" ] && echo \"\$pids\" | xargs kill -9 2>/dev/null || true" \
        2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Start functions
# ---------------------------------------------------------------------------

start_m4_worker() {
    log "  → starting M4 palimpsest worker"
    source "$HOME/.zprofile" 2>/dev/null || true
    PYTHONUNBUFFERED=1 nohup "$UV" run python -u -m palimpsest.worker --node m4 \
        </dev/null >>/tmp/palimpsest-worker-m4.log 2>&1 &
    log "    PID $!"
}

start_gemini_workers() {
    log "  → starting $GEMINI_WORKERS Gemini workers (batch=$GEMINI_BATCH)"
    source "$HOME/.zprofile" 2>/dev/null || true
    for i in $(seq 1 "$GEMINI_WORKERS"); do
        PYTHONUNBUFFERED=1 nohup "$UV" run python -u \
            "$REPO/scripts/gemini_features_worker.py" \
            --concurrency "$GEMINI_CONCURRENCY" --batch-size "$GEMINI_BATCH" --loop \
            </dev/null >>/tmp/gemini-f${i}.log 2>&1 &
        log "    Gemini $i PID $!"
    done
}

start_gonktop_broker() {
    log "  → starting gonktop broker"
    ssh -o BatchMode=yes "$GONKTOP" \
        'nohup bash -c "cd ~/dev/palimpsest && git pull -q origin main; ~/.local/bin/uv run uvicorn palimpsest.broker:app --host 0.0.0.0 --port 8077" </dev/null >>/tmp/palimpsest-broker.log 2>&1 &' \
        2>/dev/null && log "    launched" || log "    SSH failed"
}

start_gonktop_worker() {
    log "  → starting gonktop palimpsest worker"
    ssh -o BatchMode=yes "$GONKTOP" \
        'nohup bash -c "cd ~/dev/palimpsest && ~/.local/bin/uv run python -m palimpsest.worker --node gonktop" </dev/null >>/tmp/palimpsest-worker.log 2>&1 &' \
        2>/dev/null && log "    launched" || log "    SSH failed"
}

start_m5_worker() {
    log "  → starting M5 palimpsest worker"
    ssh -o BatchMode=yes "$M5" \
        'nohup bash -c "cd ~/dev/palimpsest && ~/.local/bin/uv run python -m palimpsest.worker --node m5" </dev/null >>/tmp/palimpsest-worker-m5.log 2>&1 &' \
        2>/dev/null && log "    launched" || log "    SSH failed"
}


start_harvester_fetch() {
    log "  → starting harvester fetch on gonktop"
    ssh -o BatchMode=yes "$GONKTOP" \
        'nohup bash -c "cd ~/dev/palimpsest && git pull -q origin main && ~/.local/bin/uv run python -m palimpsest.harvester fetch" </dev/null >>/tmp/palimpsest-harvest.log 2>&1 &' \
        2>/dev/null && log "    launched" || log "    SSH failed"
}

# ---------------------------------------------------------------------------
# Check functions  (0 = healthy, 1 = needs restart)
# ---------------------------------------------------------------------------

check_m4_worker() {
    local pids age
    pids=$(pgrep -f "palimpsest.worker.*m4" 2>/dev/null || true)
    age=$(local_age /tmp/palimpsest-worker-m4.log)
    if [ -z "$pids" ]; then log "M4 worker: DEAD"; return 1; fi
    if [ "$age" -gt "$STALE_SECS" ]; then log "M4 worker: STALE (${age}s silent)"; return 1; fi
    log "M4 worker: ok (log ${age}s ago)"
}

check_gemini_workers() {
    [ "$GEMINI_WORKERS" -eq 0 ] && { log "Gemini workers: disabled"; return 0; }
    local pids count worst_age=0 age
    pids=$(pgrep -f "gemini_features_worker" 2>/dev/null || true)
    if [ -z "$pids" ]; then count=0
    else count=$(echo "$pids" | wc -l | tr -d ' '); fi

    # Trim excess workers — keep only the GEMINI_WORKERS youngest PIDs
    if [ "$count" -gt "$GEMINI_WORKERS" ]; then
        local excess keep_pids kill_pids
        # Sort PIDs descending (highest = most recently started), keep first N
        keep_pids=$(echo "$pids" | sort -rn | head -n "$GEMINI_WORKERS")
        kill_pids=$(echo "$pids" | sort -rn | tail -n +$(( GEMINI_WORKERS + 1 )))
        log "Gemini workers: trimming $count → $GEMINI_WORKERS (killing: $(echo $kill_pids | tr '\n' ' '))"
        echo "$kill_pids" | xargs kill 2>/dev/null || true
        count=$GEMINI_WORKERS
        pids=$keep_pids
    fi

    for i in $(seq 1 "$GEMINI_WORKERS"); do
        age=$(local_age "/tmp/gemini-f${i}.log")
        [ "$age" -gt "$worst_age" ] && worst_age=$age
    done

    if [ "$count" -eq 0 ]; then log "Gemini workers: DEAD"; return 1; fi
    if [ "$count" -lt "$GEMINI_WORKERS" ]; then
        log "Gemini workers: only $count/$GEMINI_WORKERS alive"; return 1
    fi
    if [ "$worst_age" -gt "$STALE_SECS" ]; then
        log "Gemini workers: STALE (worst ${worst_age}s)"; return 1
    fi
    log "Gemini workers: ok ($count running, worst log ${worst_age}s ago)"
}

check_gonktop_broker() {
    # Prefer HTTP health check — more reliable than pgrep
    if curl -sf --max-time 5 "http://192.168.0.58:8077/status" >/dev/null 2>&1; then
        local age
        age=$(remote_age "$GONKTOP" /tmp/palimpsest-broker.log)
        log "gonktop broker: ok (HTTP up, log ${age}s ago)"
        return 0
    fi
    log "gonktop broker: DOWN (HTTP unreachable)"
    return 1
}

check_gonktop_worker() {
    local pids age
    pids=$(remote_pids "$GONKTOP" "palimpsest.worker.*gonktop")
    age=$(remote_age "$GONKTOP" /tmp/palimpsest-worker.log)
    if [ -z "$pids" ]; then log "gonktop worker: DEAD"; return 1; fi
    if [ "$age" -gt "$STALE_SECS" ]; then log "gonktop worker: STALE (${age}s)"; return 1; fi
    log "gonktop worker: ok (log ${age}s ago)"
}

check_m5_worker() {
    local pids age
    pids=$(remote_pids "$M5" "palimpsest.worker.*m5")
    age=$(remote_age "$M5" /tmp/palimpsest-worker-m5.log)
    if [ -z "$pids" ]; then log "M5 worker: DEAD"; return 1; fi
    if [ "$age" -gt "$STALE_SECS" ]; then log "M5 worker: STALE (${age}s)"; return 1; fi
    log "M5 worker: ok (log ${age}s ago)"
}


check_harvester() {
    local pids
    pids=$(remote_pids "$GONKTOP" "palimpsest.harvester")
    if [ -z "$pids" ]; then log "harvester: NOT running"; return 1; fi
    log "harvester: ok (PID $pids)"
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

cd "$REPO"
log "=== Fleet watchdog check (stale threshold ${STALE_SECS}s) ==="
log "    Watching: M4 (local) · gonktop (192.168.0.58) · M5 (192.168.0.63)"

# M4 local
check_m4_worker      || { kill_local "palimpsest.worker.*m4"; start_m4_worker; }
check_gemini_workers || { kill_local "gemini_features_worker"; sleep 1; start_gemini_workers; }

# gonktop
check_gonktop_broker || { kill_remote "$GONKTOP" "uvicorn.*broker"; sleep 1; start_gonktop_broker; }
check_gonktop_worker || { kill_remote "$GONKTOP" "palimpsest.worker.*gonktop"; sleep 1; start_gonktop_worker; }

# M5
check_m5_worker || { kill_remote "$M5" "palimpsest.worker.*m5"; sleep 1; start_m5_worker; }

# Harvester — restart if not running
check_harvester || start_harvester_fetch

log "=== check complete ==="
