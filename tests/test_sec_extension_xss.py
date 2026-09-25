"""SEC-20: extension content script renders untrusted text without HTML injection.

Runs extension/tests/xss_render_check.cjs in Node + jsdom against the real
content script. Skips (does not pass silently) when Node or jsdom is missing;
set NODE_PATH to a node_modules containing jsdom to run it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECK = REPO / "extension/tests/xss_render_check.cjs"


def _jsdom_available() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    probe = subprocess.run([node, "-e", "require('jsdom')"], capture_output=True, text=True)
    return probe.returncode == 0


@pytest.mark.skipif(not _jsdom_available(), reason="node + jsdom not available (set NODE_PATH)")
def test_content_script_renders_memories_and_errors_as_text():
    proc = subprocess.run(
        ["node", str(CHECK), str(REPO / "extension/content/content.js")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
