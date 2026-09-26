"""R-23: the cloud entrypoint runs litestream with an explicit replica retention.

The privacy page promises that erased data leaves the continuous backup within
48 hours; that holds because the replica keeps 24 hours of history. The script
is executed for real with a stand-in ``litestream`` binary on PATH that records
its arguments and the config file it was given (restore and startup paths are
covered in tests/test_rel_ops.py).
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloud-entrypoint.sh"

FAKE_LITESTREAM = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
if [ "$1" = "replicate" ]; then
    cat "$3" > "$FAKE_LOG.config"
fi
exit 0
"""


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "litestream"
    fake.write_text(FAKE_LITESTREAM)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_LOG": str(tmp_path / "litestream.log"),
        "REMEMBRA_DB_PATH": str(tmp_path / "remembra.db"),
        "LITESTREAM_CONFIG": str(tmp_path / "litestream.yml"),
        "LITESTREAM_REPLICA_URL": "s3://backups/remembra",
    }
    base.update(env)
    return subprocess.run(["sh", str(SCRIPT)], env=base, capture_output=True, text=True, timeout=30, check=False)


def test_replication_uses_a_config_with_24h_retention(tmp_path: Path) -> None:
    (tmp_path / "remembra.db").write_bytes(b"")  # existing database: no restore
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "litestream.log").read_text().splitlines()
    assert calls == [f"replicate -config {tmp_path / 'litestream.yml'} -exec python -m remembra.main"]
    config = yaml.safe_load((tmp_path / "litestream.log.config").read_text())
    replica = config["dbs"][0]["replicas"][0]
    assert config["dbs"][0]["path"] == str(tmp_path / "remembra.db")
    assert replica == {"url": "s3://backups/remembra", "retention": "24h", "retention-check-interval": "1h"}
    assert "replica retention 24h" in result.stdout


def test_retention_can_be_set_and_is_validated(tmp_path: Path) -> None:
    (tmp_path / "remembra.db").write_bytes(b"")
    assert _run(tmp_path, LITESTREAM_RETENTION="12h").returncode == 0
    replica = yaml.safe_load((tmp_path / "litestream.log.config").read_text())["dbs"][0]["replicas"][0]
    assert replica["retention"] == "12h"
    bad = _run(tmp_path, LITESTREAM_RETENTION="24h; rm -rf /")
    assert bad.returncode == 1 and "LITESTREAM_RETENTION" in bad.stderr
    quoted = _run(tmp_path, LITESTREAM_REPLICA_URL='s3://b/"x')
    assert quoted.returncode == 1 and "quotes" in quoted.stderr
