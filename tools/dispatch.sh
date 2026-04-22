#!/bin/bash
# dispatch.sh — thin wrapper around dispatch.py for fast CLI access.
#
# Usage:
#     ./tools/dispatch.sh stt
#     ./tools/dispatch.sh status --notes "pre-show check"
#     ./tools/dispatch.sh autocue --dry-run
#
# Run from anywhere in the repo; script resolves its own location.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 tools/dispatch.py "$@"
