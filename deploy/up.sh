#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="${W03_CONFIG:-.local/w03-config.json}"
RESOURCES="${W03_RESOURCES:-.local/resources.json}"
COMMIT="${1:-HEAD}"
USER_DATA="/tmp/w03-user-data-$$.sh"

if [[ -e "$USER_DATA" ]]; then
	printf 'Refusing to overwrite existing user data file: %s\n' "$USER_DATA" >&2
	exit 1
fi
trap 'rm -f -- "$USER_DATA"' EXIT

test -f "$CONFIG"
DEPLOY_COMMIT="$(git rev-parse --verify --end-of-options "${COMMIT}^{commit}")"
bash deploy/make-user-data.sh "$DEPLOY_COMMIT" "$USER_DATA"
python3 deploy/w03_resources.py up --config "$CONFIG" --resources "$RESOURCES" --commit "$DEPLOY_COMMIT" --user-data "$USER_DATA"