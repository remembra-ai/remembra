"""A stand-in agent process: every command of one model-free agent runs as a child of this process.

Crew identity on a host is the process tree (§8.1: crewd acts only for the session whose
recorded agent pid is an ancestor of the caller; the git gates resolve the committing
session the same way). A model-free red-team agent therefore needs a real, long-lived
parent process, like the ``claude`` process of a real session. This shim reads one JSON
request per line on stdin (``argv`` or ``shell``, ``cwd``, ``env``, ``input``), runs it as
its child and answers one JSON line with the return code and output. Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    for line in sys.stdin:
        req = json.loads(line)
        try:
            res = subprocess.run(
                req["shell"] if "shell" in req else req["argv"],
                shell="shell" in req,
                input=req.get("input", ""),
                capture_output=True,
                text=True,
                cwd=req.get("cwd"),
                env=req.get("env"),
                timeout=req.get("timeout", 120),
            )
            out = {"rc": res.returncode, "stdout": res.stdout, "stderr": res.stderr}
        except subprocess.TimeoutExpired as e:
            out = {"rc": -9, "stdout": str(e.stdout or ""), "stderr": "timeout"}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
