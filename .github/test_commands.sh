#!/bin/bash
# Test script for all CLI commands
# Can be run locally or in CI/CD

set -e  # Exit on first error

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

echo "================================="
echo "All tests passed successfully! ✅"
echo "================================="
