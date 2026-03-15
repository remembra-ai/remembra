#!/usr/bin/env python3
"""Repository-local wrapper for the Remembra bridge."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from remembra.tools.bridge import main as bridge_main

    bridge_main()


if __name__ == "__main__":
    main()
