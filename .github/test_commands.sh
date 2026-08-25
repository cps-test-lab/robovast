#!/bin/bash
# Test script for all CLI commands
# Can be run locally or in CI/CD

set -e  # Exit on first error

# Save original directory
ORIGINAL_DIR=$(pwd)

# `make venv` is the only install that carries every distribution in this repo: the CLI is
# split across robovast, robovast-client and robovast-cluster, so `poetry install` -- which
# names none of the siblings -- yields a `vast` missing whole command groups. This script
# used to run under `poetry run` and could therefore only ever see part of the surface it
# claims to test.
if [ ! -f "$ORIGINAL_DIR/venv/bin/activate" ]; then
    echo "❌ No venv found. Run 'make venv' first."
    exit 1
fi
# shellcheck source=/dev/null
. "$ORIGINAL_DIR/venv/bin/activate"

# Create temporary directory for tests
TEMP_DIR=$(mktemp -d)
echo "Created temporary directory: $TEMP_DIR"

# Cleanup function to remove temp directory on exit
cleanup() {
    echo "Cleaning up temporary directory..."
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

echo "================================="
echo "Testing CLI Commands"
echo "================================="
echo ""

# Array of commands to test
commands=(
    "vast"
    "vast init"
    "vast config"
    "vast config list"
    "vast config generate"
    "vast config variation-types"
    "vast config variation-points"
    "vast exec"
    "vast exec local"
    "vast exec local run"
    "vast exec local prepare-run"
    "vast exec cluster"
    "vast exec cluster setup"
    "vast exec cluster cleanup"
    "vast exec cluster run"
    "vast exec cluster monitor"
    "vast exec cluster upgrade"
    "vast results"
    "vast results download"
    "vast results postprocess"
    "vast results publish"
    "vast image"
    "vast workspace"
    "vast files"
    "vast doctor"
    "vast wait"
)

# Test each command
for cmd in "${commands[@]}"; do
    echo "Testing: $cmd --help"
    # Checked inline rather than through $? afterwards: `set -e` above already aborts the
    # script on a non-zero exit, so the old `if [ $exit_code -ne 0 ]` branch was unreachable
    # and the failure message it holds could never print.
    if ! $cmd --help; then
        echo "❌ Error: $cmd --help failed"
        exit 1
    fi
    echo "✅ $cmd --help succeeded"
    echo ""
done


cd "$TEMP_DIR"
vast init "$ORIGINAL_DIR/configs/examples/growth_sim/growth_sim.vast"
vast config validate
vast config info
vast config list
vast config variation-types
vast config generate ./test_generated
# No `vast config variation-points` here: it reads the variation points out of the
# scenario files, and growth_sim -- the example this initializes -- is a plain simulation
# with no scenario at all, so the command correctly fails with "No scenario file found".


echo "================================="
echo "All tests passed successfully! ✅"
echo "================================="
