from __future__ import annotations

from typing import Iterable


DEFAULT_SPECIFICATIONS = {"panel": "竖图", "background": "横图"}


def validate_specifications(value: dict) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("规格名称必须包含面板图和背景图两项")
    result = {}
    for kind, label in (("panel", "面板图"), ("background", "背景图")):
        name = value.get(kind)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{label}规格名称不能为空")
        name = name.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError(f"{label}规格名称不能包含换行或控制字符")
        result[kind] = name
    if result["panel"] == result["background"]:
        raise ValueError("面板图和背景图规格名称不能相同")
    return result


def role_album_name(role: str, kind: str, specifications: dict[str, str]) -> str:
    return f"{role}-{specifications[kind]}"


def validate_mappings(value: dict, albums: list[dict], roles: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("相册映射必须是表单对象")
    album_names = {album["name"] for album in albums}
    result = {}
    for kind in DEFAULT_SPECIFICATIONS:
        table = value.get(kind, {})
        if not isinstance(table, dict):
            raise ValueError("面板图和背景图映射必须是表单对象")
        result[kind] = {}
        role_ids = {str(role["id"]): role for role in roles[kind]}
        for role_id, album in table.items():
            role_id = str(role_id)
            if role_id not in role_ids:
                raise ValueError(f"XWUID 中不存在角色 ID：{role_id}")
            if not isinstance(album, str) or (album and album not in album_names):
                role = role_ids[role_id]
                raise ValueError(f"{role['name']}（{role_id}）：所选 Immich 相册不存在，请刷新列表")
            result[kind][role_id] = album
    return result


def match_mappings(albums: list[dict], roles: dict, specifications: dict, mappings: dict | None = None) -> dict:
    album_names = {album["name"] for album in albums}
    result = {}
    for kind in DEFAULT_SPECIFICATIONS:
        existing = (mappings or {}).get(kind, {})
        table = {}
        for role in roles[kind]:
            role_id = str(role["id"])
            if role_id in existing:
                table[role_id] = existing[role_id]
            else:
                candidate = role_album_name(role["name"], kind, specifications)
                if candidate in album_names:
                    table[role_id] = candidate
        result[kind] = table
    return result


def classify_image(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效")
    return "background" if width > height else "panel"


def parse_sync_command(command: str, roles: str) -> tuple[list[str], str]:
    normalized = command.strip()
    if normalized == "同步背景图相册":
        target = "background"
    elif normalized == "同步面板图相册":
        target = "panel"
    else:
        raise ValueError("类型只能是面板图或背景图")
    names = list(dict.fromkeys(name.strip() for name in roles.replace("，", ",").split(",") if name.strip()))
    if not names:
        raise ValueError("请至少指定一个角色名")
    return names, target


def summarize_results(results: Iterable[dict]) -> dict:
    items = list(results)
    failures = []
    for item in items:
        if item.get("status") == "failed":
            reason = item.get("reason", "unknown")
            failures.append("图片比例不符合目标规格" if reason == "ratio" else str(reason))
    return {
        "success": sum(item.get("status") == "success" for item in items),
        "duplicate": sum(item.get("status") == "duplicate" for item in items),
        "failed": sum(item.get("status") == "failed" for item in items),
        "failures": failures,
    }


def folders_by_type(panel_folders: list[dict], background_folders: list[dict]) -> dict:
    return {
        "panel": [{"id": str(x["char_id"]), "name": str(x.get("char_name") or x["char_id"])} for x in panel_folders],
        "background": [{"id": str(x["char_id"]), "name": str(x.get("char_name") or x["char_id"])} for x in background_folders],
    }


def resolve_role_folders(folders: list[dict], tokens: list[str]) -> tuple[list[dict], list[str]]:
    """Resolve an ID or an unambiguous display name without choosing a duplicate name."""
    by_id = {str(folder["char_id"]): folder for folder in folders}
    by_name: dict[str, list[dict]] = {}
    for folder in folders:
        name = str(folder.get("char_name") or folder["char_id"])
        if str(folder["char_id"]) not in {str(item["char_id"]) for item in by_name.get(name, [])}:
            by_name.setdefault(name, []).append(folder)

    resolved: list[dict] = []
    missing: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in by_id:
            matches = [by_id[token]]
        else:
            matches = by_name.get(token, [])
            if len(matches) > 1:
                choices = "、".join(str(item["char_id"]) for item in matches)
                raise ValueError(f"{token} 对应多个角色 ID（{choices}），请改用具体 ID。")
        if not matches:
            missing.append(token)
            continue
        for folder in matches:
            role_id = str(folder["char_id"])
            if role_id not in seen:
                seen.add(role_id)
                resolved.append(folder)
    return resolved, missing


def role_label(folder: dict) -> str:
    name = str(folder.get("char_name") or folder["char_id"])
    return f"{name} · {folder['char_id']}"
