"""The release migration verifiers default to the commit production runs, so a bare run tests the real upgrade.

Production (api.remembra.dev) runs main f70abac: release 0.16.1 plus copy fixes, main-DB schema 10 (versions
1-4 and 6-10). When production moves, change PROD_DEFAULT in both scripts and this test together.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "maintenance"
PRODUCTION = "f70abac"
RETIRED = "4d335ce"  # 0.16.1 before the copy fixes: no longer what production runs


def _load(name: str):  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(f"_verifier_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["verify_release_migrations", "verify_inbox_migration_order"])
def test_verifiers_default_to_the_commit_production_runs(name: str) -> None:
    assert _load(name).PROD_DEFAULT == PRODUCTION
    source = (SCRIPTS / f"{name}.py").read_text(encoding="utf-8")
    assert RETIRED not in source  # docstring, comments and help name the deployed commit
    out = subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), "--help"], capture_output=True, text=True, check=True)
    assert f"--prod {PRODUCTION}" in out.stdout and "default" in out.stdout
