"""remembra-install / remembra-relay polish before 0.16.0.

- a re-run without --url keeps the server already set up (a self-hosted URL is
  never swapped for api.remembra.dev while its key is kept);
- the hidden prompt and --api-key-stdin accept only a rem_ key;
- --remove lists the *.bak-* backups that still hold the key, and
  --delete-backups deletes them;
- remembra-install and remembra-relay answer --version;
- Windsurf is unverified: --all leaves it out, --agent windsurf writes the
  path its docs name;
- printed diffs hide secrets in the remaining common MCP config shapes;
- remembra-bridge accepts --url (older docs) as well as --upstream.

Every command runs in a subprocess with a temporary HOME.
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from remembra import __version__
from remembra.relay.config_view import config_view
from remembra.tools.agents import EXIT_NOT_WRITTEN
from remembra.tools.bridge import build_parser as bridge_parser
from remembra.tools.keyinput import KEY_SHAPE_HINT, looks_like_key, resolve_api_key
from tests.test_install_safety import KEY, OLD_KEY, install

SRC = str(Path(__file__).resolve().parents[1] / "src")
SELF_HOSTED = "https://memory.internal.example:8443"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".cursor").mkdir(parents=True)
    (h / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}))
    return h


def _cursor_env(home: Path) -> dict[str, str]:
    return json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["remembra"]["env"]


# --- the saved server is kept ----------------------------------------------


def test_a_rerun_without_url_keeps_the_self_hosted_server(home: Path) -> None:
    first = install(home, "--agent", "cursor", "--url", SELF_HOSTED, "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert first.returncode == 0, first.stderr
    creds = home / ".remembra" / "credentials"
    assert json.loads(creds.read_text())["url"] == SELF_HOSTED

    # Later: a new key, no --url. The server stays the self-hosted one, everywhere.
    rerun = install(home, "--agent", "cursor", "--apply", env={"REMEMBRA_API_KEY": OLD_KEY})
    assert rerun.returncode == 0, rerun.stderr
    assert f"server {SELF_HOSTED} (from {creds})" in rerun.stdout
    assert "api.remembra.dev" not in rerun.stdout
    assert json.loads(creds.read_text())["url"] == SELF_HOSTED
    assert _cursor_env(home)["REMEMBRA_URL"] == SELF_HOSTED and _cursor_env(home)["REMEMBRA_API_KEY"] == OLD_KEY

    # Without credentials, an existing remembra entry still names the server.
    creds.unlink()
    again = install(home, "--agent", "cursor", env={"REMEMBRA_API_KEY": OLD_KEY})
    assert f"server {SELF_HOSTED} (from the remembra entry in {home / '.cursor' / 'mcp.json'})" in again.stdout

    # --url and REMEMBRA_URL still win; a first install defaults to Remembra Cloud.
    moved = install(home, "--agent", "cursor", env={"REMEMBRA_API_KEY": KEY, "REMEMBRA_URL": "http://127.0.0.1:8787"})
    assert "server http://127.0.0.1:8787 (from REMEMBRA_URL)" in moved.stdout
    fresh = home.parent / "fresh"
    (fresh / ".cursor").mkdir(parents=True)
    first_time = install(fresh, "--agent", "cursor", env={"REMEMBRA_API_KEY": KEY})
    assert "server https://api.remembra.dev (from default (Remembra Cloud))" in first_time.stdout


# --- the key's shape --------------------------------------------------------


@pytest.mark.parametrize("value", ["hunter2", "Bearer eyJhbGciOi.x.y", "rem_short", "rem_has space in it_0123456789", ""])
def test_a_value_that_is_not_a_rem_key_is_refused(value: str) -> None:
    assert not looks_like_key(value)


def test_the_prompt_asks_again_for_a_wrong_paste_and_never_uses_it(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    creds.write_text(json.dumps({"api_key": OLD_KEY}))
    answers = iter(["my-github-password", f'"{KEY}"'])
    warnings: list[str] = []
    key, source = resolve_api_key(
        cli_key=None,
        from_stdin=False,
        credentials=creds,
        environ={},
        interactive=True,
        prompt=lambda _: next(answers),
        warn=warnings.append,
    )
    assert (key, source) == (KEY, "prompt")  # the second paste, quotes stripped
    assert len(warnings) == 1 and KEY_SHAPE_HINT in warnings[0] and "my-github-password" not in warnings[0]

    # Three wrong pastes: no key at all (the saved one is not silently used instead).
    key, source = resolve_api_key(
        cli_key=None,
        from_stdin=False,
        credentials=creds,
        environ={},
        interactive=True,
        prompt=lambda _: "not-a-key",
        warn=warnings.append,
    )
    assert key is None and KEY_SHAPE_HINT in source


def test_a_wrong_paste_at_the_real_hidden_prompt_writes_nothing(home: Path) -> None:
    """A real pseudo-terminal: two wrong pastes are refused, the third (a real key) is written."""
    master, slave = pty.openpty()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": SRC, "REMEMBRA_API_KEY": ""}
    proc = subprocess.Popen(
        [sys.executable, "-m", "remembra.tools.agents", "--agent", "cursor", "--apply"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        cwd=str(home),
        start_new_session=True,
    )
    os.close(slave)
    seen = b""

    def read_until(marker: bytes, count: int = 1, timeout: float = 20.0) -> None:
        nonlocal seen
        deadline = time.time() + timeout
        while seen.count(marker) < count:
            assert time.time() < deadline, seen.decode(errors="replace")
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                assert chunk, seen.decode(errors="replace")
                seen += chunk

    try:
        read_until(b"Remembra API key (input hidden)")
        os.write(master, b"ghp_notARemembraKeyAtAll123\n")
        read_until(b"Remembra API key (input hidden)", count=2)
        assert b"not a Remembra API key" in seen and b"2 left" in seen
        os.write(master, (KEY + "\n").encode())
        read_until(b"Next: remembra-relay connect")
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    assert b"ghp_notARemembraKeyAtAll123" not in seen and KEY.encode() not in seen
    assert _cursor_env(home)["REMEMBRA_API_KEY"] == KEY


def test_a_piped_value_that_is_not_a_key_is_refused(home: Path) -> None:
    before = (home / ".cursor" / "mcp.json").read_text()
    piped = install(home, "--agent", "cursor", "--apply", "--api-key-stdin", env={"REMEMBRA_API_KEY": ""}, stdin="oops\n")
    assert piped.returncode == 2 and "not a Remembra API key" in piped.stderr
    assert (home / ".cursor" / "mcp.json").read_text() == before and not (home / ".remembra").exists()


# --- backups that hold the key ------------------------------------------------


def test_remove_lists_the_backups_that_hold_the_key_and_can_delete_them(home: Path) -> None:
    assert install(home, "--agent", "cursor", "--apply", env={"REMEMBRA_API_KEY": OLD_KEY}).returncode == 0
    time.sleep(1.1)  # a new backup stamp
    assert install(home, "--agent", "cursor", "--apply", env={"REMEMBRA_API_KEY": KEY}).returncode == 0
    cursor_dir, creds_dir = home / ".cursor", home / ".remembra"
    with_key = sorted(p for p in [*cursor_dir.glob("mcp.json.bak-*"), *creds_dir.glob("credentials.bak-*")])
    # The first backup of mcp.json predates Remembra (no key): it is not listed.
    first_backup = min(cursor_dir.glob("mcp.json.bak-*"))
    assert "rem_" not in first_backup.read_text()
    holding = [p for p in with_key if "rem_" in p.read_text()]
    assert holding, with_key

    dry = install(home, "--remove", "--agent", "cursor")
    assert dry.returncode == EXIT_NOT_WRITTEN
    assert f"{len(holding)} backup file(s) still hold a Remembra API key" in dry.stdout
    assert all(str(p) in dry.stdout for p in holding) and str(first_backup) not in dry.stdout
    assert "--delete-backups" in dry.stdout and KEY not in dry.stdout and OLD_KEY not in dry.stdout

    removed = install(home, "--remove", "--agent", "cursor", "--apply")
    assert removed.returncode == 0, removed.stderr
    # The removal itself kept a backup of the file with the key: it is listed too.
    assert "still hold a Remembra API key and are kept" in removed.stdout
    assert all(p.exists() for p in holding)

    wiped = install(home, "--remove", "--agent", "cursor", "--apply", "--delete-backups")
    assert wiped.returncode == 0, wiped.stderr
    assert all(not p.exists() for p in holding) and first_backup.exists()
    left = [p for p in [*cursor_dir.iterdir(), *creds_dir.iterdir()] if "rem_" in p.read_text()]
    assert left == [creds_dir / "credentials"]  # only the live saved key; ~/.remembra is deleted by hand
    assert "--delete-backups goes with --remove" in install(home, "--all", "--delete-backups").stderr


def test_relay_disconnect_names_the_backups_in_its_uninstall_steps(home: Path) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "remembra.relay.cli", "disconnect"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": SRC},
        timeout=60,
    )
    assert "*.bak-remembra-*" in out.stdout and "--delete-backups" in out.stdout


# --- --version ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "name"), [("remembra.relay.cli", "remembra-relay"), ("remembra.tools.agents", "remembra-install")]
)
def test_the_commands_answer_version(tmp_path: Path, module: str, name: str) -> None:
    out = subprocess.run(
        [sys.executable, "-m", module, "--version"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": SRC},
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"{name} {__version__}"


# --- Windsurf is unverified ---------------------------------------------------------


def test_all_leaves_windsurf_out_and_agent_windsurf_writes_the_documented_path(home: Path) -> None:
    (home / ".codeium" / "windsurf").mkdir(parents=True)
    windsurf = home / ".codeium" / "windsurf" / "mcp_config.json"
    everything = install(home, "--all", "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert everything.returncode == 0, everything.stderr
    assert not windsurf.exists() and not (home / ".windsurf").exists()
    assert "[windsurf] detected, not set up by --all" in everything.stdout
    assert "remembra-install --agent windsurf" in everything.stdout

    one = install(home, "--agent", "windsurf", "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert one.returncode == 0, one.stderr
    assert "Windsurf is unverified" in one.stdout
    entry = json.loads(windsurf.read_text())["mcpServers"]["remembra"]
    assert entry["command"] == "remembra-mcp" and entry["env"]["REMEMBRA_AGENT_ID"] == "windsurf"
    # --remove --all does take it out again.
    assert install(home, "--remove", "--all", "--apply").returncode == 0
    assert not windsurf.exists()  # nothing else was in it


# --- printed diffs ---------------------------------------------------------------


def test_config_view_hides_secrets_in_the_remaining_common_mcp_shapes() -> None:
    zapier = "sk-ak-" + "9f8e7d6c5b4a3928Zq"
    composio = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    smithery = "smithery-cfg-" + "Kq81"
    jsonc_env = "short-" + "val9"
    text = json.dumps(
        {
            "mcpServers": {
                "smithery": {
                    "command": "npx",
                    "args": ["-y", "@smithery/cli@latest", "run", "@x/y", "--config", json.dumps({"apiKey": smithery})],
                },
                "zapier": {"url": f"https://actions.zapier.com/mcp/{zapier}/sse"},
                "composio": {"url": f"https://mcp.composio.dev/composio/server/{composio}/mcp"},
                "pkg": {"command": "npx", "args": ["-y", "@scope/server@1.2.3", "https://example.com/docs/getting-started"]},
            }
        }
    )
    view = config_view(text, Path("mcp.json")) or ""
    for secret in (zapier, composio, smithery):
        assert secret not in view
    assert "@scope/server@1.2.3" in view and "https://example.com/docs/getting-started" in view  # names stay readable
    assert "https://actions.zapier.com/mcp/[hidden]/sse" in view

    # JSON with comments and trailing commas (Cursor, Windsurf, Gemini CLI, VS Code accept it).
    jsonc = '{\n  // my servers\n  "mcpServers": {\n    "z": {"command": "x", "env": {"OTHER": "' + jsonc_env + '"},},\n  },\n}\n'
    shown = config_view(jsonc, Path("mcp.json")) or ""
    assert jsonc_env not in shown and '"OTHER": "[hidden]"' in shown


def test_bridge_accepts_url_as_well_as_upstream() -> None:
    assert bridge_parser().parse_args(["--url", SELF_HOSTED]).upstream == SELF_HOSTED
    assert bridge_parser().parse_args(["--upstream", SELF_HOSTED]).upstream == SELF_HOSTED


# --- setup.md's "Taking it off again" leaves no copy of the key ------------------------------------


def _taken_off(tmp_path: Path, name: str, steps: list[str]) -> list[Path]:
    """Install and connect in a fresh HOME, run ``steps`` (setup.md's uninstall lines), return files still holding KEY."""
    import shlex
    import shutil

    home = tmp_path / name
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    (home / ".claude" / "settings.json").write_text("{}")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "gpt-5"\n')
    installed = install(home, "--all", "--apply", "--api-key-stdin", "--url", "http://127.0.0.1:9", stdin=KEY + "\n")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": SRC}
    relay = [sys.executable, "-m", "remembra.relay.cli"]
    connected = subprocess.run([*relay, "connect", "--apply"], capture_output=True, text=True, env=env, timeout=60)
    assert connected.returncode == 0, connected.stdout + connected.stderr
    assert any(KEY in p.read_text(errors="replace") for p in home.rglob("*") if p.is_file())

    for line in steps:
        words = shlex.split(line)
        if words[0] == "remembra-relay":
            done = subprocess.run([*relay, *words[1:]], capture_output=True, text=True, env=env, timeout=60)
            assert done.returncode == 0, done.stdout + done.stderr
        elif words[0] == "remembra-install":
            done = install(home, *words[1:])
            assert done.returncode == 0, done.stdout + done.stderr
        elif line == "pipx uninstall remembra":
            continue  # removes the commands; nothing under HOME holds the key because of it
        else:
            assert line == "rm -r ~/.remembra", line
            shutil.rmtree(home / ".remembra")
    return [p for p in home.rglob("*") if p.is_file() and KEY.encode() in p.read_bytes()]


def test_setup_md_taking_it_off_again_leaves_no_copy_of_the_key(tmp_path: Path) -> None:
    """Whole-release review: the four lines setup.md gave left the key in ~/.claude.json.bak-remembra-* and
    ~/.codex/config.toml.bak-remembra-* (the backups remembra-install keeps). The second line now deletes them."""
    setup = (Path(__file__).resolve().parents[1] / "landing" / "setup.md").read_text()
    section = setup.split("## Taking it off again", 1)[1].split("\n## ", 1)[0]
    block = section.split("```bash\n", 1)[1].split("```", 1)[0]
    steps = [line for line in block.splitlines() if line.strip()]
    assert "remembra-install --remove --all --apply --delete-backups" in steps
    assert "revoke the key in the dashboard (Settings > API keys)" in section

    assert _taken_off(tmp_path, "now", steps) == []
    # The review's repro, with the line as it was: the backups kept the key.
    before = [line.replace(" --delete-backups", "") for line in steps]
    left = _taken_off(tmp_path, "before", before)
    assert left and all(".bak-" in p.name for p in left), left
