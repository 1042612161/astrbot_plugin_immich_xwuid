import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock


def setup_jobs(plugin, storage=None):
    plugin._sync_instance_id = "test-instance"
    plugin._active_sync_task = None
    plugin._active_sync_id = None
    plugin._sync_start_lock = asyncio.Lock()
    plugin._terminating = False
    storage = {} if storage is None else storage
    plugin.get_kv_data = AsyncMock(side_effect=lambda key, default: deepcopy(storage.get(key, default)))

    async def save(key, value):
        storage[key] = deepcopy(value)

    plugin.put_kv_data = AsyncMock(side_effect=save)
    return storage


def make_job(roles=None, target="panel"):
    return {"job_id": "job", "state": "running", "type": target, "roles": roles or ["1105"],
            "progress": "正在准备同步…", "success": 0, "duplicate": 0, "failure_count": 0,
            "failures": [], "failure_details_truncated": False, "total": 0, "processed": 0,
            "instance_id": "test-instance"}
