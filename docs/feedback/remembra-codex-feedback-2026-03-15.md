# Remembra Feedback And Codex Setup Redesign

Date: March 15, 2026
Agent: Codex

## Executive Summary

Remembra is solving a real problem. Cross-tool context loss is one of the most frustrating parts of using multiple AI systems, and shared memory across agents is a legitimate product direction. The value proposition is clear: switch tools without losing the thread.

The current state is promising but not yet trustworthy enough for broad daily use. The core idea is strong. The current weaknesses are setup reliability, failure-mode clarity, and recall precision. If those three improve, the product becomes substantially more compelling.

## Verified Remembra Findings

Query used: `Remembra launch March 11`

What Remembra returned:

1. `Remembra is set to launch on March 11, 2026 at 8 AM EST, which is also Mani's birthday. The launch eve is on March 10, 2026. The launch week is from March 16 to March 23, 2026.`
2. `Remembra got 2 real external users on launch day, March 11, 2026, and has stable infrastructure. It is now at Day 3 post-launch.`
3. `User is positioning themselves in the agent memory space ahead of a launch on March 11, 2026, which ties the agent infrastructure discussion to the launch of Remembra, specifically because it is Mani's birthday.`

My take on the verification:

1. The recall was correct and meaningfully useful.
2. The memory pool does contain real cross-session launch context.
3. The recall payload is still noisy. The relevant memories were good, but the entity output was overly broad and diluted the signal.

## Honest Product Take

The thesis is correct. Shared continuity across agents is a real pain point and copy-paste is a bad substitute for memory. Remembra should lean hard into that positioning instead of generic "AI memory."

The product risk is not that people do not want this. The product risk is trust collapse. If recall is noisy, setup is fragile, or failures are ambiguous, users stop believing the system is dependable. For a memory product, that is fatal.

What is strongest right now:

1. The problem is real and easy to explain.
2. MCP is the right integration surface for cross-tool distribution.
3. Live recall produced correct answers from a shared pool.

What is weakest right now:

1. Setup is too brittle.
2. Tooling does not explain failure modes well enough.
3. Retrieval needs tighter precision and provenance.

## Survey Response

### 1. Verify

Remembra did successfully recall the March 11 launch context. The strongest memory said launch was scheduled for March 11, 2026 at 8 AM EST and tied to Mani's birthday.

### 2. Store Confirmation

This survey should store the memory:

`[CODEX] feedback survey completed March 15, 2026`

### 3. Setup Experience

Difficulty: `8/10`

Biggest blocker during setup:

1. It was hard to tell whether the problem was Remembra itself, DNS resolution, Codex sandbox networking, or local packaging.

What was confusing:

1. The MCP config looked correct but live calls still failed.
2. A local `remembra` CLI existed but one entrypoint was broken, which made installation look worse than it was.
3. There was no single `doctor` flow that said "config is valid, network is blocked" versus "package install is broken" versus "credentials are invalid."

### 4. Top 3 Frustrations

1. Failure mode ambiguity. DNS failure, sandbox denial, and packaging issues all looked like the same generic setup failure.
2. Too many moving parts. MCP server config, env vars, wrapper scripts, and sandbox behavior all had to line up manually.
3. Recall output is not disciplined enough yet. Correct answers were mixed with noisy entities and lower-value context.

### 5. Missing Features I Wish Existed

1. `remembra doctor codex` that checks install, config, auth, DNS, sandbox reachability, and live recall.
2. One-command install for each agent with zero manual config editing.
3. Memory scopes with explicit policies like `private`, `project`, `team`, and `handoff`.
4. Recall provenance UI or CLI output that explains why each memory matched.
5. Conflict and dedupe controls for overlapping cross-agent memories.

### 6. Faster Install

How setup could be reduced to one command:

1. Ship a dedicated Codex installer that installs the correct package, writes the wrapper, patches `~/.codex/config.toml`, runs a health check, and writes a small success report.
2. Hide cloud connectivity behind a local bridge so Codex only needs to talk to `127.0.0.1`.
3. Split packages cleanly so the MCP client install does not drag in unrelated broken entrypoints.

Ideal auto-setup flow:

1. User runs one command like `curl -fsSL https://install.remembra.dev/codex | bash`.
2. Installer asks for API key, project, and user id once.
3. Installer installs the MCP package and local bridge.
4. Installer patches Codex config automatically.
5. Installer runs `health`, `store`, and `recall` smoke tests.
6. Installer prints a single green success state with the exact agent name and shared namespace.

### 7. Cross-Agent Sync

How memory sharing between agents should improve:

1. Agents should auto-write structured handoff summaries at session end.
2. Every memory should include `source_agent`, `scope`, `confidence`, and `time`.
3. Shared memories should support visibility controls, not just a flat global pool.
4. Duplicate or conflicting memories should be merged or surfaced as revisions, not silently stacked.
5. Recall should prefer recent handoff summaries before dumping raw historical matches.

### 8. Top 3 Feature Wishlist

1. One-command install plus `doctor` plus self-healing config repair.
2. Scoped memories and explicit read/write policies per agent and per project.
3. Explainable recall with provenance, ranking reasons, and conflict visibility.

### 9. Overall Rating And First Fix

Overall rating: `6/10`

One thing to fix first:

1. Installation and connection reliability. If setup is not boring and repeatable, the rest of the product does not matter.

## Technical Proposal

### What Went Wrong Today

These were the real failure points from the Codex side:

1. DNS resolution to `api.remembra.dev` failed from this environment.
2. Codex sandbox networking blocked outbound connections until escalated.
3. The configured MCP server was present, but the path from config to successful live recall was not self-validating.
4. The packaged `remembra` CLI entrypoint was broken locally because it imported server-only modules that were not installed in the tool environment.
5. There was no Codex-specific installer or doctor command to separate package issues from network issues.

### How Codex Should Connect To Remembra

Codex should not talk directly to the public Remembra cloud endpoint from inside the sandbox.

It should connect like this:

1. A user-level local bridge daemon runs outside the sandbox on `127.0.0.1:8765`.
2. Codex MCP connects only to the local bridge.
3. The local bridge owns outbound cloud connectivity, retries, DNS workarounds, auth, and health checks.
4. Codex config only contains local connection details plus non-secret identifiers like project and user id.
5. Secrets stay in the bridge environment, not scattered across multiple agent configs.

This design improves four things at once:

1. It removes repeated sandbox networking failures.
2. It simplifies the Codex config surface.
3. It avoids storing the cloud API key in multiple places.
4. It creates one place for diagnostics and retries.

### Packaging Changes I Recommend

1. Split the package into clear install targets:
   - `remembra-mcp` for MCP client/server tooling
   - `remembra-client` for SDK
   - `remembra-server` for full FastAPI server
2. Do not ship a broken `remembra` entrypoint in an environment that only installed MCP dependencies.
3. Add `remembra doctor codex`, `remembra doctor claude`, and `remembra doctor gemini`.

## Actual Code And Config

### 1. One-command Codex Installer

```bash
#!/usr/bin/env bash
set -euo pipefail

REMEMBRA_CLOUD_URL="${REMEMBRA_CLOUD_URL:-https://api.remembra.dev}"
REMEMBRA_API_KEY="${REMEMBRA_API_KEY:?REMEMBRA_API_KEY is required}"
REMEMBRA_PROJECT="${REMEMBRA_PROJECT:?REMEMBRA_PROJECT is required}"
REMEMBRA_USER_ID="${REMEMBRA_USER_ID:?REMEMBRA_USER_ID is required}"
REMEMBRA_HOST_IP="${REMEMBRA_HOST_IP:-}"

command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv tool install --upgrade "remembra[mcp]"

mkdir -p "$HOME/.local/bin" "$HOME/.remembra" "$HOME/.codex"

cat > "$HOME/.remembra/bridge.env" <<EOF
REMEMBRA_URL=$REMEMBRA_CLOUD_URL
REMEMBRA_API_KEY=$REMEMBRA_API_KEY
REMEMBRA_PROJECT=$REMEMBRA_PROJECT
REMEMBRA_USER_ID=$REMEMBRA_USER_ID
REMEMBRA_HOST_IP=$REMEMBRA_HOST_IP
EOF

cat > "$HOME/.local/bin/remembra-bridge" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. "$HOME/.remembra/bridge.env"
set +a
exec python3 "$HOME/.remembra/remembra_bridge.py"
EOF
chmod +x "$HOME/.local/bin/remembra-bridge"

cat > "$HOME/.local/bin/remembra-codex-mcp" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export REMEMBRA_URL="${REMEMBRA_URL:-http://127.0.0.1:8765}"
export REMEMBRA_PROJECT="${REMEMBRA_PROJECT:?REMEMBRA_PROJECT is required}"
export REMEMBRA_USER_ID="${REMEMBRA_USER_ID:?REMEMBRA_USER_ID is required}"
exec "$HOME/.local/bin/remembra-mcp" "$@"
EOF
chmod +x "$HOME/.local/bin/remembra-codex-mcp"

cat > "$HOME/.remembra/remembra_bridge.py" <<'EOF'
#!/usr/bin/env python3
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

UPSTREAM = os.environ["REMEMBRA_URL"].rstrip("/")
API_KEY = os.environ["REMEMBRA_API_KEY"]
FORCE_IP = os.environ.get("REMEMBRA_HOST_IP", "").strip()

if FORCE_IP:
    real_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == "api.remembra.dev":
            host = FORCE_IP
        return real_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched_getaddrinfo

client = httpx.Client(
    base_url=UPSTREAM,
    headers={"X-API-Key": API_KEY},
    timeout=30.0,
)

class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self):
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length) if length else None
        headers = {}
        content_type = self.headers.get("content-type")
        if content_type:
            headers["content-type"] = content_type

        response = client.request(
            method=self.command,
            url=self.path,
            content=body,
            headers=headers,
        )

        payload = response.content
        self.send_response(response.status_code)
        self.send_header("content-type", response.headers.get("content-type", "application/json"))
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _forward
    do_POST = _forward
    do_PATCH = _forward
    do_DELETE = _forward

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), Proxy).serve_forever()
EOF
chmod +x "$HOME/.remembra/remembra_bridge.py"

python3 <<'PY'
from pathlib import Path
import os
import re

cfg_path = Path.home() / ".codex" / "config.toml"
cfg_path.parent.mkdir(parents=True, exist_ok=True)
text = cfg_path.read_text() if cfg_path.exists() else ""

block = f'''
[mcp_servers.remembra]
command = "{Path.home() / ".local/bin/remembra-codex-mcp"}"

[mcp_servers.remembra.env]
REMEMBRA_URL = "http://127.0.0.1:8765"
REMEMBRA_PROJECT = "{os.environ["REMEMBRA_PROJECT"]}"
REMEMBRA_USER_ID = "{os.environ["REMEMBRA_USER_ID"]}"
'''.strip()

pattern = re.compile(r'(?ms)^\\[mcp_servers\\.remembra\\].*?(?=^\\[|\\Z)')
if pattern.search(text):
    text = pattern.sub(block + "\\n", text)
else:
    text = text.rstrip() + "\\n\\n" + block + "\\n"

cfg_path.write_text(text)
PY

if [ "$(uname)" = "Darwin" ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$HOME/Library/LaunchAgents/dev.remembra.bridge.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.remembra.bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/.local/bin/remembra-bridge</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/.remembra/bridge.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.remembra/bridge.stderr.log</string>
</dict>
</plist>
EOF
  launchctl unload "$HOME/Library/LaunchAgents/dev.remembra.bridge.plist" >/dev/null 2>&1 || true
  launchctl load "$HOME/Library/LaunchAgents/dev.remembra.bridge.plist"
fi

echo "Remembra Codex setup complete."
echo "Bridge: http://127.0.0.1:8765"
echo "Project: $REMEMBRA_PROJECT"
echo "User: $REMEMBRA_USER_ID"
```

### 2. Codex Config Result

This is what Codex should end up with:

```toml
[mcp_servers.remembra]
command = "/Users/your-user/.local/bin/remembra-codex-mcp"

[mcp_servers.remembra.env]
REMEMBRA_URL = "http://127.0.0.1:8765"
REMEMBRA_PROJECT = "clawdbot"
REMEMBRA_USER_ID = "user_XdEOi0CkNGvePP8tS4MZ6w"
```

### 3. Future Better Option

If Codex supports remote MCP URLs directly, the better long-term model is:

```toml
[mcp_servers.remembra]
url = "https://api.remembra.dev/mcp"
bearer_env = "REMEMBRA_API_KEY"
```

That would eliminate the local wrapper entirely. Until that is supported cleanly, the local bridge is the safer Codex path.

## How To Handle Sandbox Restrictions Automatically

This should be automatic, not user-driven.

Recommended behavior:

1. During install, start the bridge outside the sandbox.
2. During runtime, Codex talks only to `127.0.0.1`.
3. If the bridge cannot reach cloud, the bridge returns structured diagnostics:
   - `dns_failure`
   - `auth_failure`
   - `upstream_unreachable`
   - `project_scope_error`
4. `remembra doctor codex` should test both the local bridge and the upstream cloud and print exact remediation steps.
5. If DNS is bad, the bridge should optionally support a pinned IP via `REMEMBRA_HOST_IP` without requiring the user to edit `/etc/hosts`.

## Priority Fixes

If I were sequencing this product work, I would do it in this order:

1. Ship a dedicated one-command installer and doctor flow for Codex, Claude, and Gemini.
2. Split packaging so the MCP install path is minimal and cannot expose broken entrypoints.
3. Introduce bridge-based connectivity for sandboxed agents.
4. Tighten recall output with better ranking, provenance, and entity filtering.
5. Add scoped memory policies and automatic cross-agent handoff summaries.

## Bottom Line

Remembra is not a fake problem looking for a product. The problem is real and the direction is legitimate.

The most important thing now is not adding more impressive memory features. It is making setup boring, failure modes obvious, and recall trustworthy. If those three improve, the product becomes something people will depend on instead of merely experiment with.
