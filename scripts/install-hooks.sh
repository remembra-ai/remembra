#!/bin/bash
#
# Install Remembra git hooks
# Run this after cloning the repo: ./scripts/install-hooks.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
HOOKS_DIR="$REPO_DIR/.git/hooks"
SOURCE_HOOK="$REPO_DIR/hooks/pre-commit"

echo "Installing Remembra git hooks..."

# Check if hooks source exists
if [ ! -f "$SOURCE_HOOK" ]; then
    echo "Error: hooks/pre-commit not found"
    echo "Make sure you're running this from the repo root"
    exit 1
fi

# Install pre-commit hook
cp "$SOURCE_HOOK" "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"

echo "✅ Pre-commit hook installed!"
echo ""
echo "The hook will:"
echo "  - Block commits with banned words in .md files"
echo "  - Block commits with wrong maintainer name in pyproject.toml"
echo ""
echo "This is a HARD GATE for code quality."
