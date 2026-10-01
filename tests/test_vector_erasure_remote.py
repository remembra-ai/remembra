"""Kill a real pending worker after dispatch, then commit its bytes to server Qdrant.

Requires an isolated loopback Qdrant service, never a customer endpoint. The
HTTP proxy holds an already received upsert across SIGKILL and forwards it
only after deletion/recovery succeeded. SQLite, authenticated delete routes,
the worker, HTTP serialization and remote Qdrant are real; embeddings are fake.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from qdrant_client import AsyncQdrantClient
from starlette.responses import Response

from remembra.account.erasure import AccountEraser
from remembra.extraction import background
from remembra.storage.database import Database
from remembra.storage.pending_embeddings import PendingEmbeddingQueue, PendingEmbeddingWorker
from remembra.storage.qdrant import QdrantStore
from remembra.storage.vector_mutations import VectorErasureReconciler
from tests._ret_harness import DIM, VecEmbeddings, quota_error
from tests.security_harness import make_settings
from tests.test_ret_api_degraded import _app


async def _worker(path: str, proxy: str, collection: str) -> None:
    db = Database(path)
    await db.connect()
    store = QdrantStore(make_settings(qdrant_collection=collection, embedding_dimensions=DIM))
    store._client = AsyncQdrantClient(url=proxy, prefer_grpc=False, timeout=30)
    try:
        await PendingEmbeddingWorker(PendingEmbeddingQueue(db), db, store, VecEmbeddings()).run_once()
    finally:
        await store.close()
        await db.close()


@pytest.mark.parametrize("target", ["memory", "account"])
async def test_killed_worker_remote_completion_is_removed_after_restart(tmp_path, target):
    endpoint = os.environ.get("REMEMBRA_TEST_QDRANT_URL")
    if not endpoint:
        pytest.skip("isolated server-Qdrant gate; local-mode tests are not this proof")
    parts = urlsplit(endpoint)
    assert parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
    assert not parts.username and not parts.password and parts.path in {"", "/"} and not parts.query and not parts.fragment
    async with httpx.AsyncClient(trust_env=False) as ready:
        for _ in range(120):
            try:
                if (await ready.get(endpoint + "/readyz")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.5)
        else:
            pytest.fail("isolated Qdrant service never became ready")

    ctx, h, service, local, emb, headers = await _app(tmp_path)
    remote = QdrantStore(make_settings(qdrant_collection="erasure_gate_" + uuid4().hex, embedding_dimensions=DIM))
    remote._client = AsyncQdrantClient(url=endpoint, prefer_grpc=False, timeout=10)
    process = None
    proxy_task = None
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    received, forward, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    forwarded_status = []
    proxy_server = None
    restart = None
    try:
        await remote.init_collection()
        service.qdrant = remote
        emb.fail = quota_error()
        stored = await h.client.post(
            "/api/v1/memories",
            headers=headers,
            json={"content": "Synthetic pending erasure crash", "project_id": "p", "skip_extraction": True},
        )
        assert stored.status_code == 201 and stored.json()["status"] == "pending", stored.text
        mid = stored.json()["id"]
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "PUT", "POST", "DELETE"])
        async def proxy(request: Request, path: str):
            body = await request.body()
            is_upsert = request.method == "PUT" and path.endswith("/points")
            if is_upsert:
                received.set()
                await forward.wait()
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                response = await client.request(
                    request.method,
                    endpoint + "/" + path,
                    params=request.query_params,
                    content=body,
                    headers={"content-type": request.headers.get("content-type", "application/json")},
                )
            if is_upsert:
                forwarded_status.append(response.status_code)
                committed.set()
            return Response(response.content, status_code=response.status_code, media_type="application/json")

        proxy_server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        proxy_task = asyncio.create_task(proxy_server.serve(sockets=[listener]))
        for _ in range(100):
            if proxy_server.started:
                break
            await asyncio.sleep(0.01)
        assert proxy_server.started
        proxy_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        code = "import asyncio,sys; from tests.test_vector_erasure_remote import _worker; asyncio.run(_worker(*sys.argv[1:]))"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            h.db.db_path,
            proxy_url,
            remote.collection_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(received.wait(), 20)
        process.kill()
        assert await asyncio.wait_for(process.wait(), 5) == -9
        # OS death frees the cooperating writer lock. The canonical deletion
        # commits before the held HTTP request can reach remote Qdrant.
        if target == "account":
            await asyncio.wait_for(AccountEraser(h.db, remote).erase("tenant-a"), 5)
        else:
            deleted = await asyncio.wait_for(h.client.delete("/api/v1/memories", headers=headers, params={"memory_id": mid}), 5)
            assert deleted.status_code == 200, deleted.text
        restart = Database(h.db.db_path)
        await restart.connect()
        recovery = VectorErasureReconciler(restart, remote)
        assert await recovery.run_once() == 1
        assert not await remote.existing_ids([mid])
        # The received bytes survive their originating process and commit late.
        forward.set()
        await asyncio.wait_for(committed.wait(), 10)
        assert forwarded_status == [200]
        assert await remote.existing_ids([mid]) == {mid}
        assert await restart.get_memory(mid) is None
        assert await VectorErasureReconciler(restart, remote).run_once() == 1
        assert not await remote.existing_ids([mid])
        assert await VectorErasureReconciler(restart, remote).run_once() == 1  # marker survives success
        cursor = await restart.conn.execute("SELECT COUNT(*) FROM memories_fts WHERE id = ?", (mid,))
        assert (await cursor.fetchone())[0] == 0
        cursor = await restart.conn.execute("SELECT memory_id FROM vector_erasure_markers")
        assert [r[0] for r in await cursor.fetchall()] == [mid]
        if target == "account":
            cursor = await restart.conn.execute("SELECT COUNT(*) FROM erased_account_fences")
            assert (await cursor.fetchone())[0] == 1
    finally:
        forward.set()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if proxy_server is not None:
            proxy_server.should_exit = True
        if proxy_task is not None:
            await asyncio.wait_for(proxy_task, 10)
        listener.close()
        if restart is not None:
            await restart.close()
        await background.drain(timeout=5)
        try:
            client = await remote._get_client()
            if await client.collection_exists(remote.collection_name):
                await client.delete_collection(remote.collection_name)
        finally:
            await remote.close()
            await local.close()
            await ctx.__aexit__(None, None, None)
