from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import quote

import httpx


class ImmichClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60):
        self.base = base_url.rstrip("/") + "/api"
        self.client = httpx.Client(base_url=self.base, headers={"x-api-key": api_key}, timeout=timeout)

    def albums(self) -> list[dict]:
        response = self.client.get("/albums")
        response.raise_for_status()
        return response.json()

    def album_assets(self, album_id: str) -> list[dict]:
        page, cursor, assets = 1, None, []
        while True:
            body = {"albumIds": [album_id], "size": 1000}
            if cursor:
                body["cursor"] = cursor
            else:
                body["page"] = page
            response = self.client.post("/search/metadata", json=body)
            response.raise_for_status()
            result = response.json().get("assets", {})
            assets.extend(result.get("items", []))
            next_cursor = result.get("nextCursor")
            next_page = result.get("nextPage")
            if not result.get("items") or (next_cursor in (None, "") and next_page in (None, "", False)):
                return assets
            if next_cursor:
                cursor = next_cursor
            else:
                page = int(next_page)

    def download_asset(self, asset_id: str) -> tuple[bytes, str]:
        response = self.client.get(f"/assets/{quote(asset_id)}/original")
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/jpeg")

    def ensure_album(self, name: str) -> dict:
        for album in self.albums():
            if album.get("albumName") == name:
                return album
        response = self.client.post("/albums", json={"albumName": name})
        response.raise_for_status()
        return response.json()

    def share_album_with_all_editors(self, album_id: str) -> None:
        response = self.client.get("/users", params={"isAll": "true"})
        response.raise_for_status()
        users = response.json()
        album_response = self.client.get(f"/albums/{quote(album_id)}")
        album_response.raise_for_status()
        album = album_response.json()
        already_shared = {entry.get("user", {}).get("id") for entry in album.get("albumUsers", [])}
        owner_id = album.get("ownerId")
        payload = {"albumUsers": [{"userId": user["id"], "role": "editor"} for user in users
                   if user.get("id") and user["id"] != owner_id and user["id"] not in already_shared]}
        if payload["albumUsers"]:
            result = self.client.put(f"/albums/{quote(album_id)}/users", json=payload)
            result.raise_for_status()

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        response = self.client.put(f"/albums/{quote(album_id)}/assets", json={"ids": asset_ids})
        response.raise_for_status()
        results = response.json()
        errors = [item for item in results if not item.get("success")]
        if errors:
            raise RuntimeError("Immich 未能将图片加入相册：" + "; ".join(item.get("errorMessage") or item.get("error", "未知原因") for item in errors))


class WavesClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 60,
                 transport: httpx.BaseTransport | None = None):
        if username != "admin":
            raise ValueError("XWUID 编辑台只接受用户名 admin")
        self.base = base_url.rstrip("/")
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.client = httpx.Client(headers={"Authorization": f"Basic {auth}"}, timeout=timeout, transport=transport)

    def folders(self, target_type: str) -> list[dict]:
        code = "card" if target_type == "panel" else "bg"
        response = self.client.get(f"{self.base}/waves/panel-edit/api/folders", params={"type": code})
        response.raise_for_status()
        return response.json().get("folders", [])

    def images(self, target_type: str, char_id: str) -> list[dict]:
        code = "card" if target_type == "panel" else "bg"
        response = self.client.get(f"{self.base}/waves/panel-edit/api/images", params={"type": code, "char_id": char_id})
        response.raise_for_status()
        return response.json().get("images", [])

    def upload_and_confirm(self, data: bytes, filename: str, target_type: str, char_id: str) -> dict:
        code = "card" if target_type == "panel" else "bg"
        upload = self.client.post(f"{self.base}/waves/panel-edit/api/tmp/upload",
                                  files={"file": (filename, data, "application/octet-stream")})
        upload.raise_for_status()
        token = upload.json()["token"]
        confirm = self.client.post(f"{self.base}/waves/panel-edit/api/confirm",
                                   json={"token": token, "type": code, "char_id": char_id})
        confirm.raise_for_status()
        result = confirm.json()
        if result.get("duplicate"):
            return {"status": "duplicate", "matches": result.get("matches", [])}
        if not result.get("ok"):
            return {"status": "failed", "reason": json.dumps(result, ensure_ascii=False)}
        return {"status": "success", "name": result.get("name")}
