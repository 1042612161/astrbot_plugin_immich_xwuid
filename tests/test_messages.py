import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.star.filter.command import CommandFilter

from astrbot_plugin_immich_xwuid.core import parse_sync_command
from astrbot_plugin_immich_xwuid.main import ImmichXWUIDSync


class ImageExtractionTests(unittest.IsolatedAsyncioTestCase):
    def event(self, segments, result=None):
        return SimpleNamespace(
            message_obj=SimpleNamespace(message=segments, self_id="12345"),
            bot=SimpleNamespace(call_action=AsyncMock(return_value=result)),
        )

    async def test_astrbot_image_components_read_url_or_file_and_deduplicate(self):
        event = self.event([
            Image(file="onebot-token", url="https://example.invalid/zhezhi-1.jpg"),
            Image(file="https://example.invalid/zhezhi-2.png"),
            Image(file="https://example.invalid/zhezhi-1.jpg"),
        ])
        self.assertEqual(await ImmichXWUIDSync._image_sources(event), [
            ("https://example.invalid/zhezhi-1.jpg",), ("https://example.invalid/zhezhi-2.png",),
        ])
        event.bot.call_action.assert_not_awaited()

    async def test_current_images_take_priority_over_quoted_images(self):
        event = self.event([
            Reply(id="123", chain=[Image(file="https://example.invalid/quoted.jpg")]),
            Reply(id="456"),
            Image(file="https://example.invalid/current.jpg"),
        ])
        self.assertEqual(await ImmichXWUIDSync._image_sources(event), [("https://example.invalid/current.jpg",)])
        event.bot.call_action.assert_not_awaited()

    async def test_quoted_component_chain_does_not_fetch_message_again(self):
        event = self.event([Plain("上传相册 折枝"), Reply(id="123", chain=[
            Plain("图片"), Image(file="https://example.invalid/quoted.jpg"),
        ])])
        self.assertEqual(await ImmichXWUIDSync._image_sources(event), [("https://example.invalid/quoted.jpg",)])
        event.bot.call_action.assert_not_awaited()

    async def test_missing_reply_chain_uses_onebot_get_msg_with_bot_routing(self):
        event = self.event([Reply(id="123")], {"message": [
            {"type": "image", "data": {"url": "https://example.invalid/quoted.jpg"}},
        ]})
        self.assertEqual(await ImmichXWUIDSync._image_sources(event), [("https://example.invalid/quoted.jpg",)])
        event.bot.call_action.assert_awaited_once_with(action="get_msg", message_id=123, self_id="12345")

    async def test_raw_onebot_reply_and_wrapped_response_are_supported(self):
        event = self.event([{"type": "reply", "data": {"id": "123"}}], {"data": {"message": [
            {"type": "image", "data": {"file": "https://example.invalid/quoted.png"}},
        ]}})
        self.assertEqual(await ImmichXWUIDSync._image_sources(event), [("https://example.invalid/quoted.png",)])

    async def test_unsupported_sources_are_not_sent_to_onebot_get_image(self):
        for source in ("onebot-token", "file:///tmp/image.png", "base64://AAAA", "relative/image.png"):
            with self.subTest(source=source):
                event = self.event([Image(file=source)])
                with self.assertRaisesRegex(RuntimeError, "没有可读取的本地路径或 HTTP/HTTPS 链接"):
                    await ImmichXWUIDSync._image_sources(event)
                event.bot.call_action.assert_not_awaited()

    async def test_failed_current_image_does_not_fall_back_to_quote(self):
        event = self.event([Image(file=""), Reply(id="123", chain=[Image(file="https://example.invalid/quoted.jpg")])])
        with self.assertRaisesRegex(RuntimeError, "没有可读取的本地路径或 HTTP/HTTPS 链接"):
            await ImmichXWUIDSync._image_sources(event)
        event.bot.call_action.assert_not_awaited()

    async def test_expired_reply_reports_failure(self):
        event = self.event([Reply(id="123")])
        event.bot.call_action.side_effect = RuntimeError("expired")
        with self.assertRaisesRegex(RuntimeError, "读取引用消息失败"):
            await ImmichXWUIDSync._image_sources(event)

    async def test_message_and_quote_without_images_return_empty_list(self):
        event = self.event([Reply(id="123", chain=[Plain("只有文本")])])
        self.assertEqual(await ImmichXWUIDSync._image_sources(event), [])
        event.bot.call_action.assert_not_awaited()

    async def test_plain_text_image_url_is_not_treated_as_an_image_component(self):
        event = self.event([Reply(id="123", chain=[Plain("https://example.invalid/image.jpg")])])
        self.assertEqual(await ImmichXWUIDSync._image_sources(event), [])
        event.bot.call_action.assert_not_awaited()


class CommandArgumentTests(unittest.TestCase):
    def test_framework_parser_keeps_all_roles_with_spaces_around_commas(self):
        for handler, command in (
            (ImmichXWUIDSync.sync_background, "同步背景图相册"),
            (ImmichXWUIDSync.sync_panel, "同步面板图相册"),
        ):
            parser = CommandFilter(command, handler_md=SimpleNamespace(handler=handler))
            for argument in ("折枝， 测试角色", "折枝 , 测试角色", " 折枝，  测试角色,折枝， "):
                with self.subTest(command=command, argument=argument):
                    # Exercise AstrBot's actual argument converter, before domain parsing.
                    params = parser.validate_and_convert_params(argument.split(), parser.handler_params)
                    roles, _ = parse_sync_command(command, params["roles"])
                    self.assertEqual(roles, ["折枝", "测试角色"])

    def test_framework_allows_empty_greedy_argument_for_usage_reply(self):
        parser = CommandFilter("同步面板图相册", handler_md=SimpleNamespace(handler=ImmichXWUIDSync.sync_panel))
        params = parser.validate_and_convert_params([], parser.handler_params)
        with self.assertRaisesRegex(ValueError, "至少指定一个角色"):
            parse_sync_command("同步面板图相册", params["roles"])


if __name__ == "__main__":
    unittest.main()
