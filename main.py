from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star, register
from astrbot.api.web import request, json_response
from astrbot.core.star.filter.command import GreedyStr

from .clients import ImmichClient, WavesClient
from .core import (
    DEFAULT_SPECIFICATIONS, classify_image, folders_by_type, match_mappings,
    parse_sync_command, resolve_role_folders, role_album_name, role_label,
    validate_mappings, validate_specifications,
)


_LATEST_SYNC_KEY = "latest_sync"
_MAX_SYNC_FAILURE_DETAILS = 99


@register("astrbot_plugin_immich_xwuid", "Codex", "Immich 与鸣潮 XWUID 面板图/背景图同步", "1.2.1")
class ImmichXWUIDSync(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._sync_instance_id = uuid4().hex
        self._active_sync_task = None
        self._active_sync_id = None
        self._sync_start_lock = asyncio.Lock()
        self._terminating = False
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
            data = await self._api_data()
            data["last_sync"] = await self._read_latest_sync()
            return json_response(data)
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
        if not isinstance(payload, dict):
            return json_response({"error": "请提交有效的同步请求。"}, status_code=400)
        target = payload.get("type")
        roles = payload.get("roles", [])
        if target not in ("panel", "background") or not isinstance(roles, list) or not roles:
            return json_response({"error": "请选择面板图或背景图，并至少选择一个角色。"}, status_code=400)
        roles = list(dict.fromkeys(str(x).strip() for x in roles if str(x).strip()))
        if not roles:
            return json_response({"error": "请至少选择一个有效角色。"}, status_code=400)
        try:
            job_id = await self._start_sync(target, roles)
        except RuntimeError as exc:
            return json_response({"error": str(exc)}, status_code=409)
        return json_response({"job_id": job_id})

    async def _start_sync(self, target, roles, failures=None):
        async with self._sync_start_lock:
            if self._terminating:
                raise RuntimeError("插件正在关闭，请稍后重试。")
            if self._active_sync_task is not None and not self._active_sync_task.done():
                raise RuntimeError("已有同步任务正在运行，请等待完成后再启动。")
            await self._read_latest_sync_unlocked()
            job_id = uuid4().hex
            job = {
                "job_id": job_id, "state": "running", "type": target, "roles": roles,
                "progress": "正在准备同步…", "success": 0, "duplicate": 0,
                "failure_count": 0, "failures": [], "failure_details_truncated": False,
                "total": 0, "processed": 0, "started_at": self._now_iso(),
                "updated_at": self._now_iso(), "instance_id": self._sync_instance_id,
            }
            for failure in failures or []:
                self._record_sync_failure(job, failure)
            await self._save_sync_job(job)
            self._active_sync_id = job_id
            self._active_sync_task = asyncio.create_task(self._run_web_sync(job))
            self._active_sync_task.add_done_callback(self._sync_task_done)
        return job_id

    @staticmethod
    def _sync_task_done(task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("图库同步后台任务异常退出，最近状态将在读取时标记中断。")

    async def web_sync_status(self):
        job = await self._read_latest_sync()
        if not job:
            return json_response({"state": "idle", "progress": "还没有同步任务记录。"},
                                 headers={"Cache-Control": "no-store"})
        return json_response(job, headers={"Cache-Control": "no-store"})

    @staticmethod
    def _now_iso():
        return datetime.now(timezone.utc).isoformat()

    async def _save_sync_job(self, job):
        job["updated_at"] = self._now_iso()
        # The KV store is the single durable source of task progress and result.
        await self.put_kv_data(_LATEST_SYNC_KEY, {**job, "failures": list(job["failures"])})

    async def _read_latest_sync(self):
        async with self._sync_start_lock:
            return await self._read_latest_sync_unlocked()

    async def _read_latest_sync_unlocked(self):
        job = await self.get_kv_data(_LATEST_SYNC_KEY, None)
        if not isinstance(job, dict):
            return None
        task = self._active_sync_task
        task_missing = task is None or task.done() or self._active_sync_id != job.get("job_id")
        if job.get("state") == "running" and (
            job.get("instance_id") != self._sync_instance_id or task_missing
        ):
            job["state"] = "interrupted"
            job["progress"] = "上次运行的同步任务已中断（进程重启或任务退出）；任务未自动重跑。"
            job["instance_id"] = None
            job["finished_at"] = self._now_iso()
            await self._save_sync_job(job)
        return job

    async def initialize(self):
        await self._read_latest_sync()

    async def terminate(self):
        async with self._sync_start_lock:
            self._terminating = True
            task = self._active_sync_task
            if task is not None and not task.done():
                task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await self._read_latest_sync()

    async def _run_web_sync(self, job):
        job_id, roles, target = job["job_id"], job["roles"], job["type"]
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
                    self._record_sync_failure(job, f"{role_name}：未配置有效的 Immich 相册映射")
                    continue
                assets = await asyncio.to_thread(immich.album_assets, album_by_name[album_name]["id"])
                known = await asyncio.to_thread(waves.images, target, folder["char_id"])
                known_names = {x.get("name") for x in known}
                plans.append((role_name, folder, assets, known_names))
            job["total"] = sum(len(plan[2]) for plan in plans)
            job["progress"] = f"共 {job['total']} 张待检查图片"
            await self._save_sync_job(job)
            for role, folder, assets, known_names in plans:
                job["progress"] = f"正在同步 {role}（{len(assets)} 张）"
                await self._save_sync_job(job)
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
                                self._record_sync_failure(job, f"{role}/{filename}：图片比例不符合目标规格")
                            else:
                                result = await asyncio.to_thread(waves.upload_and_confirm, data, Path(filename).name, target, folder["char_id"])
                                if result["status"] == "success":
                                    job["success"] += 1
                                elif result["status"] == "duplicate":
                                    job["duplicate"] += 1
                                else:
                                    self._record_sync_failure(job, f"{role}/{filename}：{result.get('reason', '编辑台拒绝上传')}")
                        except Exception as exc:
                            self._record_sync_failure(job, f"{role}/{filename}：{exc}")
                    job["processed"] += 1
                    if job["processed"] % 5 == 0 or job["processed"] == job["total"]:
                        job["progress"] = self._sync_progress(job)
                        await self._save_sync_job(job)
            job["state"] = "completed"
            job["progress"] = f"同步完成：成功 {job['success']}，重复/已存在 {job['duplicate']}，失败 {job['failure_count']}"
        except Exception as exc:
            job["state"] = "failed"
            self._record_sync_failure(job, str(exc))
            job["progress"] = f"同步失败：成功 {job['success']}，重复/已存在 {job['duplicate']}，失败 {job['failure_count']}"
        finally:
            if job["state"] == "running":
                job["state"] = "interrupted"
                job["progress"] = "同步任务被进程关闭或插件卸载中断；任务未自动重跑。"
                job["instance_id"] = None
            job["finished_at"] = self._now_iso()
            try:
                await self._save_sync_job(job)
            finally:
                if self._active_sync_id == job_id:
                    self._active_sync_id = None
                    self._active_sync_task = None

    @staticmethod
    def _record_sync_failure(job, message):
        job["failure_count"] = int(job.get("failure_count", 0)) + 1
        failures = job.setdefault("failures", [])
        if len(failures) < _MAX_SYNC_FAILURE_DETAILS:
            failures.append(str(message)[:2000])
        else:
            job["failure_details_truncated"] = True

    @staticmethod
    def _sync_progress(job):
        details = "；失败明细仅保留前 99 条" if job.get("failure_details_truncated") else ""
        return (f"已检查 {job['processed']}/{job['total']} 张 · 成功 {job['success']} · "
                f"重复 {job['duplicate']} · 失败 {job['failure_count']}{details}")

    def _sync_config_int(self, key, default, minimum):
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

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
        max_forward_depth = self._sync_config_int("forward_max_depth", 1, 0)
        max_images = self._sync_config_int("forward_max_images", 50, 1)
        try:
            images, extraction = await self._image_sources(event, max_forward_depth, max_images)
        except RuntimeError as exc:
            yield event.plain_result(str(exc))
            return
        truncation = []
        if extraction["image_limit"]:
            truncation.append(f"图片数量超出配置上限 {max_images} 张")
        if extraction["depth_limit"]:
            truncation.append(f"嵌套转发深度超出配置上限 {max_forward_depth} 层")
        truncation_note = f"\n转发内容已截断：{'；'.join(truncation)}。" if truncation else ""
        if not images:
            yield event.plain_result("没有找到图片。请将图片与命令一起发送，或引用图片/合并转发后发送命令。" + truncation_note)
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
        album_checksums = {}
        counts = {"created": 0, "reused": 0, "skipped": 0, "failed": 0}
        async with httpx.AsyncClient(timeout=90) as client:
            for index, sources in enumerate(images, 1):
                try:
                    data, _ = await self._read_image(sources, client)
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                        width, height = image.size
                        image_format = (image.format or "JPEG").lower()
                    extension, media_type = {
                        "jpeg": ("jpg", "image/jpeg"), "jpg": ("jpg", "image/jpeg"),
                        "png": ("png", "image/png"), "webp": ("webp", "image/webp"),
                        "gif": ("gif", "image/gif"), "bmp": ("bmp", "image/bmp"),
                    }.get(image_format, (image_format, Image.MIME.get(image_format.upper(), "application/octet-stream")))
                    kind = classify_image(width, height)
                    album = existing.get(album_names[kind])
                    if not album:
                        album = await asyncio.to_thread(immich.ensure_album, album_names[kind])
                        await asyncio.to_thread(immich.share_album_with_all_editors, album["id"])
                        existing[album_names[kind]] = album
                    digest = base64.b64encode(hashlib.sha1(data).digest()).decode()
                    album_id = album["id"]
                    if album_id not in album_checksums:
                        album_checksums[album_id] = await asyncio.to_thread(immich.album_checksums, album_id)
                    if digest in album_checksums[album_id]:
                        counts["skipped"] += 1
                        report.append(f"{index}. {album_names[kind]}已存在相同图片，跳过")
                        continue
                    checked = await asyncio.to_thread(immich.check_upload, digest)
                    if checked["action"] == "reject":
                        if checked["isTrashed"]:
                            raise RuntimeError("回收站已有相同图片，请先在 Immich 恢复后重试")
                        asset_id, status = checked["assetId"], "duplicate"
                    else:
                        timestamp = datetime.now(timezone.utc).isoformat()
                        source_name = f"{name}_{uuid4().hex}.{extension}"
                        upload = await client.post(str(self._cfg("immich_url")).rstrip("/") + "/api/assets", headers={
                            "x-api-key": str(self._cfg("immich_api_key")), "x-immich-checksum": digest,
                        }, data={"deviceAssetId": f"astrbot-{name}-{digest}", "deviceId": "astrbot-immich-xwuid",
                                 "fileCreatedAt": timestamp, "fileModifiedAt": timestamp},
                            files={"assetData": (source_name, data, media_type)})
                        upload.raise_for_status()
                        uploaded = upload.json()
                        asset_id, status = uploaded.get("id"), uploaded.get("status")
                        if not isinstance(asset_id, str) or not asset_id or status not in ("created", "duplicate"):
                            raise RuntimeError("Immich 未返回有效的资产编号或上传状态")
                    added = await asyncio.to_thread(immich.add_to_album, album_id, [asset_id])
                    # Record only successful uploads/associations; a failed image may be retried in this batch.
                    album_checksums[album_id].add(digest)
                    if not added:
                        counts["skipped"] += 1
                        report.append(f"{index}. {album_names[kind]}已存在相同图片，跳过")
                    elif status == "duplicate":
                        counts["reused"] += 1
                        report.append(f"{index}. 已复用 Immich 已有图片并加入{album_names[kind]}")
                    else:
                        counts["created"] += 1
                        report.append(f"{index}. 已上传到{album_names[kind]}")
                except Exception as exc:
                    counts["failed"] += 1
                    report.append(f"{index}. 失败：{exc}")
        yield event.plain_result(
            f"相册上传完成：共 {len(images)} 张\n新上传：{counts['created']}\n"
            f"复用已有图片并加入相册：{counts['reused']}\n目标相册已存在，跳过：{counts['skipped']}\n"
            f"失败：{counts['failed']}{truncation_note}\n\n" + "\n".join(report)
        )

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
            _, waves = self._clients()
            folders = await asyncio.to_thread(waves.folders, target)
            selected, missing = resolve_role_folders(folders, roles)
            job_id = await self._start_sync(target, [str(folder["char_id"]) for folder in selected],
                [f"{role}：XWUID 中不存在对应角色或 ID" for role in missing])
            task = self._active_sync_task
            yield event.plain_result("同步任务已在后台启动，可在 WebUI 查看进度；关闭页面不会停止任务。")
            if task is not None:
                previous_progress = None
                while not task.done():
                    # asyncio.wait does not cancel the worker if the chat handler disconnects.
                    await asyncio.wait({task}, timeout=1.2)
                    current = await self._read_latest_sync()
                    if not current or current["job_id"] != job_id:
                        break
                    progress = current.get("progress")
                    if current["state"] == "running" and progress != previous_progress:
                        previous_progress = progress
                        yield event.plain_result(progress)
                # Cancelling the command handler must not cancel the background worker.
                await asyncio.shield(task)
            result = await self._read_latest_sync()
            if result and result["job_id"] == job_id:
                summary = [result["progress"], *result["failures"]]
                if result.get("failure_details_truncated"):
                    summary.append("失败明细仅保留前 99 条。")
                yield event.plain_result("\n".join(summary))
            else:
                yield event.plain_result("该同步任务已结束，最近任务记录已被下一次任务替换。")
        except Exception as exc:
            yield event.plain_result(f"同步失败：{exc}")

    @staticmethod
    async def _image_sources(event, max_forward_depth=1, max_images=50):
        """Read image segments and bounded forward nodes; never interpret plain URLs."""
        message = getattr(getattr(event, "message_obj", None), "message", [])
        limits = {"image_limit": False, "depth_limit": False}
        sources, seen_sources, visited_forwards = [], set(), {}

        def segment_data(segment):
            if isinstance(segment, dict):
                return str(segment.get("type", "")).lower(), segment.get("data", {})
            kind = getattr(segment, "type", "")
            return str(getattr(kind, "value", kind)).lower(), getattr(segment, "__dict__", {})

        def unpack(value):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    # OneBot permits CQ strings as well as segment arrays.
                    segments = []
                    for kind, params in re.findall(r"\[CQ:(image|forward),([^\]]+)\]", value):
                        fields = {}
                        for param in params.split(","):
                            key, sep, field = param.partition("=")
                            if sep:
                                fields[key] = (field.replace("&#44;", ",").replace("&#91;", "[")
                                               .replace("&#93;", "]").replace("&amp;", "&"))
                        segments.append({"type": kind, "data": fields})
                    return segments
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                if value.get("type"):
                    return [value]
                for key in ("messages", "message", "nodes", "nodeList", "content", "data"):
                    if key in value:
                        return unpack(value[key])
            return []

        async def onebot_action(action, **params):
            bot = getattr(event, "bot", None)
            call = getattr(bot, "call_action", None)
            if not callable(call):
                call = getattr(getattr(bot, "api", None), "call_action", None)
            if not callable(call):
                raise RuntimeError("无法读取图片：当前 OneBot 连接不支持读取消息")
            self_id = getattr(getattr(event, "message_obj", None), "self_id", None)
            if self_id:
                params["self_id"] = self_id
            try:
                result = await call(action=action, **params)
            except Exception as exc:
                raise RuntimeError("读取引用消息失败，请检查消息是否已失效或重新发送图片") from exc
            if not isinstance(result, dict) or result.get("status") == "failed" or result.get("retcode", 0) != 0:
                raise RuntimeError("读取图片失败：OneBot 返回的数据格式无效或消息读取失败")
            result = result.get("data", result)
            if not isinstance(result, dict):
                raise RuntimeError("读取图片失败：OneBot 返回的数据格式无效")
            return result

        async def add_image(fields, in_forward):
            local, remote = [], []
            for key in ("path", "url", "file"):
                value = fields.get(key)
                if not isinstance(value, str) or not value:
                    continue
                if value.startswith(("http://", "https://")):
                    remote.append(value)
                elif "://" not in value and Path(value).is_absolute():
                    local.append(value)
            candidates = tuple(dict.fromkeys(local + remote))
            if any(source in seen_sources for source in candidates):
                return
            if len(sources) >= max_images:
                limits["image_limit"] = True
                return
            token = fields.get("file")
            if not candidates and in_forward and isinstance(token, str) and token and "://" not in token:
                # Raw forward nodes may contain a OneBot file identifier rather than a usable URL.
                resolved = await onebot_action("get_image", file=token)
                return await add_image(resolved, False)
            if not candidates:
                raise RuntimeError(f"第 {len(sources) + 1} 张图片没有可读取的本地路径或 HTTP/HTTPS 链接，请重新发送图片")
            sources.append(candidates)
            seen_sources.update(candidates)

        async def collect(chain):
            # Iterative traversal counts only nested forward boundaries, not node wrappers.
            stack = [(item, -1) for item in reversed(unpack(chain))]
            while stack:
                segment, depth = stack.pop()
                kind, fields = segment_data(segment)
                if kind == "image":
                    await add_image(fields, depth >= 0)
                    if limits["image_limit"]:
                        break
                elif kind in ("forward", "forward_msg"):
                    next_depth = depth + 1
                    if next_depth > max_forward_depth:
                        limits["depth_limit"] = True
                        continue
                    forward_id = fields.get("id") or fields.get("message_id")
                    nodes = unpack(fields.get("content") or fields.get("messages"))
                    if not nodes and forward_id:
                        if visited_forwards.get(str(forward_id), next_depth + 1) <= next_depth:
                            continue
                        visited_forwards[str(forward_id)] = next_depth
                        # OneBot v11 uses message_id; adapters also expose forward_id or id.
                        for parameter in ("message_id", "forward_id", "id"):
                            try:
                                result = await onebot_action("get_forward_msg", **{parameter: str(forward_id)})
                                nodes = unpack(result)
                                if not nodes:
                                    raise RuntimeError("转发消息没有可读取的节点")
                                break
                            except RuntimeError:
                                if parameter == "id":
                                    raise
                    stack.extend((item, next_depth) for item in reversed(nodes))
                elif kind in ("node", "nodes") or (not kind and isinstance(segment, dict)):
                    nodes = unpack(fields.get("content") or fields.get("nodes") or fields.get("message") or segment)
                    if nodes != [segment]:
                        stack.extend((item, max(0, depth)) for item in reversed(nodes))

        # Current direct images take precedence over all quoted content.
        direct = [item for item in message or [] if segment_data(item)[0] == "image"]
        if direct:
            await collect(direct)
        else:
            current_forwards = [item for item in message or [] if segment_data(item)[0] in ("forward", "node", "nodes")]
            if current_forwards:
                await collect(current_forwards)
            else:
                for segment in message or []:
                    if isinstance(segment, Reply):
                        chain, message_id = segment.chain, segment.id
                    elif isinstance(segment, dict) and segment.get("type") == "reply":
                        chain, message_id = segment.get("chain"), segment.get("data", {}).get("id")
                    else:
                        continue
                    if not chain and message_id is not None:
                        message_id = int(message_id) if str(message_id).isdigit() else message_id
                        chain = unpack(await onebot_action("get_msg", message_id=message_id))
                    await collect(chain)
                    if limits["image_limit"]:
                        break
        return sources, limits

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
