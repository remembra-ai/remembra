"""The docs and the README: what they tell people to install, and where they lead.

* Every package an install command in docs/ or the README names is collected
  by ``scripts/site_predeploy.py``; its ``--packages`` mode (run by the docs CI
  job) checks each one against PyPI or npm. Here the collector is tested with
  fake registries, and the list is held to names verified on 2026-09-25.
* The docs home leads with the Remembra Relay install lines.
* Docs, README and site share one working Discord invite.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "landing"
SCRIPTS = ROOT / "scripts"
INSTALL_STEP = "pipx install 'remembra[mcp]'"


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def test_package_check_reads_every_install_command_form(tmp_path: Path) -> None:
    predeploy = _script("site_predeploy")
    doc = tmp_path / "guide.md"
    doc.write_text(
        "pipx install --force 'remembra[mcp]>=0.16'\n"
        'pip install -e ".[server]" -r requirements.txt httpx\n'
        "npm install -g @remembra/client@1.2.0\n"
        "yarn add remembra\n"
        "uv pip install langgraph==0.2 # comment\n"
        "pip install git+https://github.com/x/y https://example.com/z.whl\n"
    )
    assert predeploy.package_names([doc]) == {
        ("pypi", "remembra"): ["guide.md"],
        ("pypi", "httpx"): ["guide.md"],
        ("npm", "@remembra/client"): ["guide.md"],
        ("npm", "remembra"): ["guide.md"],
        ("pypi", "langgraph"): ["guide.md"],
    }
    assert predeploy.registry_url("npm", "@remembra/client") == "https://registry.npmjs.org/@remembra%2Fclient"
    assert predeploy.registry_url("pypi", "remembra") == "https://pypi.org/pypi/remembra/json"


def test_package_check_fails_on_a_package_the_registry_does_not_have(tmp_path: Path) -> None:
    predeploy = _script("site_predeploy")
    doc = tmp_path / "guide.md"
    doc.write_text("npm install @remembra/client\npip install remembra\n")
    missing = {"https://registry.npmjs.org/@remembra%2Fclient": 404}
    problems = predeploy.package_problems(fetch=lambda url: missing.get(url, 200), sources=[doc])
    assert problems == ["npm has no package named @remembra/client (guide.md)"]
    down = predeploy.package_problems(fetch=lambda url: 0, sources=[doc])
    assert len(down) == 2 and all("could not confirm" in p for p in down)


def test_docs_name_only_packages_that_were_checked_to_exist() -> None:
    # The live check runs in the docs CI job (network); here, hold the list to what was
    # verified on the registries on 2026-09-25, so a new name gets checked before it ships.
    predeploy = _script("site_predeploy")
    names = {name for (_registry, name) in predeploy.package_names()}
    assert "@remembra/client" not in names  # npm: not found (R-34)
    assert names <= {"remembra", "crewai", "httpx", "langchain-core", "langgraph", "locust", "nltk", "openai", "openai-agents"}


def test_docs_and_site_share_one_discord_invite() -> None:
    texts = [(ROOT / "mkdocs.yml").read_text(), (ROOT / "README.md").read_text()]
    texts += [p.read_text() for p in (ROOT / "docs").rglob("*.md")] + [p.read_text() for p in LANDING.rglob("*.html")]
    invites = {m for t in texts for m in re.findall(r"discord\.gg/(\w+)", t)}
    assert invites == {"mPYQRKzXz5"}


def test_docs_home_leads_with_the_relay_install() -> None:
    home = (ROOT / "docs" / "index.md").read_text()
    first = home[: home.index("## The memory API underneath")]
    assert "## Remembra Relay" in first
    block = re.search(r"```bash\n(.*?)\n```", first, re.S)
    assert block is not None and block.group(1).splitlines() == [
        INSTALL_STEP,
        "remembra-install --all --api-key <your-key>",
        "remembra-relay connect",
    ]
    assert "guides/relay.md" in first and "getting-started/agent-setup.md" in first
    assert "v0.13" not in first
