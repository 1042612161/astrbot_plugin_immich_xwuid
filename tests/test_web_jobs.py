import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_immich_xwuid import main


class WebJobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Bypass AstrBot initialization; all service calls below are offline mocks.
        self.plugin = main.ImmichXWUIDSync.__new__(main.ImmichXWUIDSync)
        self.plugin._sync_jobs = {}
        self.plugin._sync_job_cleanup = {}
        self.plugin._get_mappings = AsyncMock(return_value={"panel": {"1105": "折枝-竖图"}})
        immich, waves = Mock(), Mock()
        immich.albums.return_value = [{"id": "album-1", "albumName": "折枝-竖图"}]
        immich.album_assets.return_value = []
        waves.folders.return_value = [{"char_id": "1105", "char_name": "折枝"}]
        waves.images.return_value = []
        self.plugin._clients = Mock(return_value=(immich, waves))
        self.plugin._sync_jobs["job-1"] = {
            "state": "running", "progress": "正在准备同步…", "success": 0,
            "duplicate": 0, "failures": [], "total": 0, "processed": 0,
        }

    def tearDown(self):
        for job_id in list(self.plugin._sync_job_cleanup):
            self.plugin._discard_sync_job(job_id)

    async def read_status(self):
        with patch.object(main, "request", SimpleNamespace(query={"job_id": "job-1"})):
            return await self.plugin.web_sync_status()

    async def test_polling_running_job_does_not_remove_progress(self):
        response = await self.read_status()
        self.assertEqual(json.loads(response.body)["state"], "running")
        self.assertIn("job-1", self.plugin._sync_jobs)
        self.assertFalse(self.plugin._sync_job_cleanup)

    async def test_reading_completed_result_releases_record_and_timer(self):
        await self.plugin._run_web_sync("job-1", ["1105"], "panel")
        cleanup = self.plugin._sync_job_cleanup["job-1"]
        response = await self.read_status()
        self.assertEqual(json.loads(response.body)["state"], "completed")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(self.plugin._sync_jobs)
        self.assertFalse(self.plugin._sync_job_cleanup)
        self.assertTrue(cleanup.cancelled())
        self.assertEqual((await self.read_status()).status_code, 404)

    async def test_failed_result_remains_readable_once_with_failure_reason(self):
        self.plugin._clients.side_effect = RuntimeError("模拟连接失败")
        await self.plugin._run_web_sync("job-1", ["1105"], "panel")
        response = await self.read_status()
        result = json.loads(response.body)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["failures"], ["模拟连接失败"])
        self.assertFalse(self.plugin._sync_jobs)
        self.assertFalse(self.plugin._sync_job_cleanup)

    async def test_unread_completed_and_failed_results_expire_without_polling(self):
        for fail in (False, True):
            with self.subTest(failed=fail):
                self.setUp()
                if fail:
                    self.plugin._clients.side_effect = RuntimeError("模拟连接失败")
                with patch.object(main, "_UNREAD_RESULT_TIMEOUT", 0.01):
                    await self.plugin._run_web_sync("job-1", ["1105"], "panel")
                self.assertIn("job-1", self.plugin._sync_jobs)
                await asyncio.sleep(0.03)
                self.assertFalse(self.plugin._sync_jobs)
                self.assertFalse(self.plugin._sync_job_cleanup)

    async def test_cancelled_job_releases_state_immediately(self):
        self.plugin._get_mappings.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._run_web_sync("job-1", ["1105"], "panel")
        self.assertFalse(self.plugin._sync_jobs)
        self.assertFalse(self.plugin._sync_job_cleanup)


if __name__ == "__main__":
    unittest.main()
