"""Copy the public docs Marshal's ``remembra_help`` answers from into the package.

``remembra_help`` quotes only these pages, bundled in the wheel so it works
offline and on a base install. They are published docs (in ``mkdocs.yml``'s
nav, not in ``exclude_docs``); nothing else is ever added to the pack.

    python scripts/sync_marshal_pack.py          # copy
    python scripts/sync_marshal_pack.py --check  # exit 1 if the pack differs from docs/
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "src" / "remembra" / "marshal" / "pack"
SOURCES = {
    "relay.md": ROOT / "docs" / "guides" / "relay.md",
    "plans-and-credits.md": ROOT / "docs" / "reference" / "plans-and-credits.md",
}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for name, source in SOURCES.items():
        target = PACK / name
        text = source.read_text(encoding="utf-8")
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == text:
            continue
        if check:
            stale.append(name)
        else:
            PACK.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"updated {target.relative_to(ROOT)}")
    extra = sorted(p.name for p in PACK.glob("*.md") if p.name not in SOURCES)
    if extra:
        print(f"not from docs/ (remove): {', '.join(extra)}", file=sys.stderr)
        return 1
    if stale:
        print(f"out of date, run python scripts/sync_marshal_pack.py: {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
