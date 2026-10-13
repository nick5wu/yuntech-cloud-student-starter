#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 deploy/db_resources.py "$@"
