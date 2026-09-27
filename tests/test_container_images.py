"""CTR-1: every image builds from pinned, maintained bases and serves as a non-root user.

- Every FROM names its base by tag AND @sha256 digest (a tag can be re-pointed;
  the digest cannot), and Dependabot's docker updater watches each directory
  that holds a Dockerfile, so the pins do not go stale.
- The Node build stages use Node 22 (Node 20 is end of life).
- uv is installed from a hash-pinned requirement file, not `pip install uv==X`.
- The nginx images (landing, dashboard, docs) run as the nginx user on 8080.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
DIGEST_FROM = re.compile(r"^FROM(?: --platform=\S+)? [\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}(?: AS [\w-]+)?$")


def _dockerfiles() -> list[Path]:
    """Every tracked Dockerfile (Dockerfile, Dockerfile.cloud, docs.Dockerfile, dashboard/Dockerfile, ...)."""
    git = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if git.returncode == 0:
        candidates = git.stdout.split()
    else:  # an exported tree without .git
        candidates = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*ockerfile*") if "node_modules" not in p.parts]
    pattern = re.compile(r"(^|/)([\w.-]*\.)?Dockerfile(\.[\w-]+)?$")
    return sorted(ROOT / p for p in candidates if pattern.search(p) and not p.endswith(".dockerignore"))


def test_every_dockerfile_is_found() -> None:
    rel = {str(p.relative_to(ROOT)) for p in _dockerfiles()}
    assert {"Dockerfile", "Dockerfile.cloud", "dashboard/Dockerfile", "docs.Dockerfile", "landing/Dockerfile"} <= rel


def test_every_from_line_is_pinned_by_digest() -> None:
    unpinned = []
    for path in _dockerfiles():
        stages: set[str] = set()
        for line in path.read_text().splitlines():
            if not line.startswith("FROM "):
                continue
            words = line.split()
            if words[1] in stages:  # FROM <an earlier stage>
                continue
            if " AS " in line:
                stages.add(line.rsplit(" AS ", 1)[1])
            if not DIGEST_FROM.match(line):
                unpinned.append(f"{path.relative_to(ROOT)}: {line}")
    assert unpinned == []


def test_dependabot_updates_the_base_image_pins_of_every_dockerfile() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    docker_dirs = {u["directory"] for u in config["updates"] if u["package-ecosystem"] == "docker"}
    for path in _dockerfiles():
        directory = "/" if path.parent == ROOT else "/" + path.parent.relative_to(ROOT).as_posix()
        assert directory in docker_dirs, f"{path.relative_to(ROOT)} is not watched"


def test_node_build_stages_use_a_maintained_node() -> None:
    for path in _dockerfiles():
        for line in path.read_text().splitlines():
            if line.startswith("FROM node:"):
                major = int(re.match(r"FROM node:(\d+)", line).group(1))  # type: ignore[union-attr]
                assert major >= 22, f"{path.relative_to(ROOT)}: {line}"


def test_uv_comes_from_a_hash_pinned_requirement() -> None:
    pins = (ROOT / "scripts" / "uv-requirements.txt").read_text()
    assert re.search(r"^uv==[\d.]+ \\\n(\s+--hash=sha256:[0-9a-f]{64} \\\n)*\s+--hash=sha256:[0-9a-f]{64}$", pins, re.M)
    for name in ("Dockerfile", "Dockerfile.cloud"):
        text = (ROOT / name).read_text()
        assert "COPY scripts/uv-requirements.txt /tmp/uv-requirements.txt" in text, name
        assert "pip install --no-cache-dir --require-hashes --no-deps -r /tmp/uv-requirements.txt" in text, name
        assert not re.search(r'pip install[^\n]*"uv==', text), name
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert any(u["package-ecosystem"] == "pip" and u["directory"] == "/scripts" for u in config["updates"])


@pytest.mark.parametrize("dockerfile", ["landing/Dockerfile", "dashboard/Dockerfile", "docs.Dockerfile"])
def test_nginx_images_serve_as_the_nginx_user_on_8080(dockerfile: str) -> None:
    text = (ROOT / dockerfile).read_text()
    final = text[text.rindex("\nFROM ") :]
    assert "FROM nginx:" in final
    assert re.search(r"^USER nginx$", final, re.M)
    assert re.search(r"^EXPOSE 8080$", final, re.M)
