#!/bin/bash
# Test script for all CLI commands
# Can be run locally or in CI/CD

set -e  # Exit on first error

# Save original directory
ORIGINAL_DIR=$(pwd)

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
    "vast configuration"
    "vast configuration list"
    "vast configuration generate"
    "vast configuration variation-types"
    "vast configuration variation-points"
    "vast execution"
    "vast execution local"
    "vast execution local run"
    "vast execution local prepare-run"
    "vast execution cluster"
    "vast execution cluster setup"
    "vast execution cluster cleanup"
    "vast execution cluster run"
    "vast execution cluster prepare-run"
    "vast execution cluster download"
    "vast analysis"
    "vast analysis gui"
)

# Test each command
for cmd in "${commands[@]}"; do
    echo "Testing: poetry run $cmd --help"
    poetry run $cmd --help
    exit_code=$?
    
    if [ $exit_code -ne 0 ]; then
        echo "❌ Error: $cmd --help failed with exit code $exit_code"
        exit 1
    else
        echo "✅ $cmd --help succeeded"
    fi
    echo ""
done


cd "$TEMP_DIR"
poetry run --directory "$ORIGINAL_DIR" vast init "$ORIGINAL_DIR/configs/examples/growth_sim/growth_sim.vast"
# poetry run --directory "$ORIGINAL_DIR" vast configuration generate ./test_generated
# poetry run --directory "$ORIGINAL_DIR" vast configuration list
# poetry run --directory "$ORIGINAL_DIR" vast configuration variation-types
# poetry run --directory "$ORIGINAL_DIR" vast configuration variation-points
# poetry run --directory "$ORIGINAL_DIR" vast execution local prepare-run --config test-fixed-values --runs 1 ./test_out


echo "================================="
echo "All tests passed successfully! ✅"
echo "================================="
