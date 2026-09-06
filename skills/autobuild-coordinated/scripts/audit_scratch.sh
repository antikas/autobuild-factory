#!/usr/bin/env bash
# Audit the operating system's temporary directory for files a seat wrote there in the last N hours.
#   audit_scratch.sh [hours] [extra find predicates ...]
# Lists entries newer than N hours (default 2) that are not harness or tooling noise. Any hit is
# reported to the seat that wrote it and recorded against the item; nothing is deleted here.
set -uo pipefail
hours="${1:-2}"; shift || true
tmp="${TMP_AUDIT_ROOT:-${LOCALAPPDATA:+$LOCALAPPDATA/Temp}}"
tmp="${tmp:-/tmp}"
[ -d "$tmp" ] || { echo "FAIL: temporary directory not found: $tmp" >&2; exit 2; }
mins=$(( hours * 60 ))
find "$tmp" -mindepth 1 -maxdepth 1 -regextype posix-extended -mmin "-$mins" \
  ! -name 'claude*' ! -name '*.log' ! -name 'pyright*' ! -name 'codex-*' ! -name 'tmp*.jpg' \
  ! -name 'uv-*.lock' ! -name '.tmp*' ! -name '_antitrack*' ! -regex '.*/[0-9a-f]{8}-[0-9a-f-]+(\.tmp)?' "$@" \
  -printf '%TY-%Tm-%Td %TH:%TM %f\n' 2>/dev/null | sort
