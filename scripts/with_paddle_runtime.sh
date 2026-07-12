#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDORED_LIB="$ROOT_DIR/.vendor/libgomp1/usr/lib/x86_64-linux-gnu"

if [[ -d "$VENDORED_LIB" ]]; then
  export LD_LIBRARY_PATH="$VENDORED_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

export FLAGS_use_mkldnn="${FLAGS_use_mkldnn:-0}"
export PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT="${PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT:-False}"

exec "$@"
