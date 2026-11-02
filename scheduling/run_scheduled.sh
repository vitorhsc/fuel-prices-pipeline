#!/bin/bash
# Runs the pipeline on a schedule (installed by `make schedule`, started by macOS launchd).
#
# launchd starts jobs with a bare environment: no terminal, a minimal PATH and maybe no
# Docker Desktop running yet. This script takes care of that, then calls `make run`.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOCK_DIR="$PROJECT_DIR/logs/.run.lock"
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | scheduler | $*"; }
notify() { osascript -e "display notification \"$1\" with title \"Fuel prices pipeline\"" >/dev/null 2>&1 || true; }

cd "$PROJECT_DIR" || exit 1
mkdir -p logs

# 1. One run at a time: skip if the previous run is still going (e.g. a slow API).
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -f "$LOCK_DIR/pid" ] && kill -0 "$(cat "$LOCK_DIR/pid")" 2>/dev/null; then
    log "another run is still in progress (pid $(cat "$LOCK_DIR/pid")), skipping this one"
    exit 0
  fi
  log "removing a lock left by a run that did not finish"
  rm -rf "$LOCK_DIR" && mkdir "$LOCK_DIR"
fi
echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# 2. Docker Desktop must be running.
if ! docker info >/dev/null 2>&1; then
  log "Docker is not running, starting Docker Desktop"
  open -a Docker
  for _ in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 5
  done
  if ! docker info >/dev/null 2>&1; then
    log "Docker did not start within 5 minutes, giving up"
    notify "Docker did not start: the run was skipped."
    exit 1
  fi
fi

# 3. Run the pipeline.
log "starting make run"
make run
status=$?
if [ "$status" -eq 0 ]; then
  log "finished: success"
else
  log "finished: exit code $status (see meta.pipeline_runs for details)"
  notify "A run failed (exit code $status). See logs/scheduled.log."
fi
exit "$status"
