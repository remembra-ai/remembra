"""``remembra-relay doctor`` and ``remembra_doctor``: collect, apply the rules, render. Changes nothing."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import httpx

from remembra.marshal.render import Style, render_json, render_text
from remembra.marshal.rules import Finding, evaluate
from remembra.marshal.signals import Signals, collect

EXIT_OK = 0
EXIT_NEEDS_YOU = 1
EXIT_USAGE = 2


@dataclass(frozen=True)
class Report:
    signals: Signals
    findings: list[Finding]

    @property
    def exit_code(self) -> int:
        """0: nothing proven to need you; 1: at least one ``[!!]`` finding."""
        return EXIT_NEEDS_YOU if any(f.marker == "[!!]" for f in self.findings) else EXIT_OK

    def text(self, style: Style | None = None) -> str:
        return render_text(self.signals, self.findings, style)

    def json(self) -> dict[str, Any]:
        return render_json(self.signals, self.findings, self.exit_code)


def run(
    home: Path | None = None,
    agents: list[str] | None = None,
    check_server: bool = True,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    which: Callable[[str], str | None] = shutil.which,
    now: float | None = None,
    cwd: Path | None = None,
) -> Report:
    env = dict(os.environ if environ is None else environ)
    home = Path(home or env.get("HOME") or Path.home())
    signals = collect(home, agents, check_server, now, environ=env, transport=transport, which=which, cwd=cwd)
    return Report(signals, evaluate(signals))


def terminal_style(mode: str, stream: TextIO, environ: Mapping[str, str]) -> Style:
    """Colour when ``mode`` is always, or auto on a TTY without NO_COLOR; ASCII glyphs when the stream can't encode ours."""
    if mode == "always":
        color = True
    elif mode == "never":
        color = False
    else:
        try:
            color = stream.isatty() and "NO_COLOR" not in environ
        except (AttributeError, ValueError):
            color = False
    truecolor = (environ.get("COLORTERM") or "").lower() in ("truecolor", "24bit")
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "›●○·◆┊→".encode(encoding)
        ascii_only = False
    except (LookupError, UnicodeEncodeError):
        ascii_only = True
    return Style(color=color, truecolor=truecolor, ascii=ascii_only)


def main_args(agents: list[str] | None, fmt: str, no_server: bool, color: str, stream: TextIO | None = None) -> int:
    """The CLI entry (``remembra-relay doctor``): prints the slip or JSON; returns the exit code."""
    import json

    out = stream or sys.stdout
    report = run(agents=agents, check_server=not no_server)
    if fmt == "json":
        out.write(json.dumps(report.json(), indent=2, default=str) + "\n")
    else:
        out.write(report.text(terminal_style(color, out, os.environ)))
    out.flush()
    return report.exit_code
