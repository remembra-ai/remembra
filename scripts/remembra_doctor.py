#!/usr/bin/env python3
"""Repository-local wrapper for Remembra diagnostics."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from remembra.tools.doctor import main as doctor_main

    doctor_main()


if __name__ == "__main__":
    main()
