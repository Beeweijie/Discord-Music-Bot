import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from bot_app.application.management import ManagementService
from bot_app.infrastructure.persistence import runtime
from bot_app.infrastructure.persistence.logs import FileLogReader
from bot_app.infrastructure.persistence.settings import JsonSettingsRepository
from bot_app.presentation.api.server import create_app


class StorageFixture:
    def setup_storage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, path in {"STATUS_FILE": "status.json", "COMMANDS_DIR": "commands", "RESULTS_DIR": "results"}.items():
            p = patch.object(runtime, name, self.root / path)
            p.start()
            self.addCleanup(p.stop)
        runtime.write_status(reset=True, pid=123, instance_id="test-instance", state="online",
                             music_sessions={"1:2": {"guild_id": 123456789012345678, "channel_id": 2}})
        self.service = ManagementService(runtime, JsonSettingsRepository(self.root),
                                         FileLogReader(self.root / "bot.log"))


class ManagementTests(StorageFixture, unittest.TestCase):
    def setUp(self):
        self.setup_storage()

    def test_settings_round_trip_and_invalid_write_preserves_file(self):
        expected = {"enabled": True, "level": "strict", "model": "medium"}
        self.service.update_settings("voice_moderation", expected)
        self.assertEqual(self.service.get_settings("voice_moderation"), expected)
        with self.assertRaises(ValueError):
            self.service.update_settings("voice_moderation", dict(expected, model="bad"))
        self.assertEqual(self.service.get_settings("voice_moderation"), expected)
        with self.assertRaises(ValueError):
            self.service.get_settings("../secret")

    def test_command_round_trip_and_acknowledgement(self):
        identifier = self.service.submit_control({"action": "skip", "session_id": "1:2"})
        commands = runtime.drain_control_commands()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["id"], identifier)
        self.assertEqual(commands[0]["instance_id"], "test-instance")
        self.assertEqual(runtime.drain_control_commands(), [])
        runtime.write_control_result(commands[0], True, "done")
        self.assertTrue(self.service.get_control_result(identifier)["ok"])
        self.assertEqual(self.service.get_status()["last_control"]["id"], identifier)

    def test_expired_and_previous_instance_commands_are_rejected(self):
        for previous in (False, True):
            identifier = self.service.submit_control({"action": "skip", "session_id": "1:2"})
            path = runtime.COMMANDS_DIR / f"{identifier}.json"
            command = json.loads(path.read_text())
            if previous:
                command["instance_id"] = "old-instance"
            else:
                command["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
            path.write_text(json.dumps(command))
            self.assertEqual(runtime.drain_control_commands(), [])
            self.assertFalse(self.service.get_control_result(identifier)["ok"])

    def test_invalid_controls_do_not_publish_files(self):
        for command in [{"action":"shell", "session_id":"1:2"},
                        {"action":"volume", "session_id":"1:2", "volume": True},
                        {"action":"volume", "session_id":"1:2", "volume": 101},
                        {"action":"loop", "session_id":"1:2", "mode": []}]:
            with self.assertRaises(ValueError):
                self.service.submit_control(command)
        self.assertFalse(runtime.COMMANDS_DIR.exists())

    def test_log_tail_and_result_path_validation(self):
        (self.root / "bot.log").write_text("a\nb\nc\n", encoding="utf-8")
        self.assertEqual(self.service.get_logs(2), ["b", "c"])
        with self.assertRaises(ValueError):
            self.service.get_control_result("../../secret")


class ApiTests(StorageFixture, unittest.IsolatedAsyncioTestCase):
    async def test_log_server_selection_and_filters(self):
        from bot_app.infrastructure.logging_setup import ScopedLogHandler
        import logging
        self.service.catalog = lambda: [{"id": "1"}, {"id": "2"}]
        handler = ScopedLogHandler(self.root)
        try:
            for guild in (1, 2):
                for level in (logging.INFO, logging.ERROR):
                    record = logging.LogRecord('test', level, '', 0, f'server {guild}', (), None)
                    record.guild_id = guild
                    handler.handle(record)
        finally:
            handler.close()
        response = await self.client.get('/api/v1/logs?guild_id=1&level=ERROR')
        self.assertEqual(response.status, 200)
        lines = (await response.json())['lines']
        self.assertEqual(len(lines), 1)
        self.assertIn('server 1', lines[0])
        self.assertNotIn('server 2', lines[0])
        response = await self.client.get('/api/v1/logs')
        self.assertEqual((await response.json())['lines'], [])
        for query in ('guild_id=3', 'guild_id=../secret', 'level=INVALID', 'details=1'):
            response = await self.client.get('/api/v1/logs?' + query)
            self.assertEqual(response.status, 400)

    async def asyncSetUp(self):
        self.setup_storage()
        self.client = TestClient(TestServer(create_app(self.service)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"X-Requested-With": "BotDashboard"}

    async def test_direct_access_and_lossless_discord_ids(self):
        response = await self.client.get("/api/v1/status")
        self.assertEqual(response.status, 200)
        response = await self.client.get("/api/v1/sessions", headers=self.headers)
        self.assertEqual((await response.json())["1:2"]["guild_id"], "123456789012345678")

    async def test_http_control_reaches_same_consumer_as_desktop(self):
        response = await self.client.post("/api/v1/sessions/1:2/controls", headers=self.headers,
                                          json={"action": "volume", "volume": 50})
        self.assertEqual(response.status, 202)
        identifier = (await response.json())["id"]
        command = runtime.drain_control_commands()[0]
        self.assertEqual(command["volume"], 50)
        runtime.write_control_result(command, True, "done")
        response = await self.client.get(f"/api/v1/controls/{identifier}", headers=self.headers)
        self.assertTrue((await response.json())["ok"])

    async def test_settings_effect_and_bad_requests(self):
        response = await self.client.put("/api/v1/settings/voice_moderation", headers=self.headers,
                                         json={"enabled": False, "level": "low", "model": "small"})
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["applied"])
        response = await self.client.post("/api/v1/sessions/1:2/controls", headers=self.headers, json=[])
        self.assertEqual(response.status, 400)
        response = await self.client.get("/api/v1/logs?limit=99999", headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertIn("error", await response.json())

    async def test_cross_origin_writes_are_rejected(self):
        response = await self.client.post("/api/v1/sessions/1:2/controls",
            headers=dict(self.headers, Origin="https://example.com"), json={"action": "skip"})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/v1/sessions/1:2/controls", json={"action": "skip"})
        self.assertEqual(response.status, 403)

    async def test_guild_settings_are_independent_and_survive_repository_reload(self):
        self.service.catalog = lambda: [{"id": "1", "channels": [{"id": "11"}]}, {"id": "2", "channels": []}]
        original = self.service.get_settings("music")
        for guild in ("1", "2"):
            response = await self.client.get("/api/v1/settings/music?guild_id=" + guild)
            self.assertEqual(await response.json(), original)
        changed = dict(original, max_queue_size=501, default_playlist_url="https://music.youtube.com/playlist?list=custom")
        response = await self.client.put("/api/v1/settings/music?guild_id=1", headers=self.headers, json=changed)
        self.assertEqual(response.status, 200)
        repo = JsonSettingsRepository(self.root)
        self.assertEqual(repo.read("music", "1"), changed)
        self.assertEqual(repo.read("music", "2"), original)
        self.assertEqual(repo.read("music"), original)
        repo.write("music", dict(original, default_volume=70))
        self.assertEqual(repo.read("music", "2"), original)
        response = await self.client.get("/api/v1/settings/music?guild_id=999")
        self.assertEqual(response.status, 400)
        response = await self.client.put("/api/v1/settings/music?guild_id=1", headers=self.headers,
            json=dict(changed, default_playlist_url="file:///etc/passwd"))
        self.assertEqual(response.status, 400)
        self.assertEqual(repo.read("music", "1"), changed)
        with self.assertRaises(ValueError):
            repo.read("music", "../outside")

    async def test_welcome_channel_must_belong_to_selected_guild(self):
        self.service.catalog = lambda: [{"id": "1", "channels": [{"id": "11"}]}, {"id": "2", "channels": []}]
        values = {"enabled": True, "include_bots": False, "channel_id": "11"}
        response = await self.client.put("/api/v1/settings/welcome?guild_id=1", headers=self.headers, json=values)
        self.assertEqual(response.status, 200)
        response = await self.client.put("/api/v1/settings/welcome?guild_id=2", headers=self.headers, json=values)
        self.assertEqual(response.status, 400)
