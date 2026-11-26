import io
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image

from astrbot_plugin_immich_xwuid import main


class GallerySettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = main.ImmichXWUIDSync.__new__(main.ImmichXWUIDSync)
        self.storage = {"gallery_config": {
            "specifications": {"panel": "竖图", "background": "横图"},
            "mappings": {"panel": {"折枝": "手工相册"}, "background": {"折枝": "手工相册"}},
        }}
        self.plugin.get_kv_data = AsyncMock(side_effect=lambda key, default: deepcopy(self.storage.get(key, default)))

        async def save(key, value):
            self.storage[key] = deepcopy(value)

        self.plugin.put_kv_data = AsyncMock(side_effect=save)
        self.immich, self.waves = Mock(), Mock()
        self.immich.albums.return_value = [
            {"id": "manual", "albumName": "手工相册"},
            {"id": "old-panel", "albumName": "折枝-竖图"},
            {"id": "old-background", "albumName": "折枝-横图"},
            {"id": "new-panel", "albumName": "折枝-立绘"},
            {"id": "new-background", "albumName": "折枝-壁纸"},
        ]
        self.waves.folders.return_value = [{"char_id": "1105", "char_name": "折枝"}]
        self.plugin._clients = Mock(return_value=(self.immich, self.waves))

    async def save_form(self, payload):
        request = SimpleNamespace(json=AsyncMock(return_value=payload))
        with patch.object(main, "request", request):
            return await self.plugin.web_save()

    async def test_changing_specifications_discards_both_tables_and_matches_new_names(self):
        response = await self.save_form({
            "specifications": {"panel": "立绘", "background": "壁纸"},
            "mappings": {"panel": {"折枝": "手工相册"}, "background": {"折枝": "手工相册"}},
        })
        result = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(result["reset"])
        self.assertEqual(result["mappings"], {"panel": {"折枝": "折枝-立绘"}, "background": {"折枝": "折枝-壁纸"}})
        self.assertEqual(self.storage["gallery_config"]["mappings"], result["mappings"])
        self.immich.ensure_album.assert_not_called()

    async def test_changing_one_specification_also_rebuilds_other_table(self):
        response = await self.save_form({"specifications": {"panel": "立绘", "background": "横图"}})
        result = json.loads(response.body)
        self.assertEqual(result["mappings"]["background"], {"折枝": "折枝-横图"})

    async def test_missing_new_album_leaves_mapping_empty_without_creating_it(self):
        response = await self.save_form({"specifications": {"panel": "不存在", "background": "壁纸"}})
        self.assertEqual(json.loads(response.body)["mappings"]["panel"], {})
        self.immich.ensure_album.assert_not_called()

    async def test_saving_unchanged_specifications_keeps_manual_mappings(self):
        response = await self.save_form(self.storage["gallery_config"])
        result = json.loads(response.body)
        self.assertFalse(result["reset"])
        self.assertEqual(result["mappings"]["panel"], {"折枝": "手工相册"})

    async def test_invalid_specifications_or_mappings_do_not_change_saved_data(self):
        original = deepcopy(self.storage)
        payloads = [
            {"specifications": {"panel": "", "background": "横图"}},
            {"specifications": {"panel": " 相同 ", "background": "相同"}},
            {"specifications": {"panel": "换\n行", "background": "横图"}},
            {"mappings": {"panel": {"折枝": "未列出的相册"}}},
            {"mappings": {"panel": {"不存在的角色": "手工相册"}}},
            {"mappings": {"panel": []}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = await self.save_form(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.storage, original)

    async def test_refresh_and_legacy_upgrade_preserve_manual_mapping(self):
        self.storage = {"mappings": {"panel": {"折枝": "手工相册"}, "background": {}}}
        result = await self.plugin._api_data()
        self.assertEqual(result["specifications"], {"panel": "竖图", "background": "横图"})
        self.assertEqual(result["mappings"]["panel"], {"折枝": "手工相册"})
        self.assertEqual((await self.plugin._get_mappings())["panel"], {"折枝": "手工相册"})

    async def test_create_missing_albums_uses_saved_specification_names(self):
        self.storage["gallery_config"]["specifications"] = {"panel": "立绘", "background": "壁纸"}
        self.immich.albums.return_value = []
        self.immich.ensure_album.side_effect = lambda name: {"id": name, "albumName": name}
        response = await self.plugin.web_create_albums()
        self.assertEqual(json.loads(response.body)["created"], ["折枝-立绘", "折枝-壁纸"])
        self.assertEqual([call.args[0] for call in self.immich.share_album_with_all_editors.call_args_list], ["折枝-立绘", "折枝-壁纸"])

    async def test_upload_uses_custom_suffixes_and_square_goes_to_panel(self):
        self.storage["gallery_config"] = {"specifications": {"panel": "立绘", "background": "壁纸"}, "mappings": {}}
        await self.run_upload({"square": (10, 10), "wide": (20, 10)})
        self.assertEqual([call.args[0] for call in self.immich.add_to_album.call_args_list], ["new-panel", "new-background"])

    async def test_upload_honors_saved_manual_album_mapping(self):
        await self.run_upload({"square": (10, 10)})
        self.immich.add_to_album.assert_called_once_with("manual", ["uploaded-1"])

    async def run_upload(self, pictures):
        images = {}
        for name, size in pictures.items():
            buffer = io.BytesIO()
            Image.new("RGB", size).save(buffer, "PNG")
            images[f"https://example.invalid/{name}.png"] = buffer.getvalue()
        self.plugin._image_urls = AsyncMock(return_value=list(images))
        self.plugin._cfg = lambda key, default="": {"immich_url": "https://immich.invalid", "immich_api_key": "test-key"}.get(key, default)
        event = SimpleNamespace(plain_result=lambda text: text)

        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, content=images[str(request.url)])
            self.assertEqual(request.url.path, "/api/assets")
            return httpx.Response(200, json={"id": "uploaded-1", "status": "created"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            messages = [message async for message in self.plugin.upload_album(event, "折枝")]
        self.assertNotIn("失败", messages[-1])


if __name__ == "__main__":
    unittest.main()
