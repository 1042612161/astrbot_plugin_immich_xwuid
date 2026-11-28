import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.utils import plugin_kv_store
from astrbot.core.utils.shared_preferences import SharedPreferences
from astrbot_plugin_immich_xwuid.main import ImmichXWUIDSync, _LATEST_SYNC_KEY
from job_helpers import make_job


class ActualAstrBotStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_state_survives_fresh_store_without_growing_database_or_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(str(Path(directory) / "test.db"))
            await database.initialize()
            stores = []
            try:
                def new_store():
                    store = SharedPreferences(database, str(Path(directory) / "unused.json"))
                    stores.append(store)
                    return store

                first_store = new_store()
                plugin = ImmichXWUIDSync(SimpleNamespace())
                plugin.plugin_id = "offline-test-plugin"
                with patch.object(plugin_kv_store, "sp", first_store):
                    for index in range(20):
                        await plugin._save_sync_job({**make_job(), "job_id": str(index)})
                    rows = await first_store.range_get_async("plugin", plugin.plugin_id)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(len(first_store._cache), 1)
                # A fresh store starts with no overlay; it must read SQLite.
                fresh_store = new_store()
                restarted = ImmichXWUIDSync(SimpleNamespace())
                restarted.plugin_id = plugin.plugin_id
                with patch.object(plugin_kv_store, "sp", fresh_store):
                    await restarted.initialize()
                    record = await restarted.get_kv_data(_LATEST_SYNC_KEY, None)
                    self.assertEqual(record["job_id"], "19")
                    self.assertEqual(record["state"], "interrupted")
                    self.assertIsNone(restarted._active_sync_task)
                    self.assertEqual(len(await fresh_store.range_get_async("plugin", plugin.plugin_id)), 1)
            finally:
                for store in stores:
                    await store.flush()
                    store._scheduler.shutdown(wait=False)
                await database.engine.dispose()
