"""AGT-2 / AGT-10 / AGT-11 / AGT-12: the Clawdbot plugin and clawd bootstrap hook.

Both TypeScript files run under Node's built-in type stripping against a local
HTTP proxy to the production API routes (ASGI TestClient over real SQLite).
The only stand-in is a tiny ``@sinclair/typebox`` shim (schema builders only)
so the plugin loads without its npm dependency.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.agent_api_harness import build_api, row, seed

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "clawdbot-plugin" / "index.ts"
HANDLER = ROOT / "integrations" / "clawd-hooks" / "session-recall" / "handler.ts"
NODE = shutil.which("node")


def _node_supports_type_stripping() -> bool:
    if not NODE:
        return False
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    major, minor = (int(x) for x in out.split(".")[:2])
    return (major, minor) >= (22, 6)


pytestmark = pytest.mark.skipif(not _node_supports_type_stripping(), reason="needs node >= 22.6 (type stripping)")

TYPEBOX_SHIM = """
const opt = Symbol.for("optional");
export const Type = {
  Object: (properties, extra = {}) => ({ type: "object", properties, ...extra }),
  String: (extra = {}) => ({ type: "string", ...extra }),
  Number: (extra = {}) => ({ type: "number", ...extra }),
  Boolean: (extra = {}) => ({ type: "boolean", ...extra }),
  Array: (items, extra = {}) => ({ type: "array", items, ...extra }),
  Optional: (schema) => ({ ...schema, [opt]: true }),
};
"""

PLUGIN_DRIVER = """
import register from "./index.ts";
const calls = JSON.parse(process.argv[2]);
const tools = {};
const warnings = [];
register({
  config: { plugins: { entries: { remembra: { config: JSON.parse(process.env.PLUGIN_CFG) } } } },
  logger: { info() {}, warn(m) { warnings.push(m); }, error() {} },
  registerTool(t) { tools[t.name] = t; },
});
const results = [];
for (const [name, params] of calls) {
  const r = await tools[name].execute("call", params);
  results.push(JSON.parse(r.content[0].text));
}
console.log(JSON.stringify({ tools: Object.keys(tools).sort(), warnings, results }));
"""

HANDLER_DRIVER = """
import handler from "./handler.ts";
const event = { type: "agent", action: "bootstrap", context: { bootstrapFiles: [{ path: "AGENTS.md", content: "x" }] } };
await handler(event);
console.log(JSON.stringify(event.context.bootstrapFiles));
"""


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


@pytest.fixture()
def proxy(api):
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            seen.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "json": json.loads(body) if body else None,
                }
            )
            upstream = api["http"].request(self.command, self.path, content=body, headers={"Content-Type": "application/json"})
            payload = upstream.content
            self.send_response(upstream.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = do_DELETE = _forward  # noqa: N815

        def log_message(self, *args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{server.server_port}", "seen": seen, "api": api}
    server.shutdown()
    server.server_close()


@pytest.fixture()
def node_dir(tmp_path):
    work = tmp_path / "node"
    (work / "node_modules" / "@sinclair" / "typebox").mkdir(parents=True)
    (work / "node_modules" / "@sinclair" / "typebox" / "package.json").write_text(
        json.dumps({"name": "@sinclair/typebox", "type": "module", "main": "index.js"})
    )
    (work / "node_modules" / "@sinclair" / "typebox" / "index.js").write_text(TYPEBOX_SHIM)
    (work / "package.json").write_text(json.dumps({"type": "module"}))
    shutil.copy(PLUGIN, work / "index.ts")
    shutil.copy(HANDLER, work / "handler.ts")
    (work / "plugin_driver.mjs").write_text(PLUGIN_DRIVER)
    (work / "handler_driver.mjs").write_text(HANDLER_DRIVER)
    return work


def _plugin(node_dir: Path, cfg: dict[str, Any], calls: list[list[Any]]) -> dict[str, Any]:
    result = subprocess.run(
        [NODE, "--experimental-strip-types", "--no-warnings", "plugin_driver.mjs", json.dumps(calls)],
        cwd=node_dir,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PLUGIN_CFG": json.dumps(cfg)},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _cfg(proxy: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"apiUrl": proxy["url"], "apiKey": "rem_plugin_test", "projectId": "clawbot", "agentId": "clawdbot", **extra}


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


def test_plugin_registers_inbox_session_and_space_tools(proxy, node_dir):
    out = _plugin(node_dir, _cfg(proxy), [])
    for name in (
        "remembra_session_brief",
        "remembra_status_set",
        "remembra_status_list",
        "remembra_inbox_get",
        "remembra_inbox_send",
        "remembra_inbox_ack",
        "remembra_spaces_list",
        "remembra_spaces_create",
        "remembra_timeline",
    ):
        assert name in out["tools"]
    assert out["warnings"] == []


def test_plugin_store_stamps_provenance_and_resolves_project_alias(proxy, node_dir):
    cfg = _cfg(proxy, projectId="ClawdBot", projectAliases={"clawdbot": "clawbot"})
    out = _plugin(node_dir, cfg, [["remembra_store", {"content": "Chose Coolify for deploys", "metadata": {"topic": "ops"}}]])
    stored = out["results"][0]
    assert stored["status"] == "stored"
    db_row = row(proxy["api"], stored["id"])
    meta = json.loads(db_row["metadata"])
    assert db_row["project_id"] == "clawbot"
    assert meta["agent_id"] == "clawdbot" and meta["source"] == "clawdbot-plugin" and meta["topic"] == "ops"
    assert meta["client_version"].startswith("clawdbot-plugin/")
    assert proxy["seen"][0]["headers"]["x-api-key"] == "rem_plugin_test"


def test_plugin_session_brief_inbox_status_round_trip(proxy, node_dir):
    codex = proxy["api"]["make_client"](project="clawbot", agent_id="codex")
    sent = codex.send_to_inbox(to_agent="clawdbot", subject="rotate key", body="Rotate the OPS-2 key " * 20)
    out = _plugin(
        node_dir,
        _cfg(proxy),
        [
            ["remembra_store", {"content": "[SESSION END] plugin handoff", "memory_type": "handoff"}],
            ["remembra_status_set", {"key": "deploy:api", "value": "pushed"}],
            ["remembra_status_set", {"key": "deploy:api", "value": "live"}],
            ["remembra_session_brief", {}],
            ["remembra_inbox_get", {"summary": True}],
            ["remembra_inbox_ack", {"inbox_id": sent["inbox_id"], "result": "done", "note": "rotated"}],
            ["remembra_inbox_get", {}],
            ["remembra_inbox_send", {"to_agent": "claude_code", "subject": "s", "body": "b"}],
            ["remembra_status_list", {}],
        ],
    )
    handoff, s1, s2, brief, inbox_summary, ack, inbox_after, send, status_list = out["results"]
    assert s2["superseded"] == [s1["memory_id"]]
    assert brief["handoff"]["id"] == handoff["id"]
    assert brief["inbox"]["unread_count"] == 1
    assert [s["value"] for s in brief["status_items"]] == ["live"]
    assert inbox_summary["items"][0]["body_preview"].endswith("...") and "body" not in inbox_summary["items"][0]
    assert ack["status"] == "done"
    assert inbox_after["count"] == 0
    assert send["from_agent"] == "clawdbot" and send["warnings"]
    assert [i["value"] for i in status_list["items"]] == ["live"]


def test_plugin_forget_all_is_guarded(proxy, node_dir):
    seed(proxy["api"], "c1", "clawbot one", datetime(2026, 1, 1), project_id="clawbot")
    seed(proxy["api"], "c2", "clawbot two", datetime(2026, 1, 2), project_id="clawbot")
    seed(proxy["api"], "o1", "other", datetime(2026, 1, 3), project_id="other")
    out = _plugin(
        node_dir,
        _cfg(proxy),
        [
            ["remembra_forget", {"all": True}],
            ["remembra_forget", {"all": True, "project_id": "clawbot"}],
            ["remembra_forget", {"all": True, "project_id": "clawbot", "dry_run": False, "confirm": "yes"}],
            ["remembra_forget", {"entity": "Alice"}],
            [
                "remembra_forget",
                {"all": True, "project_id": "clawbot", "dry_run": False, "confirm": "DELETE ALL MEMORIES IN clawbot"},
            ],
            ["remembra_timeline", {}],
            ["remembra_timeline", {"project_id": "other"}],
        ],
    )
    no_project, preview, wrong, entity, done, after, other = out["results"]
    assert no_project["status"] == "error"
    assert preview["status"] == "dry_run" and preview["would_delete"] == 2
    assert wrong["status"] == "dry_run" and "nothing was deleted" in wrong["error"]
    assert entity["status"] == "not_supported"
    assert done["status"] == "deleted"
    assert after["total"] == 0 and other["total"] == 1
    deletes = [r for r in proxy["seen"] if r["method"] == "DELETE"]
    assert len(deletes) == 1 and "project_id=clawbot" in deletes[0]["path"] and "user_id" not in deletes[0]["path"]


def test_plugin_timeline_list_and_spaces(proxy, node_dir):
    for day in (1, 10, 20):
        seed(proxy["api"], f"d{day}", f"day {day}", datetime(2026, 5, day), project_id="clawbot")
    out = _plugin(
        node_dir,
        _cfg(proxy),
        [
            ["remembra_timeline", {"start_date": "2026-05-05", "end_date": "2026-05-15"}],
            ["remembra_list", {"limit": 2}],
            ["remembra_list", {"limit": 2, "offset": 2}],
            ["remembra_spaces_create", {"name": "fleet"}],
            ["remembra_spaces_list", {}],
        ],
    )
    timeline, page1, page2, created, spaces = out["results"]
    assert [m["id"] for m in timeline["memories"]] == ["d10"] and timeline["total"] == 1
    assert [m["id"] for m in page1["memories"]] == ["d20", "d10"] and page1["next_offset"] == 2
    assert [m["id"] for m in page2["memories"]] == ["d1"] and page2["next_offset"] is None
    assert created["status"] == "created"
    assert spaces["count"] == 1 and spaces["spaces"][0]["id"] == created["id"]


def test_plugin_ingest_sends_options_nested(proxy, node_dir):
    _plugin(
        node_dir,
        _cfg(proxy),
        [["remembra_ingest", {"messages": [{"role": "user", "content": "hi"}], "min_importance": 0.8, "store": False}]],
    )
    req = next(r for r in proxy["seen"] if r["path"] == "/api/v1/ingest/conversation")
    assert req["json"]["options"] == {"min_importance": 0.8, "extract_from": "both", "store": False}
    assert "min_importance" not in req["json"]


def test_plugin_warns_without_agent_id(proxy, node_dir):
    cfg = _cfg(proxy)
    del cfg["agentId"]
    out = _plugin(node_dir, cfg, [["remembra_health", {}]])
    assert any("agentId not set" in w for w in out["warnings"])
    assert out["results"][0]["agent_id"] == "clawdbot"


# ---------------------------------------------------------------------------
# clawd bootstrap hook
# ---------------------------------------------------------------------------


def _handler(node_dir: Path, env: dict[str, str]) -> list[dict[str, Any]]:
    result = subprocess.run(
        [NODE, "--experimental-strip-types", "--no-warnings", "handler_driver.mjs"],
        cwd=node_dir,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(node_dir), "REMEMBRA_HOOK_TIMEOUT_MS": "3000", **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_clawd_hook_injects_real_brief_first(proxy, node_dir):
    api = proxy["api"]
    seed(api, "h1", "[SESSION END] clawd handoff text", datetime(2026, 9, 25, 8, 0), project_id="clawbot", memory_type="handoff")
    api["make_client"](project="clawbot", agent_id="codex").send_to_inbox(to_agent="clawdbot", subject="check OPS-4", body="b")
    files = _handler(
        node_dir,
        {
            "REMEMBRA_URL": proxy["url"],
            "REMEMBRA_API_KEY": "rem_x",
            "REMEMBRA_PROJECT": "clawdbot",
            "REMEMBRA_PROJECT_ALIASES": "clawdbot=clawbot",
        },
    )
    assert [f["path"] for f in files] == ["_SESSION_BRIEF.md", "AGENTS.md"]
    content = files[0]["content"]
    assert "project: clawbot, agent: clawdbot" in content
    assert "[SESSION END] clawd handoff text" in content
    assert "## Inbox: 1 unread" in content and "check OPS-4" in content
    assert "agent_id=clawdbot" in proxy["seen"][0]["path"]


def test_clawd_hook_reads_plugin_config_file(proxy, node_dir):
    cfg_file = node_dir / "clawdbot.json"
    cfg_file.write_text(json.dumps({"plugins": {"entries": {"remembra": {"config": _cfg(proxy)}}}}))
    files = _handler(node_dir, {"REMEMBRA_HOOK_CLAWDBOT_CONFIG": str(cfg_file)})
    assert "project: clawbot, agent: clawdbot" in files[0]["content"]
    assert proxy["seen"][0]["headers"]["x-api-key"] == "rem_plugin_test"


def test_clawd_hook_falls_back_when_server_down(node_dir):
    files = _handler(node_dir, {"REMEMBRA_URL": "http://127.0.0.1:9", "REMEMBRA_API_KEY": "rem_x"})
    assert files[0]["path"] == "_SESSION_BRIEF.md"
    assert "unavailable" in files[0]["content"] and "remembra_session_brief" in files[0]["content"]
