"""Marshal's client side runs on a base ``remembra`` install (httpx, bcrypt, slowapi; no extras), which is
what ``pipx run --spec 'remembra>=0.16.1' remembra-relay doctor`` gets.

The probe runs in a subprocess where every package outside the base install fails to import, then
drives the doctor (including the rate-limit wording, which reads the plan catalog), help, setup and
connect's to-do list against a fake HOME.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.marshal_fixtures import FakeHome

SRC = str(Path(__file__).resolve().parents[1] / "src")
NOT_IN_BASE = (
    "structlog",
    "pydantic",
    "pydantic_settings",
    "fastapi",
    "starlette",
    "uvicorn",
    "openai",
    "anthropic",
    "mcp",
    "aiosqlite",
    "qdrant_client",
    "tiktoken",
    "yaml",
    "jwt",
    "pyotp",
    "qrcode",
    "email_validator",
    "multipart",
    "ulid",
)

PROBE = """
import importlib.abc, io, json, sys, contextlib

BLOCKED = set(%r)

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {name!r} (not in a base install)", name=name)
        return None

sys.meta_path.insert(0, Blocker())

from remembra.marshal import tools
from remembra.marshal import diagnosis
from remembra.relay import cli

out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = cli.main(["doctor", "--no-server", "--format", "json"])
report = json.loads(out.getvalue())
out = io.StringIO()
with contextlib.redirect_stdout(out):
    connect_code = cli.main(["connect", "--agent", "codex", "--relay-command", "/opt/bin/remembra-relay"])
help_answer = tools.help_payload("Does Free cover 4 machines?")
setup = tools.setup_payload(["codex"], os_id="macos", shell="zsh")
fixture = json.load(open(%r))
case = next(c for c in fixture["cases"] if c["expect"].get("code") == "CODEX_TRUST_MISSING")
verdict = diagnosis.diagnose_agent(diagnosis.DiagnosisInput(
    agent_id=case["input"]["agent_id"],
    keys=[diagnosis.KeyEvidence.from_mapping(k) for k in case["input"]["keys"]],
    trail=case["input"]["trail"],
    agent_trail=case["input"]["agent_trail"],
    summary_agent=None,
    now=diagnosis.parse_server_time(fixture["now"]),
    server_url=fixture["server_url"],
))
loaded = sorted({m.split(".")[0] for m in sys.modules} & BLOCKED)
print(json.dumps({
    "doctor_exit": code,
    "finding_ids": sorted({f["id"] for f in report["findings"]}),
    "evidence": [e for f in report["findings"] for e in f["evidence"]],
    "connect_exit": connect_code,
    "connect_out": out.getvalue(),
    "help_status": help_answer["answer_status"],
    "free_keys": [p["api_keys"] for p in help_answer["plan_facts"] if p["tier"] == "free"],
    "setup_steps": len(setup["steps"]),
    "loaded": loaded,
    "verdict": verdict.as_dict(),
}))
"""
FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "marshal" / "diagnosis_cases.json")


def test_the_client_side_runs_on_a_base_install(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    fh.hooks("codex")
    fh.queue("codex", "s-429", error="HTTP 429: relay rate limit exceeded", status=429)
    proc = subprocess.run(
        [sys.executable, "-c", PROBE % (NOT_IN_BASE, FIXTURE)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": SRC, "HOME": str(fh.home), "PATH": str(fh.bin)},
        timeout=90,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["loaded"] == []
    assert result["doctor_exit"] == 1
    assert {"CODEX_TRUST_MISSING", "OUTBOX_QUEUED"} <= set(result["finding_ids"])
    assert "Relay Free allows 30 relay events a minute and 300 unenriched writes a day" in result["evidence"]
    assert "You still need to:" in result["connect_out"] and "After --apply: Open Codex Settings > Hooks" in result["connect_out"]
    assert result["help_status"] == "answered" and result["free_keys"] == [3]
    assert result["setup_steps"] >= 5
    # The server port of the "why?" verdict is client-safe too.
    assert result["verdict"]["code"] == "CODEX_TRUST_MISSING" and result["verdict"]["commands"][0] == "/hooks"
