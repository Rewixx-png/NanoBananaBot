"""Tests for OmniRoute agent/vision integration and absence of hardcoded secrets/endpoints."""

import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent


class OmniRouteIntegrationTest(unittest.TestCase):
    def test_omniroute_vision_single_and_multi_image(self):
        from services import omniroute_service

        chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "Красный и синий"}}],
        })
        with patch.object(omniroute_service, "omniroute_chat", chat):
            result = asyncio.run(omniroute_service.analyze_images_with_omniroute(
                [b"\x89PNG\r\n\x1a\n1", b"\xff\xd8\xff2"],
                "Какие цвета?",
            ))

        self.assertEqual(result, "Красный и синий")
        kwargs = chat.await_args.kwargs
        self.assertEqual(kwargs["model"], omniroute_service.OMNIROUTE_VISION_MODEL)
        content = kwargs["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Какие цвета?"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_agent_vision_tools_route_to_omniroute(self):
        import agent.loop as agent_loop
        import agent.sandbox as agent_sandbox
        import agent.search as agent_search
        from agent.safety import _DebounceHook, _ToolBudget

        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "pic.png")
            with open(img_path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nfake")

            ws = SimpleNamespace(_safe_path=lambda p: os.path.join(tmpdir, os.path.basename(p)))
            vision_mock = AsyncMock(return_value="На картинке кот")

            with (
                patch.object(agent_loop, "analyze_image_with_omniroute", vision_mock),
                patch.object(agent_sandbox, "analyze_image_with_omniroute", vision_mock),
                patch.object(agent_search, "analyze_image_with_omniroute", AsyncMock(return_value="YES")),
            ):
                res_vision, _ = asyncio.run(agent_loop._execute_tool(
                    "vision",
                    {"path": "pic.png", "prompt": "Что там?"},
                    _DebounceHook(),
                    _ToolBudget(),
                    AsyncMock(),
                    None,
                    cast(Any, ws),
                ))
                res_analyze = asyncio.run(agent_sandbox._tool_analyze_image("pic.png", "Опиши", cast(Any, ws)))
                is_rel = asyncio.run(agent_search._image_is_relevant(b"\xff\xd8\xff", "cat"))

            self.assertEqual(res_vision, "На картинке кот")
            self.assertEqual(res_analyze, "На картинке кот")
            self.assertTrue(is_rel)
            self.assertEqual(vision_mock.await_count, 2)

    def test_media_video_shortcut_routes_to_omniroute(self):
        import handlers.media_in as media_in

        status = SimpleNamespace(delete=AsyncMock())
        message = SimpleNamespace(
            chat=SimpleNamespace(id=555111),
            from_user=SimpleNamespace(id=1, first_name="User", username=None),
            reply=AsyncMock(return_value=status),
        )
        omni_video = AsyncMock(return_value="Видео с котом")
        nvidia_video = AsyncMock(return_value="NVIDIA ответ")

        with (
            patch.object(media_in, "classify_agent_intent", new=AsyncMock(return_value=True)),
            patch.object(media_in, "analyze_video_with_omniroute", omni_video),
            patch("services.nvidia_vision.analyze_video", new=nvidia_video),
            patch.object(media_in, "add_user_stat", new=AsyncMock()),
            patch.object(media_in, "log_prompt", new=AsyncMock()),
        ):
            handled = asyncio.run(media_in._media_to_agent(
                cast(Any, message),
                b"video-bytes",
                "clip.mp4",
                "Что на видео?",
                {},
            ))

        self.assertTrue(handled)
        omni_video.assert_awaited_once()
        nvidia_video.assert_not_awaited()
        status.delete.assert_awaited_once()

    def test_classify_agent_intent_uses_llm_not_keyword_templates(self):
        import agent.loop as agent_loop

        omni = AsyncMock(return_value="false")
        with patch.object(agent_loop, "omniroute_text", omni):
            # Even prompts containing old trigger words like "скинь" or "create" must go to the LLM.
            result = asyncio.run(agent_loop.classify_agent_intent("просто скинь пару мыслей как дела"))

        self.assertFalse(result)
        omni.assert_awaited_once()

    def test_media_action_request_routes_to_agent_not_vision_shortcut(self):
        import handlers.media_in as media_in

        status = SimpleNamespace(delete=AsyncMock())
        message = SimpleNamespace(
            bot=SimpleNamespace(send_rich_message=AsyncMock(return_value=object())),
            chat=SimpleNamespace(id=555222),
            message_id=9,
            from_user=SimpleNamespace(id=1, first_name="User", username=None),
            reply=AsyncMock(return_value=status),
        )
        vision = AsyncMock(return_value="vision reply")
        run_agent = AsyncMock(return_value=("Готово, отправил файлом.", None))

        with (
            patch.object(media_in, "classify_agent_intent", new=AsyncMock(return_value=True)),
            patch.object(media_in, "classify_media_mode", new=AsyncMock(return_value="agent")),
            patch.object(media_in, "_analyze_photo_with_status", vision),
            patch.object(media_in, "run_agent", run_agent),
            patch.object(media_in, "send_rich_message", new=AsyncMock(return_value=object())),
            patch.object(media_in, "add_user_stat", new=AsyncMock()),
            patch.object(media_in, "log_prompt", new=AsyncMock()),
        ):
            handled = asyncio.run(media_in._media_to_agent(
                cast(Any, message),
                b"\x89PNG\r\n\x1a\nfake",
                "photo.png",
                "сделай чб и отправь файлом",
                {},
            ))

        self.assertTrue(handled)
        vision.assert_not_awaited()
        run_agent.assert_awaited_once()

    def test_send_workspace_file_supports_auto_and_explicit_media_types(self):
        from agent.tools import _tool_send_workspace_file

        with tempfile.TemporaryDirectory() as tmpdir:
            html_file = os.path.join(tmpdir, "index.html")
            png_file = os.path.join(tmpdir, "chart.png")
            ogg_file = os.path.join(tmpdir, "voice.ogg")
            with open(html_file, "w", encoding="utf-8") as f:
                f.write("<html><body>hi</body></html>")
            with open(png_file, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")
            with open(ogg_file, "wb") as f:
                f.write(b"OggS")

            ws = SimpleNamespace(_safe_path=lambda p: os.path.join(tmpdir, p))
            sent = []

            async def send_cb(payload):
                sent.append(payload)

            asyncio.run(_tool_send_workspace_file("index.html", "сайт", cast(Any, ws), send_cb, media_type="auto"))
            asyncio.run(_tool_send_workspace_file("chart.png", "график", cast(Any, ws), send_cb, media_type="auto"))
            asyncio.run(_tool_send_workspace_file("chart.png", "без сжатия", cast(Any, ws), send_cb, media_type="document"))
            asyncio.run(_tool_send_workspace_file("voice.ogg", "гс", cast(Any, ws), send_cb, media_type="auto"))

        self.assertEqual([item["type"] for item in sent], ["document", "photo", "document", "voice"])

    def test_agent_and_chat_prompts_keep_aggressive_persona(self):
        from agent.prompts import _build_system
        from config import SYSTEM_PROMPT
        from shared_types import _build_text_system_prompt

        self.assertIn("мат", SYSTEM_PROMPT.lower())
        self.assertIn("груб", SYSTEM_PROMPT.lower())
        for owner_flag in (False, True):
            prompt = _build_system(is_owner=owner_flag)
            self.assertIn("мат", prompt.lower())
            self.assertIn("агрессив", prompt.lower())
            chat_prompt = _build_text_system_prompt(is_owner=owner_flag)
            self.assertIn("мат", chat_prompt.lower())
            self.assertNotIn("общайся с уважением", chat_prompt.lower())

    def test_omniroute_text_retries_when_length_truncates_reasoning(self):
        import services.omniroute_service as omniroute

        chat = AsyncMock(side_effect=[
            {"choices": [{"finish_reason": "length", "message": {"content": ""}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": "Готово"}}]},
        ])
        with patch.object(omniroute, "omniroute_chat", chat):
            result = asyncio.run(omniroute.omniroute_text("Тест", max_tokens=256))

        self.assertEqual(result, "Готово")
        self.assertEqual(chat.await_count, 2)
        self.assertGreater(chat.await_args_list[1].kwargs["max_tokens"], 256)


class NoHardcodeAuditTest(unittest.TestCase):
    def test_no_hardcoded_secrets_or_personal_ids_in_source(self):
        forbidden_patterns = (
            ("hex API key literal", re.compile(r"\bsk-[a-f0-9]{16,}\b")),
            ("hardcoded OmniRoute IP:port", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:20128\b")),
            ("hardcoded supergroup chat id", re.compile(r"-100\d{10}\b")),
            ("hardcoded absolute project path", re.compile(r"/root/Projects/NanoHatani")),
        )
        source_files = (
            list(ROOT.glob("*.py"))
            + list((ROOT / "agent").glob("*.py"))
            + list((ROOT / "handlers").glob("*.py"))
            + list((ROOT / "services").glob("*.py"))
            + list((ROOT / "database").glob("*.py"))
            + list((ROOT / "keys").glob("*.py"))
            + [ROOT / "figma_plugin" / "code.js", ROOT / "figma_plugin" / "manifest.json"]
        )
        for path in source_files:
            content = path.read_text(encoding="utf-8")
            for label, pattern in forbidden_patterns:
                match = pattern.search(content)
                self.assertIsNone(
                    match,
                    f"Hardcoded {label} ({match.group(0)!r}) found in {path.relative_to(ROOT)}" if match else "",
                )

    def test_no_scattered_provider_urls_outside_config(self):
        provider_url_re = re.compile(
            r"https://(?:api\.firecrawl\.dev|api\.openai\.com|api\.replicate\.com|"
            r"integrate\.api\.nvidia\.com|ai\.api\.nvidia\.com|api\.groq\.com|"
            r"generativelanguage\.googleapis\.com|api\.figma\.com|api\.sunoapi\.org|"
            r"sunoapiorg\.redpandaai\.co|r\.jina\.ai)"
        )
        for folder in ("agent", "handlers", "services", "database", "keys"):
            for path in (ROOT / folder).glob("*.py"):
                for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    self.assertIsNone(
                        provider_url_re.search(line),
                        f"Hardcoded provider URL in {path.relative_to(ROOT)}:{lineno}: {stripped}",
                    )


if __name__ == "__main__":
    unittest.main()
