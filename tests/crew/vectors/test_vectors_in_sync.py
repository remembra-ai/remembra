"""The committed vector files are exactly what build.py generates (no hand edits, no drift)."""

from __future__ import annotations

import json

from tests.crew.vectors.build import VECTOR_DIR, write_all


def test_vector_files_match_their_generator() -> None:
    changed = write_all(check=True)
    assert changed == [], f"regenerate with: PYTHONPATH=src python -m tests.crew.vectors.build  ({changed})"


def test_every_vector_file_is_json() -> None:
    files = sorted(VECTOR_DIR.glob("*/*.json"))
    assert len(files) >= 25
    for path in files:
        json.loads(path.read_text(encoding="utf-8"))
