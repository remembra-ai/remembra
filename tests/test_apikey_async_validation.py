"""Cold-key verification regressions; only fixed synthetic in-memory records."""

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from remembra.auth import keys
from remembra.storage.database import Database

RAW = "rem_synthetic_async_verification_fixture"
HASH = "synthetic-verifier-not-a-credential"


class AsyncKeyValidation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="auth-regression")
        self.pool_patch = patch.object(keys, "_key_verify_pool", self.pool, create=True)
        self.pool_patch.start()
        self.cache = dict(keys._key_cache)
        keys._key_cache.clear()
        self.db = Database(":memory:")
        await self.db.connect()
        async with self.db.transaction():
            await self.db.conn.execute(
                "CREATE TABLE api_keys (id TEXT PRIMARY KEY, user_id TEXT, key_hash TEXT, "
                "key_lookup TEXT, active INTEGER, last_used_at TEXT)"
            )
            await self.db.conn.execute(
                "CREATE TABLE api_key_roles (api_key_id TEXT PRIMARY KEY, role TEXT, scopes TEXT, project_ids TEXT)"
            )
        self.manager = keys.APIKeyManager(self.db)
        self.releases = []
        self.tasks = []

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=4)
            self.assertFalse(pending, "test tasks failed to settle")
            for task in done:
                if not task.cancelled():
                    task.exception()
        await asyncio.wait_for(asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True), timeout=4)
        self.pool_patch.stop()
        keys._key_cache.clear()
        keys._key_cache.update(self.cache)
        await self.db.close()

    async def add_key(self, *, raw=RAW, key_id="fixed-key", legacy=False):
        lookup = None if legacy else keys.APIKeyManager.compute_lookup(raw)
        async with self.db.transaction():
            await self.db.conn.execute("INSERT INTO api_keys VALUES (?, 'fixed-user', ?, ?, 1, NULL)", (key_id, HASH, lookup))
            await self.db.conn.execute("INSERT INTO api_key_roles VALUES (?, 'editor', 'read,write', 'fixed-project')", (key_id,))
        return lookup

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def wait_entered(self, event):
        self.assertTrue(await asyncio.wait_for(asyncio.to_thread(event.wait, 2), timeout=3))

    def blocked_verifier(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def verify(raw, stored):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test worker release missing")
            return True

        self.manager.verify_key = verify
        return entered, release

    async def finish(self, task):
        done, _ = await asyncio.wait({task}, timeout=4)
        self.assertIn(task, done, "validation did not settle")
        return task.result()

    async def check_offloop(self, legacy):
        await self.add_key(legacy=legacy)
        loop_thread = threading.get_ident()
        seen = []
        self.manager.verify_key = lambda *args: seen.append(threading.get_ident()) or True
        result = await self.manager.validate_key(RAW, record_use=False)
        self.assertIsNotNone(result)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], loop_thread)

    async def test_indexed_crypto_is_offloop(self):
        await self.check_offloop(False)

    async def test_legacy_crypto_is_offloop(self):
        await self.check_offloop(True)

    async def test_revoked_during_worker_is_rejected(self):
        lookup = await self.add_key()
        entered, release = self.blocked_verifier()
        task = self.spawn(self.manager.validate_key(RAW))
        await self.wait_entered(entered)
        await self.db.conn.execute("UPDATE api_keys SET active=0 WHERE id='fixed-key'")
        await self.db.conn.commit()
        release.set()
        self.assertIsNone(await self.finish(task))
        self.assertNotIn(lookup, keys._key_cache)

    async def test_hash_replacement_during_worker_is_rejected(self):
        lookup = await self.add_key()
        entered, release = self.blocked_verifier()
        task = self.spawn(self.manager.validate_key(RAW))
        await self.wait_entered(entered)
        await self.db.conn.execute("UPDATE api_keys SET key_hash='replacement' WHERE id='fixed-key'")
        await self.db.conn.commit()
        release.set()
        self.assertIsNone(await self.finish(task))
        self.assertNotIn(lookup, keys._key_cache)

    async def test_owner_replacement_during_worker_is_rejected(self):
        lookup = await self.add_key()
        entered, release = self.blocked_verifier()
        task = self.spawn(self.manager.validate_key(RAW))
        await self.wait_entered(entered)
        await self.db.conn.execute("UPDATE api_keys SET user_id='other-user' WHERE id='fixed-key'")
        await self.db.conn.commit()
        release.set()
        self.assertIsNone(await self.finish(task))
        self.assertNotIn(lookup, keys._key_cache)

    async def test_role_change_during_worker_uses_fresh_restrictions(self):
        await self.add_key()
        entered, release = self.blocked_verifier()
        task = self.spawn(self.manager.validate_key(RAW, record_use=False))
        await self.wait_entered(entered)
        await self.db.conn.execute(
            "UPDATE api_key_roles SET role='viewer', scopes='read', project_ids='narrow' WHERE api_key_id='fixed-key'"
        )
        await self.db.conn.commit()
        release.set()
        result = await self.finish(task)
        self.assertEqual((result["role"], result["scopes"], result["project_ids"]), ("viewer", ["read"], ["narrow"]))

    async def test_runtime_denylist_during_worker_rejects(self):
        lookup = await self.add_key()
        entered, release = self.blocked_verifier()
        denied = set()
        with patch.object(keys, "revoked_key_lookups", lambda: frozenset(denied)):
            task = self.spawn(self.manager.validate_key(RAW))
            await self.wait_entered(entered)
            denied.add(lookup)
            release.set()
            self.assertIsNone(await self.finish(task))
        self.assertNotIn(lookup, keys._key_cache)

    async def test_legacy_backfill_and_record_use_are_preserved(self):
        await self.add_key(legacy=True)
        self.manager.verify_key = lambda *args: True
        result = await self.manager.validate_key(RAW)
        self.assertEqual(result["role"], "editor")
        cursor = await self.db.conn.execute("SELECT key_lookup, last_used_at FROM api_keys WHERE id='fixed-key'")
        row = await cursor.fetchone()
        await cursor.close()
        self.assertEqual(row[0], self.manager.compute_lookup(RAW))
        self.assertIsNotNone(row[1])

    async def test_cancellation_cannot_cache_completed_worker(self):
        lookup = await self.add_key()
        entered, release = self.blocked_verifier()
        task = self.spawn(self.manager.validate_key(RAW))
        await self.wait_entered(entered)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        self.assertIn(task, done)
        self.assertTrue(task.cancelled())
        release.set()
        await asyncio.wait_for(asyncio.to_thread(self.pool.shutdown, wait=True), timeout=4)
        self.assertNotIn(lookup, keys._key_cache)
        cursor = await self.db.conn.execute("SELECT last_used_at FROM api_keys WHERE id='fixed-key'")
        self.assertIsNone((await cursor.fetchone())[0])
        await cursor.close()

    async def test_workers_stay_bounded_after_request_cancellation(self):
        self.assertEqual(keys._KEY_VERIFY_WORKERS, 2)
        for n in range(6):
            await self.add_key(raw=f"rem_synthetic_worker_{n}", key_id=f"fixed-{n}")
        release, two_entered = threading.Event(), threading.Event()
        self.releases.append(release)
        lock = threading.Lock()
        active = maximum = entered = 0

        def verify(*args):
            nonlocal active, maximum, entered
            with lock:
                active += 1
                entered += 1
                maximum = max(maximum, active)
                if entered >= 2:
                    two_entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError("worker release missing")
                return True
            finally:
                with lock:
                    active -= 1

        self.manager.verify_key = verify
        tasks = [self.spawn(self.manager.validate_key(f"rem_synthetic_worker_{n}")) for n in range(4)]
        await self.wait_entered(two_entered)
        for task in tasks:
            task.cancel()
        done, _ = await asyncio.wait(tasks, timeout=2)
        self.assertEqual(len(done), 4)
        self.assertTrue(all(task.cancelled() for task in tasks))
        later = [self.spawn(self.manager.validate_key(f"rem_synthetic_worker_{n}")) for n in (4, 5)]
        release.set()
        for task in later:
            self.assertIsNotNone(await self.finish(task))
        self.assertLessEqual(maximum, 2)
        self.assertEqual(active, 0)

    async def test_outer_rollback_does_not_publish_cold_cache(self):
        lookup = await self.add_key()
        self.manager.verify_key = lambda *args: True
        with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
            async with self.db.transaction():
                result = await self.manager.validate_key(RAW)
                self.assertIsNotNone(result)
                self.assertNotIn(lookup, keys._key_cache)
                raise RuntimeError("synthetic rollback")
        self.assertNotIn(lookup, keys._key_cache)

    async def test_metadata_failure_does_not_publish_cache(self):
        lookup = await self.add_key()
        self.manager.verify_key = lambda *args: True

        async def fail(*args):
            raise RuntimeError("synthetic metadata failure")

        with (
            patch.object(self.db, "update_api_key_last_used", fail),
            self.assertRaisesRegex(RuntimeError, "synthetic metadata failure"),
        ):
            await self.manager.validate_key(RAW)
        self.assertNotIn(lookup, keys._key_cache)

    async def test_mismatch_and_unknown_key_still_fail_closed(self):
        lookup = await self.add_key()
        self.manager.verify_key = lambda *args: False
        self.assertIsNone(await self.manager.validate_key(RAW))
        self.assertNotIn(lookup, keys._key_cache)
        self.manager.verify_key = lambda *args: self.fail("unknown key must not verify")
        self.assertIsNone(await self.manager.validate_key("rem_synthetic_unknown"))
        self.assertIsNone(await self.manager.validate_key("bad-prefix"))

    async def test_published_legacy_denial_verifies_offloop(self):
        await self.add_key(legacy=True)
        lookup = self.manager.compute_lookup(RAW)
        seen = []
        loop_thread = threading.get_ident()
        self.manager.verify_key = lambda *args: seen.append(threading.get_ident()) or True

        async def noop(*args):
            return None

        with (
            patch.object(keys, "revoked_key_lookups", lambda: frozenset({lookup})),
            patch.object(keys, "_close_dead_sockets", noop),
        ):
            self.assertIsNone(await self.manager.validate_key(RAW))
        self.assertNotEqual(seen, [])
        self.assertTrue(all(t != loop_thread for t in seen))
        cursor = await self.db.conn.execute("SELECT active FROM api_keys WHERE id='fixed-key'")
        self.assertEqual((await cursor.fetchone())[0], 0)
        await cursor.close()

    async def test_lookup_replacement_during_worker_is_rejected(self):
        lookup = await self.add_key()
        entered, release = self.blocked_verifier()
        task = self.spawn(self.manager.validate_key(RAW))
        await self.wait_entered(entered)
        await self.db.conn.execute("UPDATE api_keys SET key_lookup='other-lookup' WHERE id='fixed-key'")
        await self.db.conn.commit()
        release.set()
        self.assertIsNone(await self.finish(task))
        self.assertNotIn(lookup, keys._key_cache)

    async def test_concurrent_legacy_backfill_is_accepted(self):
        await self.add_key(legacy=True)
        entered, release = self.blocked_verifier()
        task = self.spawn(self.manager.validate_key(RAW, record_use=False))
        await self.wait_entered(entered)
        await self.db.set_api_key_lookup("fixed-key", self.manager.compute_lookup(RAW))
        release.set()
        self.assertIsNotNone(await self.finish(task))

    async def test_commit_failure_never_publishes_cache(self):
        lookup = await self.add_key()
        self.manager.verify_key = lambda *args: True

        async def fail():
            raise RuntimeError("synthetic commit failure")

        with patch.object(self.db._connection, "commit", fail), self.assertRaisesRegex(RuntimeError, "synthetic commit failure"):
            await self.manager.validate_key(RAW)
        self.assertNotIn(lookup, keys._key_cache)

    async def test_cancellation_inside_metadata_rolls_back_without_cache(self):
        lookup = await self.add_key()
        self.manager.verify_key = lambda *args: True
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            entered.set()
            await release.wait()

        with patch.object(self.db, "update_api_key_last_used", blocked):
            task = self.spawn(self.manager.validate_key(RAW))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=2)
                self.assertIn(task, done)
                self.assertTrue(task.cancelled())
            finally:
                release.set()
        self.assertNotIn(lookup, keys._key_cache)

    async def test_warm_cache_keeps_role_and_revocation_rechecks(self):
        lookup = await self.add_key()
        self.manager.verify_key = lambda *args: True
        self.assertIsNotNone(await self.manager.validate_key(RAW, record_use=False))
        self.manager.verify_key = lambda *args: self.fail("warm/unknown key must not verify")
        await self.db.conn.execute(
            "UPDATE api_key_roles SET role='viewer', scopes='read', project_ids='narrow' WHERE api_key_id='fixed-key'"
        )
        await self.db.conn.commit()
        current = await self.manager.validate_key(RAW, record_use=False)
        self.assertEqual((current["role"], current["scopes"], current["project_ids"]), ("viewer", ["read"], ["narrow"]))
        await self.db.conn.execute("UPDATE api_keys SET active=0 WHERE id='fixed-key'")
        await self.db.conn.commit()
        self.assertIsNone(await self.manager.validate_key(RAW, record_use=False))
        self.assertNotIn(lookup, keys._key_cache)

    async def test_real_fixed_crypto_verifies_offloop(self):
        import bcrypt

        verifier = bcrypt.hashpw(RAW.encode(), b"$2b$12$abcdefghijklmnopqrstuu").decode()
        await self.add_key()
        await self.db.conn.execute("UPDATE api_keys SET key_hash=? WHERE id='fixed-key'", (verifier,))
        await self.db.conn.commit()
        source_verify = keys.APIKeyManager.verify_key
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        progress = threading.Event()
        observed = []

        def verify(raw, stored):
            loop.call_soon_threadsafe(progress.set)
            ok = source_verify(raw, stored)
            observed.append((threading.get_ident() != loop_thread, progress.is_set()))
            return ok

        self.manager.verify_key = verify
        self.assertIsNotNone(await self.manager.validate_key(RAW, record_use=False))
        self.assertEqual(observed, [(True, True)])
