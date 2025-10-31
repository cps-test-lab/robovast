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
    "vast variation"
    "vast variation list"
    "vast variation generate"
    "vast variation types"
    "vast variation points"
    "vast execution"
    "vast execution local"
    "vast execution cluster"
    "vast execution download"
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
