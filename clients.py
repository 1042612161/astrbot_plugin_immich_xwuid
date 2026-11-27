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
            payload = response.json()
            result = payload.get("assets") if isinstance(payload, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise RuntimeError("Immich 相册图片查询返回异常，请重试")
            assets.extend(result["items"])
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

    def album_checksums(self, album_id: str) -> set[str]:
        checksums = set()
        for asset in self.album_assets(album_id):
            try:
                value = asset["checksum"]
                raw = bytes.fromhex(value) if len(value) == 40 else base64.b64decode(value, validate=True)
                if len(raw) != 20:
                    raise ValueError("Not a SHA-1 checksum")
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("相册图片缺少有效 SHA-1 校验和，无法安全查重") from exc
            checksums.add(base64.b64encode(raw).decode())
        return checksums

    def check_upload(self, checksum: str) -> dict:
        response = self.client.post("/assets/bulk-upload-check", json={
            "assets": [{"id": checksum, "checksum": checksum}],
        })
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise RuntimeError("Immich 查重返回异常，请重试")
        result = results[0]
        if result.get("id") != checksum:
            raise RuntimeError("Immich 查重返回的图片编号不匹配，请重试")
        if result.get("action") == "accept":
            return result
        if (result.get("action") == "reject" and result.get("reason") == "duplicate"
                and isinstance(result.get("assetId"), str) and result["assetId"]
                and isinstance(result.get("isTrashed"), bool)):
            return result
        raise RuntimeError("Immich 查重未返回可用结果，请重试")

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

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> bool:
        """Attach assets, returning whether any were added; existing membership is harmless."""
        response = self.client.put(f"/albums/{quote(album_id)}/assets", json={"ids": asset_ids})
        response.raise_for_status()
        results = response.json()
        if (not isinstance(results, list) or len(results) != len(asset_ids)
                or any(not isinstance(item, dict) for item in results)
                or {item.get("id") for item in results} != set(asset_ids)):
            raise RuntimeError("Immich 加入相册返回异常，请重试")
        errors = [item for item in results if not item.get("success") and item.get("error") != "duplicate"]
        if errors:
            raise RuntimeError("Immich 未能将图片加入相册：" + "; ".join(item.get("errorMessage") or item.get("error", "未知原因") for item in errors))
        return any(item.get("success") for item in results)


class WavesClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 60,
                 transport: httpx.BaseTransport | None = None):
        if username != "admin":
            raise ValueError("XWUID 编辑台只接受用户名 admin")
        self.base = base_url.rstrip("/")
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.client = httpx.Client(headers={"Authorization": f"Basic {auth}", "X-Waves-Panel-Edit": "1"},
                                   timeout=timeout, transport=transport)

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
