import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from PIL import Image

from astrbot_plugin_immich_xwuid import main
from astrbot_plugin_immich_xwuid.core import match_mappings, resolve_role_folders, validate_mappings


class RoleIdDomainTests(unittest.TestCase):
    def setUp(self):
        self.roles = [{"id": "1501", "name": "漂泊者·衍射"}, {"id": "1502", "name": "漂泊者·衍射"}]
        self.catalog = {kind: self.roles for kind in ("panel", "background")}
        self.folders = [{"char_id": role["id"], "char_name": role["name"]} for role in self.roles]

    def test_same_name_ids_match_independently_and_can_share_one_album(self):
        albums = [{"name": "漂泊者·衍射-竖图"}]
        result = match_mappings(albums, self.catalog, {"panel": "竖图", "background": "横图"})
        expected = {"1501": "漂泊者·衍射-竖图", "1502": "漂泊者·衍射-竖图"}
        self.assertEqual(result["panel"], expected)
        self.assertEqual(validate_mappings(result, albums, self.catalog)["panel"], expected)

    def test_manual_mappings_remain_independent(self):
        result = match_mappings([{"name": "相册一"}, {"name": "相册二"}], self.catalog,
                                {"panel": "竖图", "background": "横图"},
                                {"panel": {"1501": "相册一", "1502": "相册二"}})
        self.assertEqual(result["panel"], {"1501": "相册一", "1502": "相册二"})

    def test_name_keys_are_rejected_in_new_mapping_format(self):
        with self.assertRaisesRegex(ValueError, "角色 ID"):
            validate_mappings({"panel": {"漂泊者·衍射": "相册"}}, [{"name": "相册"}], self.catalog)

    def test_ambiguous_name_requires_an_explicit_id(self):
        with self.assertRaisesRegex(ValueError, "1501、1502"):
            resolve_role_folders(self.folders, ["漂泊者·衍射"])
        selected, missing = resolve_role_folders(self.folders, ["1502", "1502"])
        self.assertEqual([folder["char_id"] for folder in selected], ["1502"])
        self.assertEqual(missing, [])

    def test_same_role_returned_by_both_types_is_not_ambiguous(self):
        selected, missing = resolve_role_folders([self.folders[0], self.folders[0]], ["漂泊者·衍射"])
        self.assertEqual(len(selected), 1)
        self.assertEqual(missing, [])


class RoleIdSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = main.ImmichXWUIDSync.__new__(main.ImmichXWUIDSync)
        self.plugin._sync_jobs = {"job": {"state": "running", "progress": "", "success": 0,
                                          "duplicate": 0, "failures": [], "total": 0, "processed": 0}}
        self.plugin._sync_job_cleanup = {}
        self.plugin._get_mappings = AsyncMock(return_value={"panel": {"1501": "相册一", "1502": "相册二"}})
        self.immich, self.waves = Mock(), Mock()
        self.immich.albums.return_value = [{"id": "one", "albumName": "相册一"}, {"id": "two", "albumName": "相册二"}]
        self.immich.album_assets.return_value = [{"id": "photo", "originalFileName": "test.png"}]
        buffer = io.BytesIO()
        Image.new("RGB", (10, 20)).save(buffer, "PNG")
        self.immich.download_asset.return_value = (buffer.getvalue(), "image/png")
        self.waves.folders.return_value = [{"char_id": "1501", "char_name": "漂泊者·衍射"}, {"char_id": "1502", "char_name": "漂泊者·衍射"}]
        self.waves.images.return_value = []
        self.waves.upload_and_confirm.return_value = {"status": "success"}
        self.plugin._clients = Mock(return_value=(self.immich, self.waves))

    def tearDown(self):
        for job_id in list(self.plugin._sync_jobs):
            self.plugin._discard_sync_job(job_id)

    async def test_web_sync_uses_the_selected_id_instead_of_the_last_same_name(self):
        await self.plugin._run_web_sync("job", ["1501"], "panel")
        self.immich.album_assets.assert_called_once_with("one")
        self.waves.images.assert_called_once_with("panel", "1501")
        self.assertEqual(self.waves.upload_and_confirm.call_args.args[-1], "1501")
        self.assertEqual(self.plugin._sync_jobs["job"]["success"], 1)

    async def test_web_sync_does_not_accept_display_names_instead_of_ids(self):
        await self.plugin._run_web_sync("job", ["漂泊者·衍射"], "panel")
        self.waves.upload_and_confirm.assert_not_called()
        self.assertEqual(len(self.plugin._sync_jobs["job"]["failures"]), 1)

    async def test_chat_sync_accepts_id_and_targets_that_directory(self):
        event = SimpleNamespace(plain_result=lambda text: text)
        messages = [text async for text in self.plugin._sync(event, "同步面板图相册", "1501")]
        self.immich.album_assets.assert_called_once_with("one")
        self.assertEqual(self.waves.upload_and_confirm.call_args.args[-1], "1501")
        self.assertIn("成功 1", messages[-1])

    async def test_ambiguous_chat_sync_does_not_download_or_upload(self):
        event = SimpleNamespace(plain_result=lambda text: text)
        messages = [text async for text in self.plugin._sync(event, "同步面板图相册", "漂泊者·衍射")]
        self.assertIn("请改用具体 ID", messages[-1])
        self.immich.download_asset.assert_not_called()
        self.waves.upload_and_confirm.assert_not_called()

    async def test_ambiguous_chat_upload_stops_before_http_upload(self):
        self.plugin._image_sources = AsyncMock(return_value=[("https://example.invalid/image.png",)])
        event = SimpleNamespace(plain_result=lambda text: text)
        messages = [text async for text in self.plugin.upload_album(event, "漂泊者·衍射")]
        self.assertIn("请改用具体 ID", messages[-1])
        self.immich.ensure_album.assert_not_called()
        self.immich.add_to_album.assert_not_called()


if __name__ == "__main__":
    unittest.main()
