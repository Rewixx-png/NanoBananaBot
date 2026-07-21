import asyncio
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock

from aiogram.types import InputRichMessage

from handlers.common import send_rich_message


class BotStub:
    def __init__(
        self,
        *,
        rich_result=None,
        rich_side_effect=None,
        message_result=None,
        message_side_effect=None,
    ):
        self.rich_mock = AsyncMock(return_value=rich_result, side_effect=rich_side_effect)
        self.message_mock = AsyncMock(return_value=message_result, side_effect=message_side_effect)

    async def send_rich_message(self, **kwargs):
        return await self.rich_mock(**kwargs)

    async def send_message(self, **kwargs):
        return await self.message_mock(**kwargs)


class RichMessageContractsTest(unittest.TestCase):
    def test_uses_typed_rich_html_as_primary_path(self):
        sent = object()
        bot = BotStub(rich_result=sent)

        result = asyncio.run(send_rich_message(cast(Any, bot), 42, "<b>Готово</b>"))

        self.assertIs(result, sent)
        call = bot.rich_mock.await_args
        assert call is not None
        rich_message = call.kwargs["rich_message"]
        self.assertIsInstance(rich_message, InputRichMessage)
        self.assertEqual(rich_message.html, "<b>Готово</b>")
        self.assertIsNone(rich_message.markdown)
        bot.message_mock.assert_not_awaited()

    def test_sanitizes_ai_html_before_rich_send(self):
        bot = BotStub(rich_result=object())
        unsafe = (
            '<b>Готово</b><script>alert(1)</script>'
            '<a href="javascript:alert(2)" onclick="steal()">ссылка</a>'
        )

        asyncio.run(send_rich_message(cast(Any, bot), 42, unsafe))

        call = bot.rich_mock.await_args
        assert call is not None
        html = call.kwargs["rich_message"].html
        self.assertIn("<b>Готово</b>", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("onclick", html)

    def test_falls_back_to_regular_html(self):
        sent = object()
        bot = BotStub(rich_side_effect=RuntimeError("unsupported"), message_result=sent)

        result = asyncio.run(send_rich_message(cast(Any, bot), 42, "<b>Готово</b>"))

        self.assertIs(result, sent)
        call = bot.message_mock.await_args
        assert call is not None
        self.assertEqual(call.kwargs["text"], "<b>Готово</b>")
        self.assertEqual(call.kwargs["parse_mode"], "HTML")

    def test_rich_table_and_list_degrade_readably(self):
        bot = BotStub(rich_side_effect=RuntimeError("unsupported"), message_result=object())
        rich_html = (
            "<table><tr><th>Имя</th><th>Статус</th></tr>"
            "<tr><td>Nano</td><td>Готов</td></tr></table>"
            "<ul><li>Первый шаг</li><li>Второй шаг</li></ul>"
        )

        asyncio.run(send_rich_message(cast(Any, bot), 42, rich_html))

        call = bot.message_mock.await_args
        assert call is not None
        fallback = call.kwargs["text"]
        self.assertIn("Имя · Статус", fallback)
        self.assertIn("Nano · Готов", fallback)
        self.assertIn("• Первый шаг", fallback)
        self.assertIn("• Второй шаг", fallback)

    def test_falls_back_to_plain_text_after_html_error(self):
        sent = object()
        bot = BotStub(
            rich_side_effect=RuntimeError("unsupported"),
            message_side_effect=[RuntimeError("bad html"), sent],
        )

        result = asyncio.run(send_rich_message(cast(Any, bot), 42, "<b>Готово</b>"))

        self.assertIs(result, sent)
        fallback = bot.message_mock.await_args_list[1]
        self.assertEqual(fallback.kwargs["text"], "Готово")
        self.assertIsNone(fallback.kwargs["parse_mode"])

    def test_plain_fallback_strips_rich_tags_but_keeps_text(self):
        bot = BotStub(
            rich_side_effect=RuntimeError("unsupported"),
            message_side_effect=[RuntimeError("bad html"), object()],
        )
        rich_html = (
            '<tg-emoji emoji-id="5368324170671202286">🙂</tg-emoji>'
            '<tg-spoiler>секрет</tg-spoiler>'
        )

        asyncio.run(send_rich_message(cast(Any, bot), 42, rich_html))

        fallback = bot.message_mock.await_args_list[1].kwargs["text"]
        self.assertEqual(fallback, "🙂секрет")

    def test_long_fallback_preserves_every_character(self):
        bot = BotStub(rich_side_effect=RuntimeError("unsupported"), message_result=object())
        body = "x" * 4500

        asyncio.run(send_rich_message(cast(Any, bot), 42, f"<b>{body}</b>"))

        calls = bot.message_mock.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual("".join(call.kwargs["text"] for call in calls), body)
        self.assertTrue(all(call.kwargs["parse_mode"] is None for call in calls))

    def test_long_fallback_retries_failed_later_chunk(self):
        first, last = object(), object()
        bot = BotStub(
            rich_side_effect=RuntimeError("unsupported"),
            message_side_effect=[first, RuntimeError("retry after 0"), last],
        )

        result = asyncio.run(send_rich_message(cast(Any, bot), 42, "x" * 4500))

        self.assertIs(result, last)
        self.assertEqual(len(bot.message_mock.await_args_list), 3)
        self.assertEqual(
            bot.message_mock.await_args_list[1].kwargs["text"],
            bot.message_mock.await_args_list[2].kwargs["text"],
        )

    def test_long_fallback_stops_after_retry_exhaustion(self):
        retry_error = RuntimeError("retry after 0")
        bot = BotStub(
            rich_side_effect=RuntimeError("unsupported"),
            message_side_effect=[object(), retry_error, retry_error, retry_error, object()],
        )

        result = asyncio.run(send_rich_message(cast(Any, bot), 42, "x" * 9000))

        self.assertIsNone(result)
        self.assertEqual(len(bot.message_mock.await_args_list), 4)


if __name__ == "__main__":
    unittest.main()
