#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 deploy/iot_resources.py "$@"
