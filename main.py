from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from PIL import Image

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as ImageComponent, Reply
from astrbot.api.star import Context, Star, register
from astrbot.api.web import request, json_response
from astrbot.core.star.filter.command import GreedyStr

from .clients import ImmichClient, WavesClient
from .core import (
    DEFAULT_SPECIFICATIONS, classify_image, folders_by_type, match_mappings,
    parse_sync_command, resolve_role_folders, role_album_name, role_label,
    validate_mappings, validate_specifications,
)


# Allow the current page to fetch the result once; this is not task history.
_UNREAD_RESULT_TIMEOUT = 30


@register("astrbot_plugin_immich_xwuid", "Codex", "Immich 与鸣潮 XWUID 面板图/背景图同步", "1.1.1")
class ImmichXWUIDSync(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._sync_jobs = {}
        self._sync_job_cleanup = {}
        self._register_web()

    def _cfg(self, key: str, default=""):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _clients(self):
        immich = ImmichClient(str(self._cfg("immich_url")).strip(), str(self._cfg("immich_api_key")).strip())
        waves = WavesClient(str(self._cfg("xwuid_url")).strip(), str(self._cfg("xwuid_username", "admin")).strip(), str(self._cfg("xwuid_password")).strip())
        return immich, waves

    def _register_web(self):
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            return
        routes = [
            ("/astrbot_plugin_immich_xwuid/data", self.web_data, ["GET"], "Load album/role mapping data"),
            ("/astrbot_plugin_immich_xwuid/save", self.web_save, ["POST"], "Save album/role mappings"),
            ("/astrbot_plugin_immich_xwuid/refresh", self.web_data, ["GET"], "Refresh mappings from both services"),
            ("/astrbot_plugin_immich_xwuid/create-albums", self.web_create_albums, ["POST"], "Create missing role albums"),
            ("/astrbot_plugin_immich_xwuid/sync", self.web_sync, ["POST"], "Start mapped album synchronization"),
            ("/astrbot_plugin_immich_xwuid/sync-status", self.web_sync_status, ["GET"], "Read synchronization progress"),
        ]
        for route, handler, methods, desc in routes:
            register_api(route, handler, methods, desc)

    async def _catalog_data(self):
        immich, waves = self._clients()
        albums, panel, background = await asyncio.gather(
            asyncio.to_thread(immich.albums),
            asyncio.to_thread(waves.folders, "panel"),
            asyncio.to_thread(waves.folders, "background"),
        )
        album_items = [{"id": x.get("id"), "name": x.get("albumName", "")} for x in albums]
        role_map = folders_by_type(panel, background)
        return {"albums": album_items, "roles": role_map}

    async def _get_gallery_config(self):
        saved = await self.get_kv_data("gallery_config", None)
        if saved is None:
            saved = {"specifications": dict(DEFAULT_SPECIFICATIONS),
                     "mappings": {"panel": {}, "background": {}}}
        return saved

    async def _api_data(self):
        data = await self._catalog_data()
        saved = await self._get_gallery_config()
        specifications = validate_specifications(saved["specifications"])
        mappings = match_mappings(data["albums"], data["roles"], specifications, saved["mappings"])
        await self.put_kv_data("gallery_config", {"specifications": specifications, "mappings": mappings})
        return {**data, "specifications": specifications, "mappings": mappings}

    async def web_data(self):
        try:
            return json_response(await self._api_data())
        except Exception as exc:
            return json_response({"error": str(exc)}, status_code=500)

    async def web_save(self):
        payload = await request.json(default={})
        try:
            if not isinstance(payload, dict):
                raise ValueError("请提交有效的映射表单")
            saved = await self._get_gallery_config()
            specifications = validate_specifications(payload.get("specifications", saved["specifications"]))
            reset = specifications != saved["specifications"]
            data = await self._catalog_data()
            if reset:
                # A specification change intentionally discards both manual tables.
                mappings = match_mappings(data["albums"], data["roles"], specifications)
            else:
                mappings = validate_mappings(payload.get("mappings", {}), data["albums"], data["roles"])
            # Store names and mappings together so they cannot be saved out of step.
            await self.put_kv_data("gallery_config", {"specifications": specifications, "mappings": mappings})
            return json_response({"ok": True, "reset": reset, **data,
                                  "specifications": specifications, "mappings": mappings})
        except ValueError as exc:
            return json_response({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return json_response({"error": str(exc)}, status_code=500)

    async def web_create_albums(self):
        immich, waves = self._clients()
        saved = await self._get_gallery_config()
        specifications = validate_specifications(saved["specifications"])
        existing = await asyncio.to_thread(immich.albums)
        existing_names = {x.get("albumName") for x in existing}
        made = []
        for target_type in DEFAULT_SPECIFICATIONS:
            for folder in await asyncio.to_thread(waves.folders, target_type):
                name = role_album_name(folder.get("char_name") or folder["char_id"], target_type, specifications)
                if name not in existing_names:
                    album = await asyncio.to_thread(immich.ensure_album, name)
                    await asyncio.to_thread(immich.share_album_with_all_editors, album["id"])
                    made.append(name)
                    existing_names.add(name)
        return json_response({"created": made, "count": len(made)})

    async def web_sync(self):
        payload = await request.json(default={})
        target = payload.get("type")
        roles = payload.get("roles", [])
        if target not in ("panel", "background") or not isinstance(roles, list) or not roles:
            return json_response({"error": "请选择面板图或背景图，并至少选择一个角色。"}, status_code=400)
        job_id = uuid4().hex
        self._sync_jobs[job_id] = {"state": "running", "progress": "正在准备同步…", "success": 0, "duplicate": 0, "failures": [], "total": 0, "processed": 0}
        asyncio.create_task(self._run_web_sync(job_id, list(dict.fromkeys(str(x).strip() for x in roles if str(x).strip())), target))
        return json_response({"job_id": job_id})

    async def web_sync_status(self):
        job_id = request.query.get("job_id", "")
        job = self._sync_jobs.get(job_id)
        if not job:
            return json_response({"error": "同步任务不存在或已过期。"}, status_code=404)
        response = json_response(job, headers={"Cache-Control": "no-store"})
        if job["state"] != "running":
            self._discard_sync_job(job_id)
        return response

    def _discard_sync_job(self, job_id: str):
        self._sync_jobs.pop(job_id, None)
        cleanup = self._sync_job_cleanup.pop(job_id, None)
        if cleanup is not None:
            cleanup.cancel()

    async def _run_web_sync(self, job_id: str, roles: list[str], target: str):
        job = self._sync_jobs[job_id]
        try:
            immich, waves = self._clients()
            mappings = await self._get_mappings()
            albums = await asyncio.to_thread(immich.albums)
            album_by_name = {x.get("albumName"): x for x in albums}
            folders = await asyncio.to_thread(waves.folders, target)
            folders_by_id = {str(x["char_id"]): x for x in folders}
            plans = []
            for role_id in roles:
                folder = folders_by_id.get(role_id)
                role_name = role_label(folder) if folder else role_id
                album_name = mappings.get(target, {}).get(role_id)
                if not folder or not album_name or album_name not in album_by_name:
                    job["failures"].append(f"{role_name}：未配置有效的 Immich 相册映射")
                    continue
                assets = await asyncio.to_thread(immich.album_assets, album_by_name[album_name]["id"])
                known = await asyncio.to_thread(waves.images, target, folder["char_id"])
                known_names = {x.get("name") for x in known}
                plans.append((role_name, folder, assets, known_names))
            job["total"] = sum(len(plan[2]) for plan in plans)
            job["progress"] = f"共 {job['total']} 张待检查图片"
            for role, folder, assets, known_names in plans:
                job["progress"] = f"正在同步 {role}（{len(assets)} 张）"
                for asset in assets:
                    asset_id = asset.get("id")
                    filename = asset.get("originalFileName") or f"{asset_id}.jpg"
                    if filename in known_names:
                        job["duplicate"] += 1
                    else:
                        try:
                            data, _ = await asyncio.to_thread(immich.download_asset, asset_id)
                            with Image.open(io.BytesIO(data)) as image:
                                width, height = image.size
                            if (target == "panel" and width > height) or (target == "background" and height > width):
                                job["failures"].append(f"{role}/{filename}：图片比例不符合目标规格")
                            else:
                                result = await asyncio.to_thread(waves.upload_and_confirm, data, Path(filename).name, target, folder["char_id"])
                                if result["status"] == "success":
                                    job["success"] += 1
                                elif result["status"] == "duplicate":
                                    job["duplicate"] += 1
                                else:
                                    job["failures"].append(f"{role}/{filename}：{result.get('reason', '编辑台拒绝上传')}")
                        except Exception as exc:
                            job["failures"].append(f"{role}/{filename}：{exc}")
                    job["processed"] += 1
                    if job["processed"] % 5 == 0 or job["processed"] == job["total"]:
                        job["progress"] = f"已检查 {job['processed']}/{job['total']} 张 · 成功 {job['success']} · 重复 {job['duplicate']} · 失败 {len(job['failures'])}"
            job["state"] = "completed"
            job["progress"] = f"同步完成：成功 {job['success']}，重复/已存在 {job['duplicate']}，失败 {len(job['failures'])}"
        except Exception as exc:
            job["state"] = "failed"
            job["failures"].append(str(exc))
            job["progress"] = "同步失败"
        finally:
            if job["state"] == "running":
                # A cancelled task cannot deliver a final result to the page.
                self._discard_sync_job(job_id)
            else:
                self._sync_job_cleanup[job_id] = asyncio.get_running_loop().call_later(
                    _UNREAD_RESULT_TIMEOUT, self._discard_sync_job, job_id
                )

    async def _get_mappings(self):
        try:
            return (await self._get_gallery_config())["mappings"]
        except Exception:
            return {}

    @filter.command("上传相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type("aiocqhttp")
    async def upload_album(self, event: AstrMessageEvent, character: str = ""):
        name = str(character).strip()
        if not name:
            yield event.plain_result("用法：上传相册 折枝（同时发送或引用图片）")
            return
        try:
            images = await self._image_sources(event)
        except RuntimeError as exc:
            yield event.plain_result(str(exc))
            return
        if not images:
            yield event.plain_result("没有找到图片。请将图片与命令一起发送，或引用图片后发送命令。")
            return
        immich, waves = self._clients()
        try:
            catalogs = await asyncio.gather(*(asyncio.to_thread(waves.folders, kind) for kind in DEFAULT_SPECIFICATIONS))
            resolved, missing = resolve_role_folders([folder for catalog in catalogs for folder in catalog], [name])
            if missing:
                raise ValueError(f"XWUID 中不存在角色或 ID：{name}")
            folder = resolved[0]
            role_id = str(folder["char_id"])
            name = str(folder.get("char_name") or role_id)
            saved = await self._get_gallery_config()
            specifications = validate_specifications(saved["specifications"])
            album_names = {kind: saved["mappings"].get(kind, {}).get(role_id) or role_album_name(name, kind, specifications)
                           for kind in DEFAULT_SPECIFICATIONS}
        except Exception as exc:
            yield event.plain_result(f"无法选择上传相册：{exc}")
            return
        existing = {x.get("albumName"): x for x in await asyncio.to_thread(immich.albums)}
        report = []
        async with httpx.AsyncClient(timeout=90) as client:
            for index, sources in enumerate(images, 1):
                try:
                    data, source = await self._read_image(sources, client)
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                        width, height = image.size
                        image_format = (image.format or "JPEG").lower()
                    extension, media_type = {
                        "jpeg": ("jpg", "image/jpeg"), "jpg": ("jpg", "image/jpeg"),
                        "png": ("png", "image/png"), "webp": ("webp", "image/webp"),
                        "gif": ("gif", "image/gif"), "bmp": ("bmp", "image/bmp"),
                    }.get(image_format, ("jpg", "image/jpeg"))
                    kind = classify_image(width, height)
                    album = existing.get(album_names[kind])
                    if not album:
                        album = await asyncio.to_thread(immich.ensure_album, album_names[kind])
                        await asyncio.to_thread(immich.share_album_with_all_editors, album["id"])
                        existing[album_names[kind]] = album
                    # Upload through Immich asset endpoint, then attach the returned asset to the album.
                    digest = base64.b64encode(hashlib.sha1(data).digest()).decode()
                    timestamp = datetime.now(timezone.utc).isoformat()
                    source_name = Path(urlparse(source).path if source.startswith(("http://", "https://")) else source).name
                    if Path(source_name).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
                        source_name = f"{name}-{index}.{extension}"
                    upload = await client.post(str(self._cfg("immich_url")).rstrip("/") + "/api/assets", headers={"x-api-key": str(self._cfg("immich_api_key"))},
                                               data={"deviceAssetId": f"astrbot-{name}-{digest}", "deviceId": "astrbot-immich-xwuid", "fileCreatedAt": timestamp, "fileModifiedAt": timestamp, "checksum": digest},
                                               files={"assetData": (source_name, data, media_type)})
                    upload.raise_for_status()
                    uploaded = upload.json()
                    asset_id = uploaded.get("id")
                    if asset_id:
                        await asyncio.to_thread(immich.add_to_album, album["id"], [asset_id])
                    if uploaded.get("status") == "duplicate" and asset_id:
                        report.append(f"{index}. Immich 已有相同图片，已加入{album_names[kind]}")
                    elif asset_id:
                        report.append(f"{index}. 已上传到{album_names[kind]}")
                    else:
                        report.append(f"{index}. 失败：Immich 未返回资产编号")
                except Exception as exc:
                    report.append(f"{index}. 失败：{exc}")
        yield event.plain_result("相册上传完成\n" + "\n".join(report))

    @filter.command("同步背景图相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type("aiocqhttp")
    async def sync_background(self, event: AstrMessageEvent, roles: GreedyStr):
        async for result in self._sync(event, "同步背景图相册", roles):
            yield result

    @filter.command("同步面板图相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type("aiocqhttp")
    async def sync_panel(self, event: AstrMessageEvent, roles: GreedyStr):
        async for result in self._sync(event, "同步面板图相册", roles):
            yield result

    async def _sync(self, event, command: str, roles_arg: str):
        try:
            roles, target = parse_sync_command(command, roles_arg)
            immich, waves = self._clients()
            mappings = await self._get_mappings()
            albums = await asyncio.to_thread(immich.albums)
            album_by_name = {x.get("albumName"): x for x in albums}
            folders = await asyncio.to_thread(waves.folders, target)
            selected_folders, missing_roles = resolve_role_folders(folders, roles)
            done, duplicate, failed = 0, 0, [f"{role}：XWUID 中不存在对应角色或 ID" for role in missing_roles]
            for folder in selected_folders:
                role_id = str(folder["char_id"])
                role = role_label(folder)
                album_name = mappings.get(target, {}).get(role_id)
                if not folder or not album_name or album_name not in album_by_name:
                    failed.append(f"{role}：未配置有效的 Immich 相册映射")
                    continue
                assets = await asyncio.to_thread(immich.album_assets, album_by_name[album_name]["id"])
                known = await asyncio.to_thread(waves.images, target, folder["char_id"])
                known_names = {x.get("name") for x in known}
                yield event.plain_result(f"开始同步 {role}：共 {len(assets)} 张，目标为{'面板图' if target == 'panel' else '背景图'}。")
                for index, asset in enumerate(assets, 1):
                    asset_id = asset.get("id")
                    filename = asset.get("originalFileName") or f"{asset_id}.jpg"
                    if filename in known_names:
                        duplicate += 1
                    else:
                        try:
                            data, _ = await asyncio.to_thread(immich.download_asset, asset_id)
                            with Image.open(io.BytesIO(data)) as image:
                                width, height = image.size
                            if (target == "panel" and width > height) or (target == "background" and height > width):
                                failed.append(f"{role}/{filename}：图片比例不符合目标规格")
                            else:
                                result = await asyncio.to_thread(waves.upload_and_confirm, data, Path(filename).name, target, folder["char_id"])
                                if result["status"] == "success":
                                    done += 1
                                elif result["status"] == "duplicate":
                                    duplicate += 1
                                else:
                                    failed.append(f"{role}/{filename}：{result.get('reason', '编辑台拒绝上传')}")
                        except Exception as exc:
                            failed.append(f"{role}/{filename}：{exc}")
                    if index % 5 == 0 or index == len(assets):
                        yield event.plain_result(f"同步进度：{role} {index}/{len(assets)}；成功 {done}，重复 {duplicate}，失败 {len(failed)}")
            summary = [f"同步完成：成功 {done}，重复/已存在 {duplicate}，失败 {len(failed)}"]
            summary.extend(failed)
            yield event.plain_result("\n".join(summary))
        except Exception as exc:
            yield event.plain_result(f"同步失败：{exc}")

    @staticmethod
    async def _image_sources(event):
        """Collect each image's local paths followed by its HTTP(S) alternatives."""
        message = getattr(getattr(event, "message_obj", None), "message", [])

        def image_fields(chain):
            images = []
            for segment in chain or []:
                if isinstance(segment, ImageComponent):
                    images.append({"path": segment.path, "url": segment.url, "file": segment.file})
                elif isinstance(segment, dict) and segment.get("type") == "image":
                    images.append(segment.get("data", {}))
            return images

        async def onebot_action(action, **params):
            call = getattr(getattr(event, "bot", None), "call_action", None)
            if not callable(call):
                raise RuntimeError("无法读取图片：当前 OneBot 连接不支持读取消息")
            self_id = getattr(getattr(event, "message_obj", None), "self_id", None)
            if self_id:
                params["self_id"] = self_id
            try:
                result = await call(action=action, **params)
            except Exception as exc:
                raise RuntimeError("读取引用消息失败，请检查消息是否已失效或重新发送图片") from exc
            if not isinstance(result, dict):
                raise RuntimeError("读取图片失败：OneBot 返回的数据格式无效")
            result = result.get("data", result)
            if not isinstance(result, dict):
                raise RuntimeError("读取图片失败：OneBot 返回的数据格式无效")
            return result

        # Direct images take priority, even when the message also contains a reply.
        images = image_fields(message)
        if not images:
            for segment in message or []:
                if isinstance(segment, Reply):
                    chain, message_id = segment.chain, segment.id
                elif isinstance(segment, dict) and segment.get("type") == "reply":
                    chain, message_id = segment.get("chain"), segment.get("data", {}).get("id")
                else:
                    continue
                if not chain and message_id is not None:
                    message_id = int(message_id) if str(message_id).isdigit() else message_id
                    quoted = await onebot_action("get_msg", message_id=message_id)
                    chain = quoted.get("message", [])
                images.extend(image_fields(chain))

        sources = []
        for index, fields in enumerate(images, 1):
            local, remote = [], []
            for key in ("path", "url", "file"):
                value = fields.get(key)
                if not isinstance(value, str) or not value:
                    continue
                if value.startswith(("http://", "https://")):
                    remote.append(value)
                elif Path(value).is_absolute():
                    local.append(value)
            candidates = tuple(dict.fromkeys(local + remote))
            if not candidates:
                raise RuntimeError(f"第 {index} 张图片没有可读取的本地路径或 HTTP/HTTPS 链接，请重新发送图片")
            sources.append(candidates)
        return list(dict.fromkeys(sources))

    @staticmethod
    async def _read_image(sources, client):
        """Read a source without creating or deleting AstrBot's local media files."""
        reason = "没有可读取的图片来源，请重新发送图片"
        for source in sources:
            if source.startswith(("http://", "https://")):
                try:
                    response = await client.get(source)
                    response.raise_for_status()
                    return response.content, source
                except httpx.HTTPStatusError as exc:
                    reason = f"图片下载失败（HTTP {exc.response.status_code}），请重新发送图片"
                except httpx.RequestError:
                    reason = "图片下载失败，请检查网络或重新发送图片"
            else:
                path = Path(source)
                try:
                    if not path.is_absolute():
                        reason = "图片来源不是普通本地路径或 HTTP/HTTPS 链接"
                    elif not path.exists():
                        reason = "本地图片不存在，请重新发送图片"
                    elif not path.is_file():
                        reason = "本地图片路径不是普通文件，请重新发送图片"
                    else:
                        return await asyncio.to_thread(path.read_bytes), source
                except (OSError, ValueError):
                    reason = "本地图片读取失败，请检查文件权限或重新发送图片"
        raise RuntimeError(reason)
