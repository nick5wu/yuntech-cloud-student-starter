#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="${W03_CONFIG:-.local/w03-config.json}"
RESOURCES="${W03_RESOURCES:-.local/resources.json}"
COMMIT="${1:-HEAD}"
USER_DATA=".local/w03-user-data.sh"

test -f "$CONFIG"
mkdir -p .local
DEPLOY_COMMIT="$(git rev-parse --verify --end-of-options "${COMMIT}^{commit}")"
rm -f "$USER_DATA"
bash deploy/make-user-data.sh "$DEPLOY_COMMIT" "$USER_DATA"
python3 deploy/w03_resources.py up --config "$CONFIG" --resources "$RESOURCES" --commit "$DEPLOY_COMMIT" --user-data "$USER_DATA"