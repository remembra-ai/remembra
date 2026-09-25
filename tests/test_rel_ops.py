"""REL-13 / REL-14 / REL-18 / REL-19: deploy artifacts.

The entrypoint is executed for real (``sh``) with fake ``litestream``/``python``
binaries on PATH. Dockerfiles and compose files are checked for the invariants
that previously broke deploys (Docker itself is not required for these tests).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "scripts" / "cloud-entrypoint.sh"


def _fake_bin(dir_: Path, name: str, body: str) -> None:
    path = dir_ / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_entrypoint(tmp_path: Path, env: dict[str, str], restore_exit: int = 0, restore_creates: bool = True):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    create = 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && touch "$2"; shift; done\n' if restore_creates else ""
    _fake_bin(
        bindir,
        "litestream",
        f'echo "litestream $*" >> "{log}"\n'
        f'if [ "$1" = "restore" ]; then {create} exit {restore_exit}; fi\n'
        'if [ "$1" = "replicate" ]; then echo "REPLICATE"; exit 0; fi\n',
    )
    _fake_bin(bindir, "python", f'echo "python $*" >> "{log}"\necho "APP STARTED"\n')
    full_env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "REMEMBRA_DB_PATH": str(tmp_path / "data" / "remembra.db"),
        **env,
    }
    (tmp_path / "data").mkdir(exist_ok=True)
    proc = subprocess.run(["sh", str(ENTRYPOINT)], env=full_env, capture_output=True, text=True, timeout=20)
    calls = log.read_text() if log.exists() else ""
    return proc, calls


def test_entrypoint_restore_failure_refuses_to_start(tmp_path) -> None:
    proc, calls = _run_entrypoint(tmp_path, {"LITESTREAM_REPLICA_URL": "s3://b/r"}, restore_exit=1, restore_creates=False)
    assert proc.returncode == 1
    assert "RESTORE FAILED" in proc.stderr
    assert "APP STARTED" not in proc.stdout
    assert "replicate" not in calls


def test_entrypoint_restore_failure_override_starts_empty(tmp_path) -> None:
    proc, calls = _run_entrypoint(
        tmp_path,
        {"LITESTREAM_REPLICA_URL": "s3://b/r", "LITESTREAM_ALLOW_EMPTY_START": "1"},
        restore_exit=1,
        restore_creates=False,
    )
    assert proc.returncode == 0
    assert "EMPTY database" in proc.stderr
    assert "REPLICATE" in proc.stdout


def test_entrypoint_restores_then_replicates(tmp_path) -> None:
    proc, calls = _run_entrypoint(tmp_path, {"LITESTREAM_REPLICA_URL": "s3://b/r"})
    assert proc.returncode == 0, proc.stderr
    assert "restore complete" in proc.stdout
    assert "litestream restore -if-replica-exists" in calls
    assert "litestream replicate -exec python -m remembra.main" in calls


def test_entrypoint_existing_db_skips_restore(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "remembra.db").write_text("")
    proc, calls = _run_entrypoint(tmp_path, {"LITESTREAM_REPLICA_URL": "s3://b/r"})
    assert proc.returncode == 0
    assert "restore" not in calls and "replicate" in calls


def test_entrypoint_without_replica_warns_loudly(tmp_path) -> None:
    proc, calls = _run_entrypoint(tmp_path, {})
    assert proc.returncode == 0
    assert "NOT being backed up" in proc.stderr
    assert "APP STARTED" in proc.stdout
    assert "litestream" not in calls


def test_entrypoint_missing_litestream_binary_fails(tmp_path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bin(bindir, "python", 'echo "APP STARTED"\n')
    proc = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT)],
        env={"PATH": str(bindir), "LITESTREAM_REPLICA_URL": "s3://b/r", "REMEMBRA_DB_PATH": str(tmp_path / "x.db")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 1
    assert "binary is missing" in proc.stderr


# ---------------------------------------------------------------------------
# Dockerfiles
# ---------------------------------------------------------------------------


def test_cloud_dockerfile_installs_from_lock_with_rerank_and_checks_litestream() -> None:
    text = (ROOT / "Dockerfile.cloud").read_text()
    assert "COPY pyproject.toml uv.lock README.md ./" in text
    assert 'install-locked-deps.sh "server cloud encryption rerank"' in text
    assert 'pip install --no-cache-dir ".[' not in text  # no unpinned resolution
    assert "sha256sum -c -" in text
    assert "ADD https://github.com/benbjohnson/litestream" not in text
    assert "ARG SOURCE_COMMIT" in text and "ENV REMEMBRA_BUILD_SHA=${REMEMBRA_BUILD_SHA}" in text
    assert "CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')" in text


def test_selfhost_dockerfile_installs_from_lock() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert "uv.lock" in text and "install-locked-deps.sh" in text
    assert 'pip install --no-cache-dir ".[' not in text


def test_install_script_skips_cuda_wheels_and_pins_cpu_torch() -> None:
    text = (ROOT / "scripts" / "install-locked-deps.sh").read_text()
    assert "--frozen" in text
    assert "^(torch|triton|nvidia-)" in text
    assert "download.pytorch.org/whl/cpu" in text


# ---------------------------------------------------------------------------
# Compose files
# ---------------------------------------------------------------------------


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())


def _env_list(service: dict) -> list[str]:
    env = service.get("environment", [])
    return env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.quickstart.yml"])
def test_compose_invariants(name: str) -> None:
    services = _compose(name)["services"]
    qdrant = services["qdrant"]
    assert qdrant["image"] == "qdrant/qdrant:v1.17.0"
    assert "curl" not in " ".join(qdrant["healthcheck"]["test"])
    remembra = services["remembra"]
    env = _env_list(remembra)
    assert not any(e.startswith("OPENAI_API_KEY") for e in env), "bare OPENAI_API_KEY is ignored by Settings"
    assert "curl" not in " ".join(remembra["healthcheck"]["test"])  # self-host image has no curl
    for e in env:
        if e.startswith("REMEMBRA_DATABASE_URL"):
            path = e.split("///", 1)[1]
            assert path.startswith("/"), f"relative sqlite path in {name}: {e}"


def test_compose_env_names_are_real_settings() -> None:
    from remembra.config import Settings

    fields = {f.upper() for f in Settings.model_fields}
    for name in ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.quickstart.yml"):
        for entry in _env_list(_compose(name)["services"]["remembra"]):
            key = entry.split("=", 1)[0]
            assert key.startswith("REMEMBRA_"), (name, key)
            assert key.removeprefix("REMEMBRA_") in fields, (name, key)


def test_build_sha_falls_back_to_source_commit(monkeypatch) -> None:
    from remembra.config import Settings

    monkeypatch.setenv("SOURCE_COMMIT", "abc1234")
    monkeypatch.setenv("REMEMBRA_BUILD_SHA", "")
    assert Settings().build_sha == "abc1234"
    monkeypatch.setenv("REMEMBRA_BUILD_SHA", "explicit")
    assert Settings().build_sha == "explicit"
    monkeypatch.delenv("SOURCE_COMMIT")
    monkeypatch.delenv("REMEMBRA_BUILD_SHA")
    assert Settings().build_sha is None or os.environ.get("REMEMBRA_BUILD_SHA")


def _resolve_compose_value(value: str) -> str:
    """Expand ${VAR:-default} / ${VAR:?msg} / ${VAR} the way compose does with an empty shell env."""
    import re

    def sub(m: re.Match[str]) -> str:
        expr = m.group(1)
        if ":-" in expr:
            return expr.split(":-", 1)[1]
        if ":?" in expr:
            return "x" * 40  # required secret supplied by the operator
        return ""

    return re.sub(r"\$\{([^}]*)\}", sub, value)


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.quickstart.yml"])
def test_compose_default_environment_boots_settings(name: str, monkeypatch) -> None:
    """Every compose file's default environment must parse into Settings (boot would crash otherwise)."""
    from remembra.config import Settings

    for key in list(os.environ):
        if key.startswith("REMEMBRA_"):
            monkeypatch.delenv(key)
    for entry in _env_list(_compose(name)["services"]["remembra"]):
        key, _, value = entry.partition("=")
        resolved = _resolve_compose_value(str(value))
        monkeypatch.setenv(key, resolved)
    settings = Settings()
    assert settings.database_url.split("///", 1)[1].startswith("/")
    assert settings.log_level == settings.log_level.lower()
