import json
import unittest

import httpx

from astrbot_plugin_immich_xwuid.clients import ImmichClient, WavesClient


class ClientContractTests(unittest.TestCase):
    def test_waves_write_requests_pass_required_csrf_header_for_both_types(self):
        for target_type, code in (("panel", "card"), ("background", "bg")):
            with self.subTest(target_type=target_type):
                seen = []

                def respond(request):
                    seen.append(request)
                    if request.headers.get("X-Waves-Panel-Edit") != "1":
                        return httpx.Response(403, json={"detail": "Cross-site request rejected"})
                    if request.url.path.endswith("/tmp/upload"):
                        return httpx.Response(200, json={"token": "token-1"})
                    self.assertEqual(json.loads(request.read()), {"token": "token-1", "type": code, "char_id": "1306"})
                    return httpx.Response(200, json={"ok": True, "name": "1306_test.jpg"})

                client = WavesClient("https://waves.invalid", "admin", "test-only", transport=httpx.MockTransport(respond))
                self.addCleanup(client.client.close)
                result = client.upload_and_confirm(b"test-image", "IMG_8082.JPG", target_type, "1306")
                self.assertEqual(result["status"], "success")
                self.assertEqual([request.url.path.rsplit("/", 1)[-1] for request in seen], ["upload", "confirm"])

    def test_waves_upload_uses_tmp_then_confirm_and_never_forces_duplicate(self):
        seen = []

        def respond(request):
            seen.append(request)
            if request.url.path.endswith("/tmp/upload"):
                return httpx.Response(200, json={"token": "token-1"})
            if request.url.path.endswith("/confirm"):
                self.assertEqual(request.read(), b'{"token":"token-1","type":"card","char_id":"zhezhi"}')
                return httpx.Response(200, json={"ok": False, "duplicate": True, "matches": [{"name": "old.png"}]})
            raise AssertionError(request.url)

        client = WavesClient("https://waves.example", "admin", "secret", transport=httpx.MockTransport(respond))
        result = client.upload_and_confirm(b"fake image", "new.png", "panel", "zhezhi")
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual([item.url.path.rsplit("/", 1)[-1] for item in seen], ["upload", "confirm"])
        self.assertNotIn(b"force", seen[-1].read())
        self.assertEqual(seen[0].headers["authorization"], "Basic YWRtaW46c2VjcmV0")

    def test_immich_album_asset_search_uses_album_filter_and_paginates(self):
        seen = []

        def respond(request):
            seen.append(request)
            body = json.loads(request.read())
            if len(seen) == 1:
                self.assertEqual(body["page"], 1)
                return httpx.Response(200, json={"assets": {"items": [{"id": "asset-1"}], "nextCursor": "cursor-2"}})
            self.assertEqual(body["cursor"], "cursor-2")
            return httpx.Response(200, json={"assets": {"items": [{"id": "asset-2"}], "nextCursor": None}})

        client = ImmichClient("https://immich.example", "test-key")
        client.client = httpx.Client(base_url=client.base, headers={"x-api-key": "test-key"}, transport=httpx.MockTransport(respond))
        self.assertEqual([x["id"] for x in client.album_assets("album-1")], ["asset-1", "asset-2"])
        self.assertEqual([request.url.path for request in seen], ["/api/search/metadata", "/api/search/metadata"])
        self.assertTrue(all(b'"albumIds":["album-1"]' in request.read() for request in seen))

    def test_immich_create_share_and_attach_contract(self):
        seen = []

        def respond(request):
            seen.append(request)
            if request.method == "GET" and request.url.path == "/api/users":
                return httpx.Response(200, json=[{"id": "u1"}, {"id": "u2"}])
            if request.method == "GET" and request.url.path == "/api/albums/a1":
                return httpx.Response(200, json={"ownerId": "u1", "albumUsers": [{"user": {"id": "u1"}, "role": "editor"}]})
            if request.method == "GET" and request.url.path == "/api/albums":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == "/api/albums":
                return httpx.Response(200, json={"id": "a1", "albumName": "折枝-竖图"})
            if request.method == "PUT" and request.url.path == "/api/albums/a1/users":
                return httpx.Response(200, json={})
            if request.method == "PUT" and request.url.path == "/api/albums/a1/assets":
                return httpx.Response(200, json=[])
            raise AssertionError((request.method, request.url))

        client = ImmichClient("https://immich.example", "test-key")
        client.client = httpx.Client(base_url=client.base, headers={"x-api-key": "test-key"}, transport=httpx.MockTransport(respond))
        album = client.ensure_album("折枝-竖图")
        client.share_album_with_all_editors(album["id"])
        client.add_to_album(album["id"], ["asset-1"])
        shared = next(request for request in seen if request.url.path.endswith("/users") and request.method == "PUT")
        self.assertEqual(shared.read(), b'{"albumUsers":[{"userId":"u2","role":"editor"}]}')
        self.assertEqual(seen[-1].read(), b'{"ids":["asset-1"]}')


if __name__ == "__main__":
    unittest.main()
