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
    "list_variants"
    "execute_local"
    "cluster_execution"
    "generate_variants"
)

# Test each command
for cmd in "${commands[@]}"; do
    echo "Testing: $cmd --help"
    poetry run "$cmd" --help
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
