#!/usr/bin/env bash
# Usage: scripts/make_checkpoint.sh <label>
# Builds a clean recovery archive of the project into CHECKPOINTS/, writes its SHA-256,
# then VERIFIES it (sha256 -c, unzip -t, member count). Exits non-zero on any failure.
set -euo pipefail
LABEL="${1:?label required, e.g. CP01_phaseB_closed_month}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
TS="$(date -u +%Y%m%dT%H%M%SZ)"; NAME="alahd_erp_improved_${LABEL}_${TS}"
mkdir -p CHECKPOINTS
ZIP="CHECKPOINTS/${NAME}.zip"
( cd .. && zip -qr "$ROOT/$ZIP" "$(basename "$ROOT")" \
  -x '*/__pycache__/*' '*.pyc' '*/.git/*' '*/CHECKPOINTS/*.zip' '*.db' '*.db-wal' '*.db-shm' \
     '*/.mypy_cache/*' '*/.ruff_cache/*' '*/node_modules/*' '*/build/*' '*/dist/*' '*.egg-info/*' )
( cd CHECKPOINTS && sha256sum "${NAME}.zip" > "${NAME}.zip.sha256" && sha256sum -c "${NAME}.zip.sha256" )
unzip -tq "$ZIP" >/dev/null && echo "unzip -t OK"
echo "members: $(unzip -l "$ZIP" | tail -1)"
echo "ARCHIVE=$ZIP"; echo "SHA256=$(cut -d' ' -f1 "CHECKPOINTS/${NAME}.zip.sha256")"
