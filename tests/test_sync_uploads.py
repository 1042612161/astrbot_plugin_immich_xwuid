import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from PIL import Image
from job_helpers import setup_jobs, make_job

from astrbot_plugin_immich_xwuid.clients import WavesClient
from astrbot_plugin_immich_xwuid.main import ImmichXWUIDSync


class SyncUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_and_chat_sync_use_csrf_header_and_confirm_correct_role(self):
        for entry in ("web", "chat"):
            for target, code, size in (("panel", "card", (10, 20)), ("background", "bg", (20, 10))):
                with self.subTest(entry=entry, target=target):
                    with io.BytesIO() as buffer:
                        with Image.new("RGB", size) as image:
                            image.save(buffer, "JPEG")
                        data = buffer.getvalue()
                    writes = []

                    def respond(request):
                        if request.method == "POST":
                            if request.headers.get("X-Waves-Panel-Edit") != "1":
                                return httpx.Response(403, json={"detail": "Cross-site request rejected"})
                            writes.append(request)
                        if request.url.path.endswith("/folders"):
                            return httpx.Response(200, json={"folders": [{"char_id": "1306", "char_name": "奥古斯塔"}]})
                        if request.url.path.endswith("/images"):
                            return httpx.Response(200, json={"images": []})
                        if request.url.path.endswith("/tmp/upload"):
                            self.assertIn(data, request.read())
                            return httpx.Response(200, json={"token": "offline-token"})
                        if request.url.path.endswith("/confirm"):
                            self.assertEqual(json.loads(request.read()), {"token": "offline-token", "type": code, "char_id": "1306"})
                            return httpx.Response(200, json={"ok": True, "name": "1306_offline.jpg"})
                        self.fail(f"Unexpected request: {request.method} {request.url.path}")

                    waves = WavesClient("https://waves.invalid", "admin", "test-only", transport=httpx.MockTransport(respond))
                    self.addCleanup(waves.client.close)
                    immich = Mock()
                    immich.albums.return_value = [{"albumName": "offline-album", "id": "offline-album-id"}]
                    immich.album_assets.return_value = [{"id": "offline-asset", "originalFileName": "IMG_8082.JPG"}]
                    immich.download_asset.return_value = (data, "image/jpeg")
                    plugin = ImmichXWUIDSync.__new__(ImmichXWUIDSync)
                    plugin._clients = Mock(return_value=(immich, waves))
                    plugin._get_mappings = AsyncMock(return_value={target: {"1306": "offline-album"}})
                    setup_jobs(plugin)
                    try:
                        if entry == "web":
                            await plugin._run_web_sync(make_job(["1306"], target))
                            result = (await plugin._read_latest_sync())
                            self.assertEqual(result["success"], 1)
                            self.assertEqual(result["failures"], [])
                        else:
                            command = "同步面板图相册" if target == "panel" else "同步背景图相册"
                            event = SimpleNamespace(plain_result=lambda text: text)
                            messages = [message async for message in plugin._sync(event, command, "1306")]
                            self.assertIn("成功 1", messages[-1])
                            self.assertIn("失败 0", messages[-1])
                        self.assertEqual([r.url.path.rsplit("/", 1)[-1] for r in writes], ["upload", "confirm"])
                        immich.ensure_album.assert_not_called()
                        immich.add_to_album.assert_not_called()
                    finally:
                        await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
