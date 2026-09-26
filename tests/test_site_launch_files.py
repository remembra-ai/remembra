"""Files the public sites need at launch: Paddle's Apple Pay domain association
on app.remembra.dev, and the docs image that rebuilds docs.remembra.dev from docs/."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLE_PAY = ROOT / "dashboard" / "public" / ".well-known" / "apple-developer-merchantid-domain-association"
DOCS_DOCKERFILE = ROOT / "docs.Dockerfile"


def test_apple_pay_domain_association_ships_with_the_dashboard():
    # Vite copies public/ into dist/ as-is, and dashboard/nginx.conf serves any
    # existing file before the SPA fallback, so this becomes
    # https://app.remembra.dev/.well-known/apple-developer-merchantid-domain-association
    body = APPLE_PAY.read_text(encoding="ascii")
    assert len(body) > 1000
    # Paddle's file is hex-encoded JSON ({"pspId":...}) on one line.
    assert re.fullmatch(r"[0-9A-F]+", body)
    assert bytes.fromhex(body[:20]).startswith(b'{"pspId"')


def test_docs_image_builds_the_site_from_docs_only():
    text = DOCS_DOCKERFILE.read_text()
    copies = re.findall(r"^COPY (?!--from)(.+)$", text, flags=re.MULTILINE)
    # The pages come from mkdocs.yml and docs/ only; the rest is the pinned build
    # tools, and the nginx config plus the CSP script (LIVE-4 / CTR-1).
    assert copies == [
        ".github/docs-requirements.txt ./docs-requirements.txt",
        "mkdocs.yml ./",
        "docs ./docs",
        "docs-nginx/remembra-headers.conf scripts/docs_csp.py ./",
        "docs-nginx/nginx.conf /etc/nginx/nginx.conf",
    ]
    assert "mkdocs build --strict" in text
    pins = (ROOT / ".github" / "docs-requirements.txt").read_text()
    for pinned in ("mkdocs==", "mkdocs-material==", "pymdown-extensions=="):
        assert re.search(rf"^{re.escape(pinned)}\S+ \\\n\s+--hash=sha256:[0-9a-f]{{64}}", pins, re.M), pinned
    assert "COPY --from=build /out /usr/share/nginx/html" in text


def test_docs_image_has_its_own_build_context():
    # .dockerignore drops docs/ and *.md for the API image; the docs image needs
    # both, so BuildKit's per-Dockerfile ignore file must let them through.
    root_ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "docs" in root_ignore and "*.md" in root_ignore
    lines = [
        line.strip()
        for line in (ROOT / "docs.Dockerfile.dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == ["*", "!mkdocs.yml", "!docs/", "!docs-nginx/", "!scripts/docs_csp.py", "!.github/docs-requirements.txt"]
