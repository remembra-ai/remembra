"""Hook metadata survives a slow daemon and concurrent outbox wakeups."""

import asyncio
import os
import time

from remembra.relay.crew import outbox as O
from tests.crew.test_wp9_gate import _start_crewd, arun_gate
from tests.crew.wp9_support import crew_server, make_repo, peer


async def test_posttool_metadata_survives_ancestry_read_after_hook_exits(tmp_path):
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        a = await d.join(
            peer(os.getpid()),
            {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": os.getpid()},
        )
        original = d.ppid_table

        def late_table(pid=None):
            time.sleep(0.3)  # longer than the hook's100ms IPC budget
            return original(pid)

        d.ppid_table = late_table
        result = await arun_gate(
            layout,
            "posttool",
            {
                "session_id": "sess-a",
                "cwd": str(repo),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src/app/reports/export.ts")},
                "tool_response": {},
            },
        )
        assert (result.returncode, result.stdout) == (0, "")
        await asyncio.sleep(0.4)
        await d.drain()
        await d.flush_outbox()
        assert d.pending_footprints[a["key"]]["src/app/reports/export.ts"]["attribution"] == "certain"
        assert d.sessions[a["key"]]["files_since_checkpoint"] == 1


async def test_concurrent_outbox_wakeups_apply_activity_once(tmp_path):
    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        a = await d.join(
            peer(os.getpid()),
            {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": os.getpid()},
        )
        O.spool(
            layout.outbox,
            "activity",
            {"key": a["key"], "phase": "end", "tool": "Edit", "paths": ["src/app/reports/export.ts"]},
            session_key=a["key"],
            crew_id=a["crew_id"],
        )
        original = d._send_entry
        entered = asyncio.Event()
        release = asyncio.Event()

        async def paused(entry):
            entered.set()
            await release.wait()
            return await original(entry)

        d._send_entry = paused
        first = asyncio.create_task(d.flush_outbox())
        await entered.wait()
        second = asyncio.create_task(d.flush_outbox())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        assert d.sessions[a["key"]]["files_since_checkpoint"] == 1
        assert O.read_entries(layout.outbox, ("activity",)) == []
