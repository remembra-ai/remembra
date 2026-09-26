"""WP-16: the cloud entrypoint backs up and restores ``crew.db`` alongside the main database.

``scripts/cloud-entrypoint.sh`` runs for real under ``sh``. ``litestream`` and ``python`` on PATH
are stand-ins written in Python that behave like the real tools for what the entrypoint relies on:

* ``litestream restore -if-replica-exists -o PATH URL`` copies ``<replica dir>/db`` to PATH
  (``file://`` replicas), exits 0 without a file when no replica exists, and fails for URLs listed
  in ``STUB_RESTORE_FAIL``;
* ``litestream replicate -config FILE -exec CMD`` validates the config against litestream's
  ``dbs[].path`` / ``dbs[].replicas[].url`` shape, runs CMD, then copies every configured database
  to its replica (so a second boot on an empty volume can restore it);
* ``python -m remembra.main`` stands in for the server: it writes one row into the main database
  and, when Crew mode is on, into ``crew.db``; ``python -c`` runs the real interpreter.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import remembra.main as main_module
from remembra.crew.db import resolve_crew_db_path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "scripts" / "cloud-entrypoint.sh"
# The container's /bin/sh is dash (Debian); run under it too when this machine has it.
SHELLS = ["sh", *(["/bin/dash"] if Path("/bin/dash").exists() else [])]

LITESTREAM_STUB = r"""
import json, os, shlex, shutil, sqlite3, subprocess, sys
import yaml

log = os.environ["STUB_LOG"]
def record(entry):
    with open(log, "a") as f:
        f.write(json.dumps(entry) + "\n")

def replica_dir(url):
    assert url.startswith("file://"), url
    return url[len("file://"):]

args = sys.argv[1:]
record({"argv": args})
if args[0] == "restore":
    url = args[-1]
    out = args[args.index("-o") + 1]
    if url in [u for u in os.environ.get("STUB_RESTORE_FAIL", "").split(",") if u]:
        print("stub: cannot reach replica " + url, file=sys.stderr)
        sys.exit(1)
    src = os.path.join(replica_dir(url), "db")
    if not os.path.exists(src):
        if "-if-replica-exists" in args:
            print("no matching backups found")
            sys.exit(0)
        sys.exit(1)
    shutil.copyfile(src, out)
    sys.exit(0)
if args[0] == "replicate":
    assert args[1] == "-config" and args[3] == "-exec", args
    with open(args[2]) as f:
        config = yaml.safe_load(f)
    assert set(config) == {"dbs"}, config
    for db in config["dbs"]:
        assert set(db) == {"path", "replicas"} and isinstance(db["path"], str), db
        assert [set(r) for r in db["replicas"]] == [{"url"}], db
    record({"config": config})
    code = subprocess.call(shlex.split(args[4]))
    for db in config["dbs"]:
        if os.path.exists(db["path"]):
            dest = replica_dir(db["replicas"][0]["url"])
            os.makedirs(dest, exist_ok=True)
            src = sqlite3.connect(db["path"])
            dst = sqlite3.connect(os.path.join(dest, "db"))
            src.backup(dst)
            dst.close()
            src.close()
    sys.exit(code)
sys.exit(2)
"""

PYTHON_STUB = r"""
import os, sqlite3, subprocess, sys
if sys.argv[1:2] == ["-c"]:
    sys.exit(subprocess.call([os.environ["STUB_REAL_PYTHON"], *sys.argv[1:]]))
assert sys.argv[1:] == ["-m", "remembra.main"], sys.argv
value = os.environ.get("STUB_APP_VALUE", "boot")
paths = [os.environ["STUB_MAIN_DB"]]
if os.environ.get("REMEMBRA_CREW_MODE", "").strip().lower() in {"1", "true", "yes", "on"}:
    paths.append(os.environ["STUB_CREW_DB"])
for path in paths:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS boots (value TEXT)")
    conn.execute("INSERT INTO boots VALUES (?)", (value,))
    conn.commit()
    conn.close()
print("APP STARTED")
"""


def _script(dir_: Path, name: str, body: str) -> None:
    path = dir_ / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Box:
    """One container host: a volume, two replica directories and the stub tools."""

    def __init__(self, tmp_path: Path, shell: str = "sh") -> None:
        self.shell = shell
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        _script(self.bin, "litestream", LITESTREAM_STUB)
        _script(self.bin, "python", PYTHON_STUB)
        self.volume = tmp_path / "data"
        self.main_replica = tmp_path / "replicas" / "remembra"
        self.crew_replica = tmp_path / "replicas" / "remembra-crew"
        self.log = tmp_path / "calls.jsonl"

    @property
    def main_db(self) -> Path:
        return self.volume / "remembra.db"

    @property
    def crew_db(self) -> Path:
        return self.volume / "crew.db"

    def boot(self, **env: str) -> subprocess.CompletedProcess[str]:
        self.volume.mkdir(exist_ok=True)
        self.log.unlink(missing_ok=True)
        full = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "TMPDIR": str(self.tmp),
            "STUB_LOG": str(self.log),
            "STUB_REAL_PYTHON": sys.executable,
            "STUB_MAIN_DB": str(self.main_db),
            "STUB_CREW_DB": str(self.crew_db),
            "REMEMBRA_DATABASE_URL": f"sqlite+aiosqlite:///{self.main_db}",
            "LITESTREAM_REPLICA_URL": f"file://{self.main_replica}",
            **env,
        }
        full = {k: v for k, v in full.items() if v is not None}
        return subprocess.run([self.shell, str(ENTRYPOINT)], env=full, capture_output=True, text=True, timeout=60)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def config(self) -> dict:
        configs = [c["config"] for c in self.calls() if "config" in c]
        assert len(configs) == 1, self.calls()
        return configs[0]

    def restores(self) -> list[str]:
        return [c["argv"][-1] for c in self.calls() if c.get("argv", [""])[0] == "restore"]

    def wipe_volume(self) -> None:
        for f in self.volume.iterdir():
            f.unlink()


def rows(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT value FROM boots ORDER BY rowid")]
    finally:
        conn.close()


@pytest.fixture(params=SHELLS)
def box(request: pytest.FixtureRequest, tmp_path: Path) -> Box:
    return Box(tmp_path, request.param)


def test_crew_mode_on_replicates_both_files_and_a_wiped_volume_restores_both(box: Box) -> None:
    first = box.boot(REMEMBRA_CREW_MODE="true", STUB_APP_VALUE="first")
    assert first.returncode == 0, first.stderr
    assert "APP STARTED" in first.stdout
    assert "created an empty crew.db" in first.stdout
    assert box.config() == {
        "dbs": [
            {"path": str(box.main_db), "replicas": [{"url": f"file://{box.main_replica}"}]},
            {"path": str(box.crew_db), "replicas": [{"url": f"file://{box.crew_replica}"}]},
        ]
    }
    assert rows(box.crew_db) == ["first"]

    box.wipe_volume()
    second = box.boot(REMEMBRA_CREW_MODE="true", STUB_APP_VALUE="second")
    assert second.returncode == 0, second.stderr
    assert "main restore complete" in second.stdout and "crew restore complete" in second.stdout
    assert box.restores() == [f"file://{box.main_replica}", f"file://{box.crew_replica}"]
    # both histories came back and kept growing
    assert rows(box.main_db) == ["first", "second"]
    assert rows(box.crew_db) == ["first", "second"]


def test_precreated_crew_db_is_an_empty_wal_database(box: Box) -> None:
    proc = box.boot(REMEMBRA_CREW_MODE="1", STUB_APP_VALUE="x")
    assert proc.returncode == 0, proc.stderr
    conn = sqlite3.connect(box.crew_db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_crew_mode_off_without_a_crew_replica_leaves_crew_db_out(box: Box) -> None:
    proc = box.boot(REMEMBRA_CREW_MODE="false")
    assert proc.returncode == 0, proc.stderr
    assert not box.crew_db.exists()
    assert [db["path"] for db in box.config()["dbs"]] == [str(box.main_db)]
    assert "no crew.db on this volume" in proc.stdout
    # the restore was still attempted, so a crew history is never silently skipped
    assert box.restores() == [f"file://{box.main_replica}", f"file://{box.crew_replica}"]


def test_crew_mode_off_still_restores_and_replicates_an_existing_crew_history(box: Box) -> None:
    assert box.boot(REMEMBRA_CREW_MODE="on", STUB_APP_VALUE="crew-era").returncode == 0
    box.wipe_volume()
    # rollback: flag off, fresh volume. crew.db must come back so switching the flag on again loses nothing
    proc = box.boot(REMEMBRA_CREW_MODE="false", STUB_APP_VALUE="flag-off")
    assert proc.returncode == 0, proc.stderr
    assert rows(box.crew_db) == ["crew-era"]  # the app did not touch it
    assert [db["path"] for db in box.config()["dbs"]] == [str(box.main_db), str(box.crew_db)]


def test_failed_crew_restore_stops_the_container_when_crew_mode_is_on(box: Box) -> None:
    proc = box.boot(REMEMBRA_CREW_MODE="true", STUB_RESTORE_FAIL=f"file://{box.crew_replica}")
    assert proc.returncode == 1
    assert "RESTORE FAILED for crew" in proc.stderr
    assert "APP STARTED" not in proc.stdout
    assert not any("config" in c for c in box.calls())


def test_failed_crew_restore_with_the_override_starts_empty_and_replicates(box: Box) -> None:
    proc = box.boot(
        REMEMBRA_CREW_MODE="true",
        STUB_RESTORE_FAIL=f"file://{box.crew_replica}",
        LITESTREAM_ALLOW_EMPTY_START="1",
    )
    assert proc.returncode == 0, proc.stderr
    assert "EMPTY database (crew)" in proc.stderr
    assert [db["path"] for db in box.config()["dbs"]] == [str(box.main_db), str(box.crew_db)]


def test_failed_crew_restore_with_crew_mode_off_warns_and_never_overwrites_the_replica(box: Box) -> None:
    proc = box.boot(REMEMBRA_CREW_MODE="", STUB_RESTORE_FAIL=f"file://{box.crew_replica}")
    assert proc.returncode == 0, proc.stderr
    assert "left out of replication" in proc.stderr
    assert "APP STARTED" in proc.stdout
    assert [db["path"] for db in box.config()["dbs"]] == [str(box.main_db)]


def test_failed_main_restore_still_stops_the_container(box: Box) -> None:
    proc = box.boot(REMEMBRA_CREW_MODE="true", STUB_RESTORE_FAIL=f"file://{box.main_replica}")
    assert proc.returncode == 1
    assert "RESTORE FAILED for main" in proc.stderr
    assert box.restores() == [f"file://{box.main_replica}"]


def test_existing_files_are_not_restored_over(box: Box) -> None:
    assert box.boot(REMEMBRA_CREW_MODE="true", STUB_APP_VALUE="one").returncode == 0
    proc = box.boot(REMEMBRA_CREW_MODE="true", STUB_APP_VALUE="two")
    assert proc.returncode == 0, proc.stderr
    assert box.restores() == []
    assert rows(box.crew_db) == ["one", "two"]


@pytest.mark.parametrize(
    ("main_url", "crew_override", "expected"),
    [
        ("s3://bucket/remembra", None, "s3://bucket/remembra-crew"),
        ("s3://bucket/remembra/", None, "s3://bucket/remembra-crew"),
        ("s3://bucket", None, "s3://bucket/crew"),
        ("s3://bucket/", None, "s3://bucket/crew"),
        ("s3://bucket/remembra", "s3://other/crew-backups", "s3://other/crew-backups"),
    ],
)
def test_crew_replica_url(box: Box, main_url: str, crew_override: str | None, expected: str) -> None:
    box.main_db.parent.mkdir(exist_ok=True)
    box.main_db.write_bytes(b"")  # skip the main restore; the crew restore shows the derived URL
    env = {"LITESTREAM_REPLICA_URL": main_url, "STUB_RESTORE_FAIL": expected}
    if crew_override:
        env["LITESTREAM_CREW_REPLICA_URL"] = crew_override
    proc = box.boot(**env)
    assert box.restores() == [expected], proc.stderr


def test_crew_replica_must_differ_from_the_main_replica(box: Box) -> None:
    proc = box.boot(LITESTREAM_CREW_REPLICA_URL=f"file://{box.main_replica}/")
    assert proc.returncode == 1
    assert "must differ" in proc.stderr
    assert box.calls() == []


def test_config_values_litestream_would_expand_are_refused(box: Box) -> None:
    proc = box.boot(LITESTREAM_REPLICA_URL="s3://bucket/$HOME")
    assert proc.returncode == 1
    assert "refusing to write a config value" in proc.stderr
    assert box.calls() == []


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"REMEMBRA_DATABASE_URL": "sqlite+aiosqlite:////data/remembra.db"}, "/data/crew.db"),
        ({"REMEMBRA_DATABASE_URL": "sqlite:////srv/x/main.db"}, "/srv/x/crew.db"),
        ({"REMEMBRA_DATABASE_URL": "sqlite:///rel/dir/main.db"}, "rel/dir/crew.db"),
        ({"REMEMBRA_DATABASE_URL": "sqlite:////data/remembra.db", "REMEMBRA_CREW_DB_PATH": "/crew/c.db"}, "/crew/c.db"),
        ({"REMEMBRA_DB_PATH": "/vol/remembra.db", "REMEMBRA_DATABASE_URL": "sqlite:////vol/remembra.db"}, "/vol/crew.db"),
    ],
)
@pytest.mark.parametrize("shell", SHELLS)
def test_entrypoint_finds_crew_db_where_the_server_opens_it(
    tmp_path: Path, shell: str, env: dict[str, str], expected: str
) -> None:
    """The shell derivation must agree with remembra.crew.db.resolve_crew_db_path (else backups miss it)."""
    script = ENTRYPOINT.read_text()
    probe = script.split('if [ -n "$LITESTREAM_REPLICA_URL" ]; then', 1)[0] + 'printf "%s\\n%s" "$DB_PATH" "$CREW_DB_PATH"\n'
    (tmp_path / "probe.sh").write_text(probe)
    proc = subprocess.run(
        [shell, str(tmp_path / "probe.sh")], env={"PATH": "/usr/bin:/bin", **env}, capture_output=True, text=True
    )
    main_path, crew_path = proc.stdout.split("\n")
    assert crew_path == expected
    old = os.environ.get("REMEMBRA_CREW_DB_PATH")
    try:
        if "REMEMBRA_CREW_DB_PATH" in env:
            os.environ["REMEMBRA_CREW_DB_PATH"] = env["REMEMBRA_CREW_DB_PATH"]
        else:
            os.environ.pop("REMEMBRA_CREW_DB_PATH", None)
        assert resolve_crew_db_path(env["REMEMBRA_DATABASE_URL"]) == crew_path
    finally:
        if old is None:
            os.environ.pop("REMEMBRA_CREW_DB_PATH", None)
        else:
            os.environ["REMEMBRA_CREW_DB_PATH"] = old


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On", "", "0", "false", "no", "off", "o n", "*", "tru"])
def test_entrypoint_reads_the_flag_exactly_like_the_server(tmp_path: Path, monkeypatch, shell: str, value: str) -> None:
    script = ENTRYPOINT.read_text()
    probe = script.split('if [ -n "$LITESTREAM_REPLICA_URL" ]; then', 1)[0] + 'printf "%s" "$CREW_MODE"\n'
    (tmp_path / "probe.sh").write_text(probe)
    proc = subprocess.run(
        [shell, str(tmp_path / "probe.sh")],
        env={"PATH": "/usr/bin:/bin", "REMEMBRA_CREW_MODE": value},
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv(main_module.CREW_MODE_ENV, value)
    assert (proc.stdout == "1") is main_module.crew_mode_enabled()


def test_crew_mode_ships_off_in_every_deploy_config(monkeypatch) -> None:
    dockerfile = (ROOT / "Dockerfile.cloud").read_text()
    assert "ENV REMEMBRA_CREW_MODE=false" in dockerfile
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        env = yaml.safe_load((ROOT / name).read_text())["services"]["remembra"]["environment"]
        assert "REMEMBRA_CREW_MODE=${REMEMBRA_CREW_MODE:-false}" in env, name
    assert 'REMEMBRA_CREW_MODE = "false"' in (ROOT / "fly.toml").read_text()
    monkeypatch.setenv(main_module.CREW_MODE_ENV, "false")
    assert main_module.crew_mode_enabled() is False
