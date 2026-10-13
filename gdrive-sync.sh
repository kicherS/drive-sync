#!/usr/bin/env bash
# Compatibility entry point for running directly from the source checkout.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/drive_sync.py" "$@"
