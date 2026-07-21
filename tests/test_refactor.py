import asyncio
import io
import os
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch


class RefactorContractsTest(unittest.TestCase):
    def test_voice_download_uses_aiogram_download(self):
        from handlers.voice import _download_tg_file

        class Bot:
            def __init__(self):
                self.download_mock = AsyncMock(return_value=io.BytesIO(b"audio"))

            async def download(self, file_id):
                return await self.download_mock(file_id)

        bot = Bot()
        result = asyncio.run(_download_tg_file(bot, "file-id"))

        self.assertEqual(result, b"audio")
        bot.download_mock.assert_awaited_once_with("file-id")

    def test_handlers_import_without_torch(self):
        result = subprocess.run(
            [sys.executable, "-c", "import handlers"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chat_action_callback_remains_reachable(self):
        from handlers.agent_cb import send_agent_callback

        bot = SimpleNamespace(send_chat_action=AsyncMock())
        message = SimpleNamespace(
            bot=bot,
            chat=SimpleNamespace(id=-1001, type="supergroup"),
            message_id=7,
            from_user=SimpleNamespace(id=42),
        )

        asyncio.run(send_agent_callback(
            {"type": "tg_chat_action", "action": "typing"},
            message=cast(Any, message),
            reply_kwargs={},
        ))

        bot.send_chat_action.assert_awaited_once_with(chat_id=-1001, action="typing")

    def test_kick_tool_builds_temporary_ban(self):
        from agent.tg_api import handle_tg_tool

        async def scenario():
            with patch("agent.tg_api._tg_api", new=AsyncMock(return_value={"ok": True})) as api:
                result, media = await handle_tg_tool(
                    "tg_kick_user", {"user_id": 42}, None, AsyncMock(), -1001
                )
            return result, media, api

        result, media, api = asyncio.run(scenario())

        self.assertIn("кикнут", result)
        self.assertIsNone(media)
        call = api.await_args
        assert call is not None
        self.assertGreater(call.args[1]["until_date"], 0)

    def test_migration_removes_agent_tasks(self):
        import aiosqlite
        from database.migrations import apply_migrations

        async def scenario():
            db = await aiosqlite.connect(":memory:")
            await db.executescript(
                "CREATE TABLE _schema_version (version INTEGER PRIMARY KEY);"
                "INSERT INTO _schema_version VALUES (2);"
                "CREATE TABLE agent_tasks (task_id TEXT PRIMARY KEY);"
            )
            applied = await apply_migrations(db)
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_tasks'"
            )
            row = await cursor.fetchone()
            await db.close()
            return applied, row

        applied, row = asyncio.run(scenario())
        self.assertEqual(applied, 1)
        self.assertIsNone(row)

    def test_playwright_screenshot_uses_photo_payload(self):
        from agent.sandbox import _tool_playwright_browse

        with tempfile.TemporaryDirectory() as directory:
            class Workspace:
                host_path = directory

                def write(self, path, content):
                    assert path == "_pw_script.py"
                    assert content

                async def run_as_user(self, command):
                    assert command == ["python3", "_pw_script.py"]
                    with open(os.path.join(directory, "_pw_screen.png"), "wb") as image:
                        image.write(b"png")
                    return "SCREENSHOT_DONE", "", 0

            payloads = []

            async def send(payload):
                payloads.append(payload)

            asyncio.run(
                _tool_playwright_browse(
                    "https://example.com", "screenshot", ws=cast(Any, Workspace()), send_cb=send
                )
            )

        self.assertEqual(payloads, [{
            "type": "photo",
            "data": b"png",
            "caption": "📸 https://example.com",
            "filename": "screenshot.png",
        }])


if __name__ == "__main__":
    unittest.main()
