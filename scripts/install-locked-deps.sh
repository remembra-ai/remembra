#!/bin/sh
# Install Remembra's Python dependencies at the exact versions pinned in
# uv.lock (REL-18), into the active virtualenv ($VIRTUAL_ENV).
#
# usage: install-locked-deps.sh "server cloud encryption rerank"
#
# If the extras pull in PyTorch (rerank -> sentence-transformers), torch is
# installed at the LOCKED version from the CPU-only wheel index, and the CUDA
# runtime wheels (nvidia-*, triton) that the lock resolves for linux/x86_64 are
# skipped. That keeps the reranker at ~+1 GB instead of ~+6 GB.
#
# Requires: uv on PATH, uv.lock + pyproject.toml in the working directory.
set -eu

EXTRAS="${1:?usage: install-locked-deps.sh \"extra1 extra2 ...\"}"
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

set --
for extra in $EXTRAS; do
    set -- "$@" --extra "$extra"
done

uv export --frozen --no-dev --no-emit-project --no-hashes --no-annotate "$@" -o "$WORK/requirements.lock.txt"

TORCH_VERSION="$(sed -nE 's/^torch==([^ ;]+).*/\1/p' "$WORK/requirements.lock.txt")"
if [ -n "$TORCH_VERSION" ]; then
    echo "install-locked-deps: torch==$TORCH_VERSION (CPU wheel from $TORCH_INDEX_URL)"
    uv pip install --no-cache --no-deps --index-url "$TORCH_INDEX_URL" "torch==$TORCH_VERSION"
fi

grep -vE '^(torch|triton|nvidia-)' "$WORK/requirements.lock.txt" > "$WORK/requirements.nogpu.txt"
uv pip install --no-cache --no-deps -r "$WORK/requirements.nogpu.txt"
echo "install-locked-deps: installed $(grep -cE '^[A-Za-z0-9]' "$WORK/requirements.nogpu.txt") locked packages (extras: $EXTRAS)"
