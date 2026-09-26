"""CI builds the production image (Dockerfile.cloud) and boots it.

Coolify builds Dockerfile.cloud on every deploy, but CI only ever built the
self-host Dockerfile, so a broken production build was first seen at deploy
time. The ``docker-cloud`` job builds it (never pushes), and
scripts/ci/cloud_image_smoke.sh boots it next to a digest-pinned Qdrant and
checks /health. These tests keep the job wired the way it was verified; the
script itself was run against a local build of this commit.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SMOKE = ROOT / "scripts" / "ci" / "cloud_image_smoke.sh"
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _job() -> dict:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert "docker-cloud" in jobs, "the docker-cloud job is gone from ci.yml"
    return dict(jobs["docker-cloud"])


def test_job_builds_dockerfile_cloud_without_pushing() -> None:
    job = _job()
    assert job["runs-on"] == "ubuntu-latest"
    assert job.get("permissions") == {"contents": "read"}
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "docker buildx build" in runs and "--file Dockerfile.cloud" in runs
    assert "--load" in runs
    assert "--push" not in runs and "push=true" not in runs and "docker push" not in runs and "docker login" not in runs
    assert '--build-arg REMEMBRA_BUILD_SHA="${GITHUB_SHA}"' in runs
    assert 'bash scripts/ci/cloud_image_smoke.sh remembra-cloud:ci "${GITHUB_SHA}"' in runs


def test_job_actions_are_pinned_to_commit_shas() -> None:
    uses = [step["uses"] for step in _job()["steps"] if "uses" in step]
    assert uses, "expected at least the checkout step"
    for ref in uses:
        assert PINNED_ACTION.match(ref), f"{ref} is not pinned to a full commit SHA"
    assert all(ref.split("@")[0] == "actions/checkout" for ref in uses)  # no third-party actions


def test_smoke_script_checks_health_against_a_pinned_qdrant() -> None:
    assert os.access(SMOKE, os.X_OK)
    text = SMOKE.read_text()
    assert re.search(r"qdrant/qdrant:v[\d.]+@sha256:[0-9a-f]{64}", text)
    prod_qdrant = re.search(r"image: (qdrant/qdrant:v[\d.]+)", (ROOT / "docker-compose.prod.yml").read_text())
    assert prod_qdrant is not None and f"{prod_qdrant.group(1)}@sha256:" in text  # the version production runs
    for check in ('"$BASE/health"', '"$BASE/health/ready"', "/oauth/callback", "/v1/__check__", "remembra-predeploy-"):
        assert check in text, check
    assert "set -euo pipefail" in text and "trap cleanup EXIT" in text
    # No secrets in CI: placeholders only.
    assert "REMEMBRA_OPENAI_API_KEY=ci-placeholder-not-a-key" in text
    assert not re.search(r"\$\{\{\s*secrets\.", WORKFLOW.read_text().split("docker-cloud:", 1)[1])


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_smoke_script_is_valid_bash() -> None:
    done = subprocess.run(["bash", "-n", str(SMOKE)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    usage = subprocess.run(["bash", str(SMOKE)], capture_output=True, text=True, check=False)
    assert usage.returncode != 0 and "usage: cloud_image_smoke.sh <image>" in usage.stderr
