#!/bin/bash
#
# Install Remembra git hooks
# Run this after cloning the repo: ./scripts/install-hooks.sh
#
# - pre-commit: banned words in .md files, maintainer name in pyproject.toml,
#               a Remembra API key (rem_...) in any staged file
# - pre-push:   refuses a push that would publish private material
#               (scripts/ci/repo_hygiene.py: docs/audits/, *-fixlog.md, ...)
#
# The hooks go into the repository's shared hooks folder, so they cover every
# worktree of this clone. Re-run after pulling a change to hooks/ or to
# scripts/ci/repo_hygiene.py.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Works in a linked worktree too, where .git is a file, not a folder, and
# follows core.hooksPath when it is set.
HOOKS_DIR="$(cd "$REPO_DIR" && git rev-parse --git-path hooks)"
case "$HOOKS_DIR" in
    /*) ;;
    *) HOOKS_DIR="$REPO_DIR/$HOOKS_DIR" ;;
esac
mkdir -p "$HOOKS_DIR"

echo "Installing Remembra git hooks into $HOOKS_DIR ..."

for hook in pre-commit pre-push; do
    if [ ! -f "$REPO_DIR/hooks/$hook" ]; then
        echo "Error: hooks/$hook not found"
        echo "Make sure you're running this from the repo root"
        exit 1
    fi
    cp "$REPO_DIR/hooks/$hook" "$HOOKS_DIR/$hook"
    chmod +x "$HOOKS_DIR/$hook"
done

# The pre-push hook runs the checker from the working tree; this copy guards
# pushes from branches that predate it.
cp "$REPO_DIR/scripts/ci/repo_hygiene.py" "$HOOKS_DIR/repo_hygiene.py"

echo "✅ Hooks installed!"
echo ""
echo "The hooks will:"
echo "  - Block commits with banned words in .md files"
echo "  - Block commits with wrong maintainer name in pyproject.toml"
echo "  - Block commits that carry a Remembra API key (rem_...)"
echo "  - Block pushes that would publish private material (docs/audits/, *-fixlog.md, ...)"
echo ""
echo "This is a HARD GATE for code quality."
