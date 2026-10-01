"""Run pytest normally; expose process-shutdown stalls without changing its result.

A completed pytest summary does not guarantee that Python exits. CI's job timeout
bounds the whole process; this runner prints surviving threads and dumps their
stacks if interpreter shutdown still has not finished after a minute. It never
forces process exit or converts a failure into success, and prints no locals.
"""

from __future__ import annotations

import faulthandler
import sys
import threading
import traceback

import pytest


def main() -> int:
    result = int(pytest.main(sys.argv[1:]))
    frames = sys._current_frames()
    for thread in threading.enumerate():
        if thread is threading.main_thread() or thread.daemon:
            continue
        print(f"Pending shutdown thread: {thread.name}", flush=True)
        if thread.ident in frames:
            traceback.print_stack(frames[thread.ident], file=sys.stderr)
    faulthandler.dump_traceback_later(60, repeat=True)
    return result


if __name__ == "__main__":
    sys.exit(main())
