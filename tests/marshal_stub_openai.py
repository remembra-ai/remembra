"""A tiny OpenAI-compatible chat server for local desk runs and the e2e test. It is never the real API.

Run it and point the desk at it::

    python -m tests.marshal_stub_openai --port 8799
    REMEMBRA_MARSHAL_OPENAI_BASE_URL=http://127.0.0.1:8799/v1 ...

``POST /v1/chat/completions`` answers like gpt-4o-mini would in the desk's
loop, deterministically: a request with no read yet (besides a "why?" slip's
pre-read) gets a ``trail_summary`` tool call; once a read is back it gets a
JSON answer citing every successful read, with no figures and the Codex trust
fix's two commands. ``--script FILE`` serves a JSON list of completions in
order instead. ``--delay SECONDS`` holds each completion that long (a slow model,
so a live run can see the reads stream before the answer). ``GET /_stub/requests``
returns every request body received.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

CONTEXT_CALL = "call_context_r1"
ANSWER_TEXT = (
    "Codex: no brief or handoff from it has reached Remembra yet. "
    "Likely cause: its hooks aren't trusted. Trust them with /hooks in the Codex CLI."
)
ANSWER_COMMANDS = ["/hooks", "remembra-relay doctor --agent codex"]
_OK_READ = re.compile(r'"status": "ok",\s*"id": "(r[1-4])"')


def completion(message: dict[str, Any], finish: str, usage: tuple[int, int, int]) -> dict[str, Any]:
    prompt, output, cached = usage
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": output,
            "total_tokens": prompt + output,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


def auto_reply(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    own_reads = [m for m in tool_messages if m.get("tool_call_id") != CONTEXT_CALL]
    if not own_reads and body.get("tool_choice") != "none":
        call = {"id": "call_stub_1", "type": "function", "function": {"name": "trail_summary", "arguments": '{"days": 7}'}}
        return completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls", (2400, 30, 0))
    evidence = [m.group(1) for t in tool_messages for m in _OK_READ.finditer(str(t.get("content") or ""))][:4]
    content = json.dumps({"text": ANSWER_TEXT, "evidence": evidence, "commands": ANSWER_COMMANDS})
    return completion({"role": "assistant", "content": content}, "stop", (2600, 80, 1792))


def build_app(script: list[dict[str, Any]] | None = None, delay: float = 0.0) -> Starlette:
    received: list[dict[str, Any]] = []
    queue = list(script or [])

    async def chat(request: Request) -> JSONResponse:
        body = await request.json()
        received.append(body)
        if delay > 0:
            await asyncio.sleep(delay)
        if script is not None:
            if not queue:
                return JSONResponse({"error": {"message": "script exhausted"}}, status_code=500)
            step = queue.pop(0)
            return JSONResponse(step.get("body", step), status_code=int(step.get("status", 200)))
        return JSONResponse(auto_reply(body))

    async def requests(_request: Request) -> JSONResponse:
        return JSONResponse(received)

    return Starlette(
        routes=[
            Route("/v1/chat/completions", chat, methods=["POST"]),
            Route("/_stub/requests", requests, methods=["GET"]),
        ]
    )


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--script", type=Path, default=None, help="JSON list of completions to serve in order")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to hold each completion (a slow model)")
    args = parser.parse_args()
    script = json.loads(args.script.read_text()) if args.script else None
    uvicorn.run(build_app(script, args.delay), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
