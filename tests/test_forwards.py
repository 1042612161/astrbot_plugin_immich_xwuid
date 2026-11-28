import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import Forward, Reply, Node, Nodes, Image
from astrbot_plugin_immich_xwuid.main import ImmichXWUIDSync


def image(index):
    return {"type": "image", "data": {"url": f"https://example.invalid/{index}.png"}}


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    def event(self, nodes):
        return SimpleNamespace(
            message_obj=SimpleNamespace(message=[Reply(id="1", chain=[Forward(id="root")])]),
            bot=SimpleNamespace(call_action=AsyncMock(return_value={"messages": nodes})),
        )

    async def test_quoted_forward_reads_node_images(self):
        event = self.event([{"content": [image(1), image(2)]}])
        sources, limits = await ImmichXWUIDSync._image_sources(event, 1, 50)
        self.assertEqual(len(sources), 2)
        self.assertEqual(limits, {"image_limit": False, "depth_limit": False})

    async def test_image_limit_reports_actual_truncation(self):
        sources, limits = await ImmichXWUIDSync._image_sources(
            self.event([{"content": [image(1), image(2), image(3)]}]), 1, 2)
        self.assertEqual(len(sources), 2)
        self.assertTrue(limits["image_limit"])

    async def test_exact_image_limit_is_not_reported_as_truncated(self):
        _, limits = await ImmichXWUIDSync._image_sources(
            self.event([{"content": [image(1), image(2)]}]), 1, 2)
        self.assertFalse(limits["image_limit"])

    async def test_nested_depth_is_configurable_and_reports_omitted_forward(self):
        payloads = {
            "root": {"messages": [{"content": [image(1), {"type": "forward", "data": {"id": "child"}}]}]},
            "child": {"messages": [{"content": [image(2), {"type": "forward", "data": {"id": "grandchild"}}]}]},
            "grandchild": {"messages": [{"content": [image(3)]}]},
        }
        for depth, count, truncated in ((0, 1, True), (1, 2, True), (2, 3, False)):
            event = self.event([])
            async def get(action, **params):
                return payloads[params["message_id"]]
            event.bot.call_action.side_effect = get
            sources, limits = await ImmichXWUIDSync._image_sources(event, depth, 50)
            self.assertEqual(len(sources), count)
            self.assertEqual(limits["depth_limit"], truncated)
            self.assertEqual(event.bot.call_action.await_count, count)

    async def test_get_msg_then_get_forward_msg_reads_two_images(self):
        event = self.event([])
        event.message_obj.message = [Reply(id="123")]
        event.bot.call_action.side_effect = [
            {"data": {"message": [{"type": "forward", "data": {"id": "root"}}]}},
            {"data": {"messages": [{"message": [image(1), image(2)]}]}},
        ]
        sources, _ = await ImmichXWUIDSync._image_sources(event)
        self.assertEqual(len(sources), 2)
        self.assertEqual([call.kwargs["action"] for call in event.bot.call_action.call_args_list],
                         ["get_msg", "get_forward_msg"])

    async def test_inline_astrbot_nodes_use_same_nested_depth(self):
        event = self.event([])
        event.message_obj.message[0].chain = [Nodes(nodes=[Node(content=[
            Image(file="https://example.invalid/1.png"), Forward(id="child")])])]
        sources, limits = await ImmichXWUIDSync._image_sources(event, 0, 50)
        self.assertEqual(len(sources), 1)
        self.assertTrue(limits["depth_limit"])
        event.bot.call_action.assert_not_awaited()

    async def test_forward_id_parameter_compatibility(self):
        event = self.event([])
        async def get(action, **params):
            if "message_id" in params:
                raise RuntimeError("requires forward_id")
            return {"nodes": [{"content": [image(1)]}]}
        event.bot.call_action.side_effect = get
        sources, _ = await ImmichXWUIDSync._image_sources(event)
        self.assertEqual(len(sources), 1)
        self.assertIn("forward_id", event.bot.call_action.call_args.kwargs)

    async def test_opaque_forward_file_is_resolved_to_http_source(self):
        event = self.event([])
        event.bot.call_action.side_effect = [
            {"nodeList": [{"content": [{"type": "image", "data": {"file": "opaque-token"}}]}]},
            {"url": "https://example.invalid/resolved.png"},
        ]
        sources, _ = await ImmichXWUIDSync._image_sources(event)
        self.assertEqual(sources, [("https://example.invalid/resolved.png",)])
        self.assertEqual(event.bot.call_action.call_args.kwargs["action"], "get_image")

    async def test_cq_and_json_node_content_and_source_deduplication(self):
        event = self.event([
            {"content": '[CQ:image,file=https://example.invalid/1.png]'},
            {"content": '[{"type":"image","data":{"url":"https://example.invalid/1.png"}}]'},
        ])
        sources, limits = await ImmichXWUIDSync._image_sources(event, 1, 1)
        self.assertEqual(len(sources), 1)
        self.assertFalse(limits["image_limit"])

    async def test_image_cap_stops_before_fetching_later_nested_forward(self):
        event = self.event([{"content": [image(1), image(2), {"type": "forward", "data": {"id": "child"}}]}])
        sources, limits = await ImmichXWUIDSync._image_sources(event, 1, 1)
        self.assertEqual(len(sources), 1)
        self.assertTrue(limits["image_limit"])
        self.assertEqual(event.bot.call_action.await_count, 1)

    async def test_self_referential_forward_does_not_loop(self):
        event = self.event([{"content": [image(1), {"type": "forward", "data": {"id": "root"}}]}])
        sources, _ = await ImmichXWUIDSync._image_sources(event)
        self.assertEqual(len(sources), 1)
        self.assertEqual(event.bot.call_action.await_count, 1)

    async def test_revisited_forward_at_shallower_depth_can_expand_more_children(self):
        forward = lambda key: {"type": "forward", "data": {"id": key}}
        payloads = {
            "root": {"messages": [{"content": [forward("middle"), forward("leaf")]}]},
            "middle": {"messages": [{"content": [forward("leaf")]}]},
            "leaf": {"messages": [{"content": [image(1), forward("last")]}]},
            "last": {"messages": [{"content": [image(2)]}]},
        }
        event = self.event([])
        async def get(action, **params):
            return payloads[params["message_id"]]
        event.bot.call_action.side_effect = get
        sources, _ = await ImmichXWUIDSync._image_sources(event, 2, 50)
        self.assertEqual(len(sources), 2)
