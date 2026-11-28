import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_immich_xwuid import main
from job_helpers import setup_jobs, make_job


class WebJobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = main.ImmichXWUIDSync.__new__(main.ImmichXWUIDSync)
        self.storage = setup_jobs(self.plugin)
        self.plugin._get_mappings = AsyncMock(return_value={"panel": {"1105": "折枝-竖图"}})
        self.immich, self.waves = Mock(), Mock()
        self.immich.albums.return_value = [{"id": "album-1", "albumName": "折枝-竖图"}]
        self.immich.album_assets.return_value = []
        self.waves.folders.return_value = [{"char_id": "1105", "char_name": "折枝"}]
        self.waves.images.return_value = []
        self.plugin._clients = Mock(return_value=(self.immich, self.waves))

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def start(self):
        job_id = await self.plugin._start_sync("panel", ["1105"])
        task = self.plugin._active_sync_task
        return job_id, task

    async def test_close_page_without_polling_does_not_stop_worker(self):
        job_id, task = await self.start()
        await task
        for _ in range(2):
            response = await self.plugin.web_sync_status()
            result = json.loads(response.body)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["job_id"], job_id)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIsNone(self.plugin._active_sync_task)
        self.assertEqual(list(self.storage), [main._LATEST_SYNC_KEY])

    async def test_completed_record_is_replaced_instead_of_accumulating(self):
        first, task = await self.start()
        await task
        second, task = await self.start()
        await task
        self.assertNotEqual(first, second)
        self.assertEqual(self.storage[main._LATEST_SYNC_KEY]["job_id"], second)
        self.assertEqual(len(self.storage), 1)
        self.assertFalse(hasattr(self.plugin, "_sync_jobs"))

    async def test_progress_is_persisted_in_batches_with_a_final_snapshot(self):
        assets = [{"id": str(i), "originalFileName": f"{i}.png"} for i in range(6)]
        self.immich.album_assets.return_value = assets
        self.waves.images.return_value = [{"name": x["originalFileName"]} for x in assets]
        _, task = await self.start()
        await task
        snapshots = [call.args[1] for call in self.plugin.put_kv_data.call_args_list]
        self.assertTrue(any(x["state"] == "running" and x["processed"] == 5 for x in snapshots))
        result = await self.plugin._read_latest_sync()
        self.assertEqual((result["processed"], result["total"], result["duplicate"]), (6, 6, 6))
        self.assertEqual(result["state"], "completed")
        self.immich.download_asset.assert_not_called()

    async def test_concurrent_web_and_command_sync_share_single_worker(self):
        gate = asyncio.Event()
        original = self.plugin._get_mappings
        async def blocked():
            await gate.wait()
            return await original()
        self.plugin._get_mappings = blocked
        _, task = await self.start()
        response_request = SimpleNamespace(json=AsyncMock(return_value={"type": "panel", "roles": ["1105"]}))
        with patch.object(main, "request", response_request):
            response = await self.plugin.web_sync()
        self.assertEqual(response.status_code, 409)
        event = SimpleNamespace(plain_result=lambda text: text)
        messages = [text async for text in self.plugin._sync(event, "同步面板图相册", "1105")]
        self.assertIn("已有同步任务", messages[-1])
        self.assertEqual((await self.plugin._read_latest_sync())["state"], "running")
        gate.set()
        await task

    async def test_restart_marks_running_record_interrupted_without_replay(self):
        self.storage[main._LATEST_SYNC_KEY] = {**make_job(), "instance_id": "old-process"}
        await self.plugin.initialize()
        result = await self.plugin._read_latest_sync()
        self.assertEqual(result["state"], "interrupted")
        self.assertIn("未自动重跑", result["progress"])
        self.plugin._clients.assert_not_called()
        self.assertIsNone(self.plugin._active_sync_task)

    async def test_lost_chat_consumer_does_not_cancel_accepted_background_task(self):
        gate = asyncio.Event()
        self.plugin._get_mappings = AsyncMock(side_effect=lambda: None)
        async def blocked():
            await gate.wait()
            return {"panel": {"1105": "折枝-竖图"}}
        self.plugin._get_mappings = blocked
        stream = self.plugin._sync(SimpleNamespace(plain_result=lambda text: text), "同步面板图相册", "1105")
        await anext(stream)
        task = self.plugin._active_sync_task
        listener = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        self.assertFalse(task.cancelled())
        self.assertFalse(task.done())
        gate.set()
        await task
        self.assertEqual((await self.plugin._read_latest_sync())["state"], "completed")

    async def test_status_cannot_mark_a_task_interrupted_between_save_and_start(self):
        saved = asyncio.Event()
        release = asyncio.Event()
        put = self.plugin.put_kv_data
        async def delayed_save(key, value):
            await put(key, value)
            if not saved.is_set():
                saved.set()
                await release.wait()
        self.plugin.put_kv_data = delayed_save
        starter = asyncio.create_task(self.start())
        await saved.wait()
        reader = asyncio.create_task(self.plugin._read_latest_sync())
        await asyncio.sleep(0)
        self.assertFalse(reader.done())
        release.set()
        _, task = await starter
        self.assertNotEqual((await reader)["state"], "interrupted")
        await task

    async def test_plugin_unload_persists_interrupted_status(self):
        self.plugin._get_mappings = AsyncMock(side_effect=asyncio.CancelledError)
        _, task = await self.start()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((await self.plugin._read_latest_sync())["state"], "interrupted")
        self.assertIsNone(self.plugin._active_sync_task)

    async def test_terminate_before_worker_starts_still_marks_interrupted(self):
        await self.start()
        await self.plugin.terminate()
        self.assertEqual((await self.plugin._read_latest_sync())["state"], "interrupted")
        with self.assertRaisesRegex(RuntimeError, "正在关闭"):
            await self.start()

    async def test_failed_state_and_bounded_failure_details_remain_readable(self):
        self.plugin._clients.side_effect = RuntimeError("模拟连接失败")
        _, task = await self.start()
        await task
        result = await self.plugin._read_latest_sync()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["failures"], ["模拟连接失败"])
        self.assertEqual(result["failure_count"], 1)
        job = make_job()
        for i in range(150):
            self.plugin._record_sync_failure(job, str(i))
        self.assertEqual(job["failure_count"], 150)
        self.assertEqual(len(job["failures"]), 99)
        self.assertTrue(job["failure_details_truncated"])

    async def test_status_available_when_catalog_is_unavailable(self):
        self.storage[main._LATEST_SYNC_KEY] = {**make_job(), "state": "completed"}
        self.plugin._api_data = AsyncMock(side_effect=RuntimeError("服务不可达"))
        self.assertEqual((await self.plugin.web_data()).status_code, 500)
        self.assertEqual(json.loads((await self.plugin.web_sync_status()).body)["state"], "completed")

    async def test_disk_record_restores_in_new_plugin_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.json"
            async def put(key, value):
                path.write_text(json.dumps(value), encoding="utf-8")
            async def get(key, default):
                return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
            self.plugin.put_kv_data, self.plugin.get_kv_data = put, get
            _, task = await self.start()
            await task
            other = main.ImmichXWUIDSync.__new__(main.ImmichXWUIDSync)
            setup_jobs(other)
            other.get_kv_data, other.put_kv_data = get, put
            other._sync_instance_id = "restarted"
            await other.initialize()
            self.assertEqual((await other._read_latest_sync())["state"], "completed")
            self.plugin.get_kv_data = AsyncMock(return_value=None)

    async def test_empty_status_and_invalid_request(self):
        self.assertEqual(json.loads((await self.plugin.web_sync_status()).body)["state"], "idle")
        for payload in ([], {"type": "invalid", "roles": ["1"]}, {"type": "panel", "roles": []}):
            with patch.object(main, "request", SimpleNamespace(json=AsyncMock(return_value=payload))):
                self.assertEqual((await self.plugin.web_sync()).status_code, 400)
