#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="${W03_CONFIG:-.local/w03-config.json}"
RESOURCES="${W03_RESOURCES:-.local/resources.json}"
MODE="${1:-delete}"

test -f "$CONFIG"
test -f "$RESOURCES"
case "$MODE" in
  --stop) python3 deploy/w03_resources.py down --config "$CONFIG" --resources "$RESOURCES" --stop ;;
  delete) python3 deploy/w03_resources.py down --config "$CONFIG" --resources "$RESOURCES" ;;
  *) printf 'usage: %s [--stop|delete]\n' "$0" >&2; exit 2 ;;
esac