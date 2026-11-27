import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image as PillowImage

from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage
from astrbot.core.utils.media_utils import MediaResolver

from astrbot_plugin_immich_xwuid import main


class ImageUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths, self.contents = [], []
        for index, size in enumerate([(10, 20), (20, 10)]):
            with io.BytesIO() as buffer:
                with PillowImage.new("RGB", size, color="blue") as image:
                    image.save(buffer, "PNG")
                data = buffer.getvalue()
            path = self.root / f"image-{index}.png"
            path.write_bytes(data)
            self.paths.append(path)
            self.contents.append(data)

        self.plugin = main.ImmichXWUIDSync.__new__(main.ImmichXWUIDSync)
        self.immich, self.waves = Mock(), Mock()
        self.immich.album_checksums.side_effect = lambda album_id: set()
        self.immich.check_upload.return_value = {"action": "accept"}
        self.immich.add_to_album.return_value = True
        self.immich.albums.return_value = [
            {"id": "panel-album", "albumName": "折枝-竖图"},
            {"id": "background-album", "albumName": "折枝-横图"},
        ]
        self.waves.folders.return_value = [{"char_id": "1105", "char_name": "折枝"}]
        self.plugin._clients = Mock(return_value=(self.immich, self.waves))
        self.plugin._get_gallery_config = AsyncMock(return_value={
            "specifications": {"panel": "竖图", "background": "横图"}, "mappings": {},
        })
        self.plugin._cfg = lambda key, default="": {
            "immich_url": "https://immich.invalid", "immich_api_key": "test-key",
        }.get(key, default)

    def event(self, segments):
        return SimpleNamespace(
            message_obj=SimpleNamespace(message=segments, self_id="12345"),
            bot=SimpleNamespace(call_action=AsyncMock(side_effect=RuntimeError("file not found"))),
            plain_result=lambda text: text,
            track_temporary_local_file=Mock(), untrack_temporary_local_file=Mock(),
        )

    async def upload(self, event, expected_contents, downloads=None):
        seen, uploaded = [], []
        downloads = downloads or {}

        def respond(request):
            seen.append(request)
            if request.method == "GET":
                self.assertIn(str(request.url), downloads)
                return httpx.Response(200, content=downloads[str(request.url)])
            self.assertEqual(str(request.url), "https://immich.invalid/api/assets")
            self.assertEqual(request.headers["x-api-key"], "test-key")
            content = expected_contents[len(uploaded)]
            self.assertIn(content, request.read())
            uploaded.append(content)
            return httpx.Response(200, json={"id": f"uploaded-{len(uploaded)}", "status": "created"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            messages = [message async for message in self.plugin.upload_album(event, "折枝")]
        self.assertIn("失败：0", messages[-1])
        self.assertEqual(uploaded, expected_contents)
        self.immich.ensure_album.assert_not_called()
        self.immich.share_album_with_all_editors.assert_not_called()
        return seen, messages

    async def test_preprocessed_current_and_quoted_one_or_two_images_upload_without_get_image(self):
        for quoted in (False, True):
            for count in (1, 2):
                with self.subTest(quoted=quoted, count=count):
                    self.immich.add_to_album.reset_mock()
                    images = [Image(file=f"qq-file-{i}.png", url=f"https://qq.invalid/{i}.png") for i in range(count)]
                    event = self.event([Plain("上传相册 折枝"), *(
                        [Reply(id="123", chain=images)] if quoted else images
                    )])
                    stage = PreProcessStage()
                    # Run AstrBot's real normalization, substituting only its download result.
                    with patch.object(MediaResolver, "to_path", AsyncMock(side_effect=[str(p) for p in self.paths[:count]])):
                        for image in images:
                            await stage._normalize_image_component(event, image)
                    self.assertEqual([image.url for image in images], [str(p) for p in self.paths[:count]])
                    seen, _ = await self.upload(event, self.contents[:count])
                    self.assertTrue(all(request.method == "POST" for request in seen))
                    event.bot.call_action.assert_not_awaited()
                    self.assertEqual([call.args[0] for call in self.immich.add_to_album.call_args_list],
                                     ["panel-album", "background-album"][:count])
                    self.assertTrue(all(path.exists() for path in self.paths))

    async def test_valid_local_file_takes_priority_over_http_url(self):
        event = self.event([Image(file="qq-file", path=str(self.paths[0]), url="https://qq.invalid/0.png")])
        seen, _ = await self.upload(event, [self.contents[0]])
        self.assertEqual([request.method for request in seen], ["POST"])
        event.bot.call_action.assert_not_awaited()

    async def test_missing_local_file_falls_back_to_http_url(self):
        url = "https://qq.invalid/0.png"
        event = self.event([Image(file=str(self.root / "missing.png"), url=url)])
        seen, _ = await self.upload(event, [self.contents[0]], {url: self.contents[0]})
        self.assertEqual([request.method for request in seen], ["GET", "POST"])
        event.bot.call_action.assert_not_awaited()

    async def test_unreadable_local_file_falls_back_to_http_url(self):
        url = "https://qq.invalid/0.png"
        event = self.event([Image(file=str(self.paths[0]), url=url)])
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            seen, _ = await self.upload(event, [self.contents[0]], {url: self.contents[0]})
        self.assertEqual([request.method for request in seen], ["GET", "POST"])
        event.bot.call_action.assert_not_awaited()

    async def test_http_and_https_image_components_download_and_upload(self):
        urls = ["http://qq.invalid/0.png", "https://qq.invalid/1.png"]
        event = self.event([Reply(id="123", chain=[Image(file=url) for url in urls])])
        seen, _ = await self.upload(event, self.contents, dict(zip(urls, self.contents)))
        self.assertEqual([request.method for request in seen], ["GET", "POST", "GET", "POST"])

    async def test_path_field_and_raw_reply_paths_are_supported_and_deduplicated(self):
        path = str(self.paths[0])
        event = self.event([Reply(id="123")])
        event.bot.call_action = AsyncMock(return_value={"message": [
            {"type": "image", "data": {"path": path}},
            {"type": "image", "data": {"file": path, "url": path}},
        ]})
        seen, _ = await self.upload(event, [self.contents[0]])
        self.assertEqual([request.method for request in seen], ["POST"])
        event.bot.call_action.assert_awaited_once_with(action="get_msg", message_id=123, self_id="12345")

    async def test_local_directory_is_rejected_without_http_or_onebot_requests(self):
        event = self.event([Image(file=str(self.root))])
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: self.fail("A directory must never be uploaded")
        ))
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            messages = [message async for message in self.plugin.upload_album(event, "折枝")]
        self.assertIn("本地图片路径不是普通文件", messages[-1])
        event.bot.call_action.assert_not_awaited()
        self.immich.add_to_album.assert_not_called()

    async def test_http_failure_is_reported_without_uploading(self):
        for error in (httpx.Response(403), httpx.ConnectError("offline")):
            with self.subTest(error=type(error).__name__):
                event = self.event([Image(file="https://qq.invalid/0.png")])

                def respond(request):
                    self.assertEqual(request.method, "GET")
                    if isinstance(error, Exception):
                        raise error
                    return error

                client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
                with patch.object(main.httpx, "AsyncClient", return_value=client):
                    messages = [message async for message in self.plugin.upload_album(event, "折枝")]
                self.assertIn("图片下载失败", messages[-1])
                self.immich.add_to_album.assert_not_called()

    async def test_failed_first_local_image_does_not_block_second_image(self):
        event = self.event([Image(file=str(self.root / "missing.png")), Image(file=str(self.paths[1]))])
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "uploaded-2", "status": "created"})
        ))
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            messages = [message async for message in self.plugin.upload_album(event, "折枝")]
        self.assertIn("1. 失败：本地图片不存在", messages[-1])
        self.assertIn("2. 已上传到折枝-横图", messages[-1])
        self.immich.add_to_album.assert_called_once_with("background-album", ["uploaded-2"])
        event.bot.call_action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
