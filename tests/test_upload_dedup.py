import base64
import hashlib
import io
import json
import re
import unittest
from uuid import UUID
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image as PillowImage

from astrbot.api.message_components import Image
from astrbot_plugin_immich_xwuid import main
from astrbot_plugin_immich_xwuid.clients import ImmichClient


class UploadDedupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.contents = []
        for color in ("blue", "red"):
            with io.BytesIO() as buffer:
                with PillowImage.new("RGB", (10, 20), color=color) as image:
                    image.save(buffer, "PNG")
                self.contents.append(buffer.getvalue())
        self.checksums = [base64.b64encode(hashlib.sha1(data).digest()).decode() for data in self.contents]
        self.requests = []
        self.assets = []
        self.check_results = [{"id": self.checksums[0], "action": "accept"}]
        self.upload_result = {"id": "new-asset", "status": "created"}
        self.attach_result = [{"id": "new-asset", "success": True}]
        self.search_error = None
        self.check_error = None
        self.upload_error = None
        self.plugin = main.ImmichXWUIDSync.__new__(main.ImmichXWUIDSync)
        self.immich = ImmichClient("https://immich.invalid", "test-key")
        self.immich.client.close()
        self.immich.client = httpx.Client(
            base_url=self.immich.base, headers={"x-api-key": "test-key"},
            transport=httpx.MockTransport(self.respond),
        )
        self.addCleanup(self.immich.client.close)
        waves = Mock()
        waves.folders.return_value = [{"char_id": "1105", "char_name": "折枝"}]
        self.plugin._clients = Mock(return_value=(self.immich, waves))
        self.plugin._get_gallery_config = AsyncMock(return_value={
            "specifications": {"panel": "竖图", "background": "横图"}, "mappings": {},
        })
        self.plugin._cfg = lambda key, default="": {
            "immich_url": "https://immich.invalid", "immich_api_key": "test-key",
        }.get(key, default)

    def respond(self, request):
        self.requests.append(request)
        self.assertEqual(request.url.host, "immich.invalid")
        self.assertEqual(request.headers["x-api-key"], "test-key")
        path = request.url.path
        if request.method == "GET" and path == "/api/albums":
            return httpx.Response(200, json=[{"id": "panel-album", "albumName": "折枝-竖图"}])
        if path == "/api/search/metadata":
            self.assertEqual(json.loads(request.read())["albumIds"], ["panel-album"])
            if self.search_error:
                return httpx.Response(self.search_error)
            return httpx.Response(200, json={"assets": {"items": self.assets, "nextPage": None}})
        if path == "/api/assets/bulk-upload-check":
            self.assertEqual(request.method, "POST")
            checksum = json.loads(request.read())["assets"][0]["checksum"]
            self.assertIn(checksum, self.checksums)
            self.assertEqual(json.loads(request.read()), {"assets": [{"id": checksum, "checksum": checksum}]})
            if self.check_error:
                return httpx.Response(self.check_error)
            return httpx.Response(200, json={"results": self.check_results})
        if request.method == "POST" and path == "/api/assets":
            if self.upload_error:
                return httpx.Response(self.upload_error)
            self.assertIn(request.headers.get("x-immich-checksum"), self.checksums)
            self.assertNotIn(b'name="checksum"', request.read())
            return httpx.Response(201, json=self.upload_result)
        if path == "/api/albums/panel-album/assets":
            self.assertEqual(request.method, "PUT")
            return httpx.Response(200, json=self.attach_result)
        self.fail(f"Unexpected request: {request.method} {path}")

    async def run_upload(self, contents=None, sources=None):
        contents = contents or [self.contents[0]]
        sources = sources or [f"https://qq.invalid/{i}.png" for i in range(len(contents))]
        event = SimpleNamespace(
            message_obj=SimpleNamespace(message=[Image(file=f"https://qq.invalid/{i}.png") for i in range(len(contents))]),
            plain_result=lambda text: text,
        )
        self.plugin._read_image = AsyncMock(side_effect=list(zip(contents, sources)))
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            messages = [message async for message in self.plugin.upload_album(event, "折枝")]
        return messages[-1]

    def paths(self, method=None):
        return [r.url.path for r in self.requests if method is None or r.method == method]

    def uploaded_filename(self):
        upload = next(r for r in reversed(self.requests) if r.url.path == "/api/assets")
        return re.search(rb'filename="([^"]+)"', upload.read()).group(1).decode()

    async def test_each_new_upload_uses_unique_character_uuid_filename(self):
        names = []
        for source in ("https://qq.invalid/same.png", "https://qq.invalid/same.png",
                       "C:/images/same.png", "https://qq.invalid/download?id=1"):
            await self.run_upload(sources=[source])
            filename = self.uploaded_filename()
            self.assertRegex(filename, r"^折枝_[0-9a-f]{32}\.png$")
            token = filename.removeprefix("折枝_").removesuffix(".png")
            self.assertEqual(UUID(hex=token).version, 4)
            names.append(filename)
        self.assertEqual(len(set(names)), len(names))

    async def test_filename_extension_follows_bytes_instead_of_source_suffix(self):
        for image_format, extension in (("JPEG", "jpg"), ("PNG", "png"),
                                        ("WEBP", "webp"), ("GIF", "gif"),
                                        ("BMP", "bmp"), ("TIFF", "tiff")):
            with self.subTest(image_format=image_format):
                with io.BytesIO() as buffer:
                    with PillowImage.new("RGB", (10, 20), color="blue") as image:
                        image.save(buffer, image_format)
                    data = buffer.getvalue()
                checksum = base64.b64encode(hashlib.sha1(data).digest()).decode()
                self.checksums.append(checksum)
                self.check_results = [{"id": checksum, "action": "accept"}]
                await self.run_upload([data], ["https://qq.invalid/misleading.jpeg"])
                self.assertRegex(self.uploaded_filename(), rf"^折枝_[0-9a-f]{{32}}\.{extension}$")

    async def test_skip_and_reuse_do_not_generate_or_update_filenames(self):
        with patch.object(main, "uuid4") as uuid:
            self.assets = [{"id": "album-asset", "checksum": self.checksums[0]}]
            await self.run_upload()
            self.assets = []
            self.check_results = [{"id": self.checksums[0], "action": "reject", "reason": "duplicate",
                                   "assetId": "owned-asset", "isTrashed": False}]
            self.attach_result = [{"id": "owned-asset", "success": True}]
            await self.run_upload()
            uuid.assert_not_called()
        self.assertNotIn("/api/assets", self.paths())
        self.assertFalse(any(r.method == "PATCH" for r in self.requests))

    async def test_shared_album_same_bytes_skip_without_upload_or_attach(self):
        self.assets = [{"id": "other-user-asset", "checksum": self.checksums[0], "ownerId": "other-user"}]
        message = await self.run_upload()
        self.assertIn("已存在", message)
        self.assertIn("跳过", message)
        self.assertNotIn("/api/assets", self.paths())
        self.assertNotIn("/api/assets/bulk-upload-check", self.paths())
        self.assertEqual(self.paths("PUT"), [])

    async def test_owned_duplicate_is_attached_without_upload(self):
        self.check_results = [{"id": self.checksums[0], "action": "reject", "reason": "duplicate", "assetId": "owned-asset", "isTrashed": False}]
        self.attach_result = [{"id": "owned-asset", "success": True}]
        message = await self.run_upload()
        self.assertIn("复用", message)
        self.assertNotIn("/api/assets", self.paths())
        attach = next(r for r in self.requests if r.method == "PUT")
        self.assertEqual(json.loads(attach.read()), {"ids": ["owned-asset"]})

    async def test_new_upload_sets_checksum_header_and_reports_success(self):
        message = await self.run_upload()
        self.assertIn("新上传：1", message)
        self.assertEqual(self.paths("POST"), ["/api/search/metadata", "/api/assets/bulk-upload-check", "/api/assets"])

    async def test_concurrent_upload_duplicate_reuses_returned_asset(self):
        self.upload_result = {"id": "race-asset", "status": "duplicate"}
        self.attach_result = [{"id": "race-asset", "success": True}]
        message = await self.run_upload()
        self.assertIn("复用已有图片并加入相册：1", message)

    async def test_album_attach_duplicate_is_skipped_not_failed(self):
        self.attach_result = [{"id": "new-asset", "success": False, "error": "duplicate"}]
        message = await self.run_upload()
        self.assertIn("目标相册已存在，跳过：1", message)
        self.assertIn("失败：0", message)

    async def test_trashed_duplicate_is_not_restored_attached_or_uploaded(self):
        self.check_results = [{"id": self.checksums[0], "action": "reject", "reason": "duplicate", "assetId": "trashed-asset", "isTrashed": True}]
        message = await self.run_upload()
        self.assertIn("回收站", message)
        self.assertEqual(self.paths("PUT"), [])
        self.assertNotIn("/api/assets", self.paths())

    async def test_repeated_bytes_in_one_command_upload_once(self):
        message = await self.run_upload([self.contents[0], self.contents[0]])
        self.assertEqual(self.paths().count("/api/assets"), 1)
        self.assertEqual(self.paths().count("/api/search/metadata"), 1)
        self.assertIn("新上传：1", message)
        self.assertIn("目标相册已存在，跳过：1", message)

    async def test_checksum_cache_is_not_retained_between_commands(self):
        for _ in range(2):
            self.requests.clear()
            message = await self.run_upload()
            self.assertEqual(self.paths().count("/api/search/metadata"), 1)
            self.assertEqual(self.paths().count("/api/assets"), 1)
            self.assertIn("新上传：1", message)

    async def test_album_or_preflight_failure_never_uploads(self):
        for field in ("search_error", "check_error"):
            with self.subTest(field=field):
                self.requests.clear()
                setattr(self, field, 403)
                message = await self.run_upload()
                self.assertIn("失败", message)
                self.assertNotIn("/api/assets", self.paths())
                self.assertEqual(self.paths("PUT"), [])
                setattr(self, field, None)

    async def test_invalid_preflight_response_never_uploads(self):
        for result in ([], [{"id": "wrong-id", "action": "accept"}],
                       [{"id": self.checksums[0], "action": "reject", "reason": "duplicate"}],
                       [{"id": self.checksums[0], "action": "reject", "reason": "unsupported"}]):
            with self.subTest(result=result):
                self.requests.clear()
                self.check_results = result
                message = await self.run_upload()
                self.assertIn("失败", message)
                self.assertNotIn("/api/assets", self.paths())

    async def test_missing_album_checksum_fails_closed(self):
        self.assets = [{"id": "asset-without-checksum"}]
        message = await self.run_upload()
        self.assertIn("校验和", message)
        self.assertNotIn("/api/assets", self.paths())

    async def test_failed_first_image_does_not_poison_same_batch_checksum(self):
        self.attach_result = [{"id": "new-asset", "success": False, "error": "no_permission"}]
        message = await self.run_upload([self.contents[0], self.contents[0]])
        self.assertEqual(self.paths().count("/api/assets"), 2)
        self.assertIn("失败：2", message)

    async def test_upload_failure_continues_with_next_image(self):
        original = self.respond
        uploads = 0

        def respond(request):
            nonlocal uploads
            if request.url.path == "/api/assets" and request.method == "POST":
                uploads += 1
                if uploads == 1:
                    return httpx.Response(503)
            return original(request)

        self.respond = respond
        message = await self.run_upload([self.contents[0], self.contents[0]])
        self.assertEqual(uploads, 2)
        self.assertIn("新上传：1", message)
        self.assertIn("失败：1", message)

    async def test_invalid_upload_response_is_not_attached(self):
        for payload in ({"status": "created"}, {"id": "asset"}, {"id": "asset", "status": "replaced"}):
            with self.subTest(payload=payload):
                self.requests.clear()
                self.upload_result = payload
                message = await self.run_upload()
                self.assertIn("有效的资产编号或上传状态", message)
                self.assertEqual(self.paths("PUT"), [])

    async def test_missing_album_is_created_and_shared_then_checked(self):
        self.immich.albums = Mock(return_value=[])
        self.immich.ensure_album = Mock(return_value={"id": "panel-album", "albumName": "折枝-竖图"})
        self.immich.share_album_with_all_editors = Mock()
        message = await self.run_upload()
        self.assertIn("新上传：1", message)
        self.immich.ensure_album.assert_called_once_with("折枝-竖图")
        self.immich.share_album_with_all_editors.assert_called_once_with("panel-album")

    async def test_preflight_timeout_continues_with_next_image(self):
        original = self.respond
        checks = 0

        def respond(request):
            nonlocal checks
            if request.url.path.endswith("/bulk-upload-check"):
                checks += 1
                if checks == 1:
                    raise httpx.ReadTimeout("offline timeout", request=request)
                self.check_results = [{"id": self.checksums[1], "action": "accept"}]
            return original(request)

        self.immich.client.close()
        self.immich.client = httpx.Client(base_url=self.immich.base, headers={"x-api-key": "test-key"}, transport=httpx.MockTransport(respond))
        self.addCleanup(self.immich.client.close)
        message = await self.run_upload(self.contents)
        self.assertEqual(checks, 2)
        self.assertIn("新上传：1", message)
        self.assertIn("失败：1", message)


if __name__ == "__main__":
    unittest.main()
