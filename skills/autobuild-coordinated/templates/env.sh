#!/usr/bin/env bash
# Campaign environment: sourced by every seat before every command. Copy per campaign and fill in.
# Every temporary and cache path points at the scratch root on the build drive; nothing lands in the
# operating system's temporary directory.
SCRATCH_ROOT="<scratch-root>"
export TMPDIR="$SCRATCH_ROOT/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
export PIP_CACHE_DIR="$SCRATCH_ROOT/cache/pip"
export XDG_CACHE_HOME="$SCRATCH_ROOT/cache"
export UV_CACHE_DIR="$SCRATCH_ROOT/cache/uv"
export NPM_CONFIG_CACHE="$SCRATCH_ROOT/cache/npm"
export PYTHONPYCACHEPREFIX="$SCRATCH_ROOT/cache/pycache"
export PYTEST_ADDOPTS="--basetemp=$SCRATCH_ROOT/tmp/pytest"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR" "$UV_CACHE_DIR" "$PYTHONPYCACHEPREFIX"
# Validator prerequisites the project declares (interpreter, package caches, reference checkouts) go here.
