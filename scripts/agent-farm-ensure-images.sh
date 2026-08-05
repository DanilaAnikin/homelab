#!/usr/bin/env bash
# Self-heal: když chybí on-demand image agent-farmy (prune/disk cleanup je občas
# smaže i přes farm.keep), dorebuildí je — jinak farma jede naprázdno (404 no such image).
set -uo pipefail
APP=/srv/homelab/compose/agent-farm/app
cd "$APP" || exit 1
ensure() {
  local name="$1" df="$2"
  if ! docker image inspect "$name" >/dev/null 2>&1; then
    echo "$(date -Is) [ensure-images] $name CHYBÍ → rebuild"
    docker build -f "$df" -t "$name" . && echo "$(date -Is) [ensure-images] $name OK"
  fi
}
ensure agent-farm-worker:latest infra/docker/worker.Dockerfile
ensure agent-farm-judge:latest infra/docker/judge-runner.Dockerfile
