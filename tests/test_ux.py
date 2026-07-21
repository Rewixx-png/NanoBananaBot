import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
import unittest


class UxContractsTest(unittest.TestCase):
    def test_public_command_menu_is_short_and_ordered(self):
        from handlers.core import PUBLIC_COMMANDS

        self.assertEqual(
            [command.command for command in PUBLIC_COMMANDS],
            ["start", "help", "image", "video", "music", "tts", "voice", "up", "clear"],
        )
        self.assertTrue(all(command.description for command in PUBLIC_COMMANDS))

    def test_main_menu_exposes_four_sections_and_help(self):
        from handlers.core import _main_menu_keyboard

        keyboard = _main_menu_keyboard()
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertEqual(callbacks, [
            "menu:chat", "menu:create", "menu:voice", "menu:tools", "help:0",
        ])

    def test_create_section_uses_progressive_disclosure(self):
        from handlers.core import _section_view

        text, keyboard = _section_view("create")
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertIn("что будем создавать", text.lower())
        self.assertEqual(callbacks, [
            "guide:image", "guide:video", "guide:music", "guide:figma",
            "menu:home", "menu:close",
        ])

    def test_help_is_split_into_five_pages(self):
        from handlers.core import HELP_PAGES, _help_view

        self.assertEqual(len(HELP_PAGES), 5)
        for page in range(len(HELP_PAGES)):
            text, keyboard = _help_view(page)
            callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
            self.assertTrue(text.startswith("<b>"))
            self.assertIn("menu:home", callbacks)
            self.assertIn("menu:close", callbacks)

    def test_clear_requires_confirmation(self):
        from handlers.core import _clear_confirmation_keyboard

        keyboard = _clear_confirmation_keyboard()
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertEqual(callbacks, [
            "menu:clear:confirm", "menu:tools", "menu:close",
        ])

    def test_action_callbacks_recheck_membership(self):
        from handlers.common import callback_has_access

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                bot=object(),
                chat=SimpleNamespace(id=-1001),
            ),
            answer=AsyncMock(),
        )

        async def scenario():
            with patch("handlers.common.check_membership", new=AsyncMock(return_value=False)):
                return await callback_has_access(cast(Any, callback))

        self.assertFalse(asyncio.run(scenario()))
        call = callback.answer.await_args
        assert call is not None
        self.assertTrue(call.kwargs["show_alert"])

    def test_callback_middleware_blocks_resource_actions(self):
        from main import CallbackAccessMiddleware

        handler = AsyncMock(return_value="handled")
        event = SimpleNamespace(data="imgsel:request:model")

        async def scenario():
            with patch("main.callback_has_access", new=AsyncMock(return_value=False)):
                return await CallbackAccessMiddleware()(handler, cast(Any, event), {})

        self.assertIsNone(asyncio.run(scenario()))
        handler.assert_not_awaited()

    def test_callback_middleware_allows_safe_exit(self):
        from main import CallbackAccessMiddleware

        handler = AsyncMock(return_value="handled")
        event = SimpleNamespace(data="voice:cancel")

        async def scenario():
            with patch("main.callback_has_access", new=AsyncMock(return_value=False)) as access:
                result = await CallbackAccessMiddleware()(handler, cast(Any, event), {})
            access.assert_not_awaited()
            return result

        self.assertEqual(asyncio.run(scenario()), "handled")
        handler.assert_awaited_once()

    def test_image_prompt_request_survives_foreign_callback(self):
        from handlers.media_gen import handle_prompt_use
        from state import pending_prompt_requests

        pending_prompt_requests["req"] = {"user_id": 1}
        callback = SimpleNamespace(
            data="puse:req",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
        )
        try:
            asyncio.run(handle_prompt_use(cast(Any, callback)))
            self.assertIn("req", pending_prompt_requests)
        finally:
            pending_prompt_requests.pop("req", None)

    def test_tts_request_survives_foreign_callback(self):
        from handlers.media_tts import handle_tts_generate
        from state import pending_tts_configs

        pending_tts_configs["req"] = {"user_id": 1}
        callback = SimpleNamespace(
            data="ttsgen:req",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
        )
        try:
            asyncio.run(handle_tts_generate(cast(Any, callback)))
            self.assertIn("req", pending_tts_configs)
        finally:
            pending_tts_configs.pop("req", None)

    def test_music_request_survives_foreign_cancel(self):
        from handlers.music import _pending_music, music_model_callback

        _pending_music["req"] = {"user_id": 1}
        callback = SimpleNamespace(
            data="musicsel:req:cancel",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        try:
            asyncio.run(music_model_callback(cast(Any, callback)))
            self.assertIn("req", _pending_music)
        finally:
            _pending_music.pop("req", None)

    def test_voice_info_callbacks_fit_telegram_limit(self):
        from handlers.voice import _voice_info_keyboard

        keyboard = _voice_info_keyboard("voice-id-1234567890")
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertTrue(all(len(data.encode("utf-8")) <= 64 for data in callbacks if data))
        self.assertTrue(all("Очень длинное имя" not in data for data in callbacks if data))

    def test_foreign_voice_delete_is_blocked_before_api_call(self):
        from handlers.voice import voice_callback

        callback = SimpleNamespace(
            data="voice:delete_confirm:voice-id",
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
            message=SimpleNamespace(),
        )
        state = AsyncMock()

        async def scenario():
            with (
                patch("handlers.voice.get_voice_by_id", new=AsyncMock(return_value=None)),
                patch("handlers.voice.elevenlabs_delete_voice", new=AsyncMock()) as delete_api,
            ):
                await voice_callback(cast(Any, callback), state)
            return delete_api

        delete_api = asyncio.run(scenario())
        delete_api.assert_not_awaited()
        call = callback.answer.await_args
        assert call is not None
        self.assertTrue(call.kwargs["show_alert"])

    def test_foreign_tts_voice_selection_preserves_pending_text(self):
        from handlers.voice import _voice_active_users, voice_callback

        user_id = 42
        _voice_active_users.add(user_id)
        state = AsyncMock()
        state.get_data.return_value = {"tts_text": "keep me"}
        callback = SimpleNamespace(
            data="voice:tts_sel:foreign-voice",
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            bot=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(chat=SimpleNamespace(id=7), edit_text=AsyncMock()),
        )
        tts = AsyncMock(return_value=None)
        settings = {"tts_model": "eleven_v3", "stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0}
        try:
            with (
                patch("handlers.voice.get_voice_by_id", new=AsyncMock(return_value=None)),
                patch("handlers.voice.get_settings", new=AsyncMock(return_value=settings)),
                patch("handlers.voice.elevenlabs_tts", new=tts),
            ):
                asyncio.run(voice_callback(cast(Any, callback), state))
            tts.assert_not_awaited()
            state.clear.assert_not_awaited()
            self.assertIn(user_id, _voice_active_users)
            self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        finally:
            _voice_active_users.discard(user_id)

    def test_foreign_changer_voice_selection_preserves_pending_audio(self):
        from handlers.voice import _CHANGER_AUDIO, process_changer_voice_select

        user_id = 42
        pending = {"vocals": b"voice", "instrumental": b"music"}
        _CHANGER_AUDIO[user_id] = pending
        state = AsyncMock()
        callback = SimpleNamespace(
            data="vc:foreign-voice",
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        changer = AsyncMock(return_value=(None, "denied"))
        try:
            with (
                patch("handlers.voice.get_voice_by_id", new=AsyncMock(return_value=None)),
                patch("handlers.voice.elevenlabs_voice_changer", new=changer),
            ):
                asyncio.run(process_changer_voice_select(cast(Any, callback), state))
            changer.assert_not_awaited()
            self.assertIs(_CHANGER_AUDIO.get(user_id), pending)
            state.clear.assert_not_awaited()
            self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        finally:
            _CHANGER_AUDIO.pop(user_id, None)

    def test_foreign_speech_voice_selection_preserves_pending_audio(self):
        from handlers.voice import _CHANGE_AUDIO, process_change_voice_select

        user_id = 42
        _CHANGE_AUDIO[user_id] = b"audio"
        state = AsyncMock()
        callback = SimpleNamespace(
            data="change:foreign-voice",
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        changer = AsyncMock(return_value=(None, "denied"))
        try:
            with (
                patch("handlers.voice.get_voice_by_id", new=AsyncMock(return_value=None)),
                patch("handlers.voice.elevenlabs_voice_changer", new=changer),
            ):
                asyncio.run(process_change_voice_select(cast(Any, callback), state))
            changer.assert_not_awaited()
            self.assertEqual(_CHANGE_AUDIO.get(user_id), b"audio")
            state.clear.assert_not_awaited()
            self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        finally:
            _CHANGE_AUDIO.pop(user_id, None)

    def test_builtin_voice_selection_skips_ownership_lookup(self):
        from handlers.voice import _DEFAULT_VOICE_IDS, _can_use_voice

        lookup = AsyncMock(return_value=None)
        with patch("handlers.voice.get_voice_by_id", new=lookup):
            allowed = asyncio.run(_can_use_voice(42, next(iter(_DEFAULT_VOICE_IDS))))

        self.assertTrue(allowed)
        lookup.assert_not_awaited()

    def test_voice_cleanup_clears_every_transient_store(self):
        from handlers.voice import (
            _AUDIO_FOR_CLONE,
            _CHANGER_AUDIO,
            _CHANGE_AUDIO,
            _clear_voice_state,
            _voice_active_users,
        )

        user_id = 42
        _voice_active_users.add(user_id)
        _AUDIO_FOR_CLONE[user_id] = b"audio"
        _CHANGER_AUDIO[user_id] = b"changer"
        _CHANGE_AUDIO[user_id] = b"change"
        state = AsyncMock()

        asyncio.run(_clear_voice_state(user_id, state))

        self.assertNotIn(user_id, _voice_active_users)
        self.assertNotIn(user_id, _AUDIO_FOR_CLONE)
        self.assertNotIn(user_id, _CHANGER_AUDIO)
        self.assertNotIn(user_id, _CHANGE_AUDIO)
        state.clear.assert_awaited_once()

    def test_voice_entry_edit_failure_rolls_back_state(self):
        from handlers.voice import _voice_active_users, voice_callback

        user_id = 42
        state = AsyncMock()
        bot = SimpleNamespace(send_message=AsyncMock())
        callback = SimpleNamespace(
            data="voice:tts",
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            bot=bot,
            message=SimpleNamespace(
                chat=SimpleNamespace(id=7),
                edit_text=AsyncMock(side_effect=RuntimeError("edit failed")),
            ),
        )

        asyncio.run(voice_callback(cast(Any, callback), state))

        self.assertNotIn(user_id, _voice_active_users)
        state.clear.assert_awaited_once()
        self.assertIn("edit failed", bot.send_message.await_args.kwargs["text"])

    def test_voice_entry_state_failure_rolls_back_active_marker(self):
        from handlers.voice import _voice_active_users, voice_callback

        user_id = 43
        state = AsyncMock()
        state.set_state.side_effect = RuntimeError("state failed")
        bot = SimpleNamespace(send_message=AsyncMock())
        callback = SimpleNamespace(
            data="voice:tts",
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=7), edit_text=AsyncMock()),
        )

        asyncio.run(voice_callback(cast(Any, callback), state))

        self.assertNotIn(user_id, _voice_active_users)
        state.clear.assert_awaited_once()
        callback.message.edit_text.assert_not_awaited()
        self.assertIn("state failed", bot.send_message.await_args.kwargs["text"])

    def test_tts_selection_keeps_text_when_settings_fail(self):
        from handlers.voice import _voice_active_users, voice_callback

        user_id = 42
        _voice_active_users.add(user_id)
        state = AsyncMock()
        state.get_data.return_value = {"tts_text": "keep me"}
        callback = SimpleNamespace(
            data="voice:tts_sel:21m00Tcm4TlvDq8ikWAM",
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            bot=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(chat=SimpleNamespace(id=7), edit_text=AsyncMock()),
        )
        try:
            with patch("handlers.voice.get_settings", new=AsyncMock(side_effect=RuntimeError("db down"))):
                asyncio.run(voice_callback(cast(Any, callback), state))
            state.clear.assert_not_awaited()
            self.assertIn(user_id, _voice_active_users)
            self.assertIn("db down", callback.bot.send_message.await_args.kwargs["text"])
        finally:
            _voice_active_users.discard(user_id)
    def test_user_cooldowns_are_reduced_threefold(self):
        from config import FULL_ACCESS_CHAT_IMAGE_COOLDOWN, IMAGE_COOLDOWN_SECONDS, TEXT_COOLDOWN_SECONDS
        from handlers.common import _RANDOM_MEDIA_MIN_INTERVAL
        from handlers.media_gen import VIDEO_COOLDOWN
        from handlers.media_tts import TTS_COOLDOWN_SECONDS
        from handlers.music import _COOLDOWN as MUSIC_COOLDOWN

        self.assertEqual(FULL_ACCESS_CHAT_IMAGE_COOLDOWN, 10)
        self.assertEqual(IMAGE_COOLDOWN_SECONDS, 10)
        self.assertAlmostEqual(TEXT_COOLDOWN_SECONDS, 5 / 3)
        self.assertEqual(VIDEO_COOLDOWN, 20)
        self.assertEqual(TTS_COOLDOWN_SECONDS, 10)
        self.assertEqual(MUSIC_COOLDOWN, 20)
        self.assertEqual(_RANDOM_MEDIA_MIN_INTERVAL, 10)


    def test_invalid_image_input_does_not_start_cooldown(self):
        from handlers.media_gen import cmd_image
        from state import user_image_cooldowns

        user_id = 9001
        user_image_cooldowns.pop(user_id, None)
        message = SimpleNamespace(
            media_group_id=None,
            chat=SimpleNamespace(type="private", id=1),
            from_user=SimpleNamespace(id=user_id),
            text="/image",
            caption=None,
            photo=None,
            reply=AsyncMock(),
        )
        with patch("handlers.media_gen._track_user"):
            asyncio.run(cmd_image(cast(Any, message)))
        self.assertNotIn(user_id, user_image_cooldowns)

    def test_invalid_video_input_does_not_start_cooldown(self):
        from handlers.media_video import cmd_video
        from state import user_video_cooldowns

        user_id = 9002
        user_video_cooldowns.pop(user_id, None)
        message = SimpleNamespace(
            bot=object(),
            chat=SimpleNamespace(id=1),
            from_user=SimpleNamespace(id=user_id),
            text="/video",
            caption=None,
            photo=None,
            reply=AsyncMock(),
        )
        with patch("handlers.media_video.check_membership", new=AsyncMock(return_value=True)):
            asyncio.run(cmd_video(cast(Any, message)))
        self.assertNotIn(user_id, user_video_cooldowns)

    def test_invalid_tts_input_does_not_start_cooldown(self):
        from handlers.media_tts import cmd_tts
        from state import user_tts_cooldowns

        user_id = 9003
        user_tts_cooldowns.pop(user_id, None)
        message = SimpleNamespace(
            bot=object(),
            chat=SimpleNamespace(type="private", id=1),
            from_user=SimpleNamespace(id=user_id),
            text="/tts",
            reply=AsyncMock(),
        )
        with (
            patch("handlers.media_tts._track_user"),
            patch("handlers.media_tts.check_membership", new=AsyncMock(return_value=True)),
        ):
            asyncio.run(cmd_tts(cast(Any, message)))
        self.assertNotIn(user_id, user_tts_cooldowns)

    def test_voice_tts_preview_escapes_user_text(self):
        from handlers.voice import process_tts_text

        state = AsyncMock()
        message = SimpleNamespace(
            text="<b>unsafe & text</b>",
            from_user=SimpleNamespace(id=1),
            reply=AsyncMock(),
        )
        with patch("handlers.voice.get_voices", new=AsyncMock(return_value=[])):
            asyncio.run(process_tts_text(cast(Any, message), state))
        rendered = message.reply.await_args.args[0]
        self.assertIn("&lt;b&gt;unsafe &amp; text&lt;/b&gt;", rendered)

    def test_image_prompt_result_escapes_model_text(self):
        from handlers.media_gen import _run_prompt_generation
        from state import pending_prompt_requests

        pending_prompt_requests["req"] = {"prompt": "p", "images_bytes": [], "prev_prompts": []}
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        try:
            with patch(
                "handlers.media_gen.generate_image_prompt",
                new=AsyncMock(return_value=("<tag>&", "<перевод>&", None)),
            ):
                asyncio.run(_run_prompt_generation(cast(Any, callback), "req"))
            rendered = callback.message.edit_text.await_args_list[-1].args[0]
            self.assertIn("&lt;tag&gt;&amp;", rendered)
            self.assertIn("&lt;перевод&gt;&amp;", rendered)
        finally:
            pending_prompt_requests.pop("req", None)

    def test_music_progress_escapes_user_prompt(self):
        from handlers.music import MUSIC_MODELS, _pending_music, music_model_callback

        choice = next(iter(MUSIC_MODELS))
        _pending_music["req"] = {"user_id": 1, "prompt": "<verse>&", "chat_id": 1}
        message = SimpleNamespace(edit_text=AsyncMock())
        callback = SimpleNamespace(
            data=f"musicsel:req:{choice}",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            message=message,
        )
        try:
            with patch("handlers.music.generate_music", new=AsyncMock(return_value=(None, None, "failed"))):
                asyncio.run(music_model_callback(cast(Any, callback)))
            rendered = message.edit_text.await_args_list[0].args[0]
            self.assertIn("&lt;verse&gt;&amp;", rendered)
            self.assertNotIn("req", _pending_music)
        finally:
            _pending_music.pop("req", None)

    def test_music_ack_failure_keeps_pending_request(self):
        from handlers.music import MUSIC_MODELS, _cooldowns, _pending_music, music_model_callback

        choice = next(iter(MUSIC_MODELS))
        user_id = 42
        _pending_music["req"] = {"user_id": user_id, "prompt": "music", "chat_id": 1}
        _cooldowns.pop(user_id, None)
        callback = SimpleNamespace(
            data=f"musicsel:req:{choice}",
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(side_effect=RuntimeError("answer failed")),
            message=SimpleNamespace(),
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "answer failed"):
                asyncio.run(music_model_callback(cast(Any, callback)))
            self.assertIn("req", _pending_music)
            self.assertNotIn(user_id, _cooldowns)
        finally:
            _pending_music.pop("req", None)
            _cooldowns.pop(user_id, None)

    def test_generation_keyboards_offer_back_or_cancel(self):
        from handlers.common import _providers_keyboard, _temp_keyboard, _tts_cfg_keyboard
        from handlers.media_video import _video_model_keyboard
        from state import pending_tts_configs

        pending_tts_configs["req"] = {"cfg": {}}
        try:
            keyboards = [
                _providers_keyboard("req", 1),
                _temp_keyboard("req"),
                _tts_cfg_keyboard("req"),
                _video_model_keyboard("req"),
            ]
            callbacks = [
                {button.callback_data for row in keyboard.inline_keyboard for button in row}
                for keyboard in keyboards
            ]
            self.assertIn("imgcancel:req", callbacks[0])
            self.assertIn("imgback:req", callbacks[1])
            self.assertIn("imgcancel:req", callbacks[1])
            self.assertIn("ttsback:req", callbacks[2])
            self.assertIn("ttsabort:req", callbacks[2])
            self.assertIn("veocancel:req", callbacks[3])
        finally:
            pending_tts_configs.pop("req", None)

    def test_image_cancel_clears_every_pending_stage(self):
        from handlers.media_gen import _nsfw_awaiting_input, handle_image_cancel
        from state import pending_image_requests, pending_nsfw_configs, pending_prompt_requests

        request = {"user_id": 1, "chat_id": 2}
        pending_image_requests["req"] = request.copy()
        pending_prompt_requests["req"] = request.copy()
        pending_nsfw_configs["req"] = request.copy()
        _nsfw_awaiting_input[(2, 1)] = {"request_id": "req"}
        callback = SimpleNamespace(
            data="imgcancel:req",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        try:
            asyncio.run(handle_image_cancel(cast(Any, callback)))
            self.assertNotIn("req", pending_image_requests)
            self.assertNotIn("req", pending_prompt_requests)
            self.assertNotIn("req", pending_nsfw_configs)
            self.assertNotIn((2, 1), _nsfw_awaiting_input)
        finally:
            pending_image_requests.pop("req", None)
            pending_prompt_requests.pop("req", None)
            pending_nsfw_configs.pop("req", None)
            _nsfw_awaiting_input.pop((2, 1), None)

    def test_clear_denial_explains_recovery(self):
        from handlers.core import cmd_clear

        message = SimpleNamespace(
            bot=object(),
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=2),
            reply=AsyncMock(),
        )
        with patch("handlers.core.check_membership", new=AsyncMock(return_value=False)):
            asyncio.run(cmd_clear(cast(Any, message)))
        text = message.reply.await_args.args[0]
        self.assertIn("вступи", text.lower())
        self.assertIn("/clear", text)

    def test_dual_wrong_chat_explains_scope(self):
        from handlers.commands_misc import cmd_dual

        message = SimpleNamespace(chat=SimpleNamespace(id=0), reply=AsyncMock())
        asyncio.run(cmd_dual(cast(Any, message)))
        text = message.reply.await_args.args[0]
        self.assertIn("только", text.lower())
        self.assertIn("бесед", text.lower())

    def test_upscaler_cleanup_failure_does_not_report_send_failure(self):
        from handlers.commands_misc import cmd_up

        wait_message = SimpleNamespace(
            edit_text=AsyncMock(),
            delete=AsyncMock(side_effect=RuntimeError("delete failed")),
        )
        downloaded = SimpleNamespace(read=lambda: b"image")
        message = SimpleNamespace(
            bot=SimpleNamespace(download=AsyncMock(return_value=downloaded)),
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=2),
            photo=[SimpleNamespace(file_id="photo")],
            reply=AsyncMock(return_value=wait_message),
            reply_document=AsyncMock(return_value=object()),
        )
        with (
            patch("handlers.commands_misc.check_membership", new=AsyncMock(return_value=True)),
            patch("handlers.commands_misc.upscale_image", new=AsyncMock(return_value=(b"upscaled", None))),
        ):
            asyncio.run(cmd_up(cast(Any, message)))

        message.reply_document.assert_awaited_once()
        rendered = " ".join(call.args[0] for call in wait_message.edit_text.await_args_list)
        self.assertNotIn("не принял результат", rendered)

    def test_voice_delete_treats_remote_not_found_as_success(self):
        from services.elevenlabs_service import elevenlabs_delete_voice

        with patch(
            "services.elevenlabs_service._try_with_keys",
            new=AsyncMock(return_value=(None, None, "HTTP 404: voice_not_found")),
        ):
            self.assertTrue(asyncio.run(elevenlabs_delete_voice("voice-id")))

    def test_voice_delete_propagates_other_provider_errors(self):
        from services.elevenlabs_service import elevenlabs_delete_voice

        with patch(
            "services.elevenlabs_service._try_with_keys",
            new=AsyncMock(return_value=(None, None, "HTTP 429: rate_limited")),
        ):
            with self.assertRaisesRegex(RuntimeError, "rate_limited"):
                asyncio.run(elevenlabs_delete_voice("voice-id"))

    def test_elevenlabs_media_services_propagate_diagnostics(self):
        from services.elevenlabs_service import (
            elevenlabs_add_voice,
            elevenlabs_design_voice,
            elevenlabs_music,
            elevenlabs_sfx,
            elevenlabs_voice_isolator,
        )

        calls = [
            (elevenlabs_add_voice, ("name", b"audio")),
            (elevenlabs_design_voice, ("name", "prompt")),
            (elevenlabs_music, ("prompt",)),
            (elevenlabs_sfx, ("prompt",)),
            (elevenlabs_voice_isolator, (b"audio",)),
        ]
        for service, args in calls:
            with self.subTest(service=service.__name__):
                with patch(
                    "services.elevenlabs_service._try_with_keys",
                    new=AsyncMock(return_value=(None, None, "HTTP 429: rate_limited")),
                ):
                    with self.assertRaisesRegex(RuntimeError, "rate_limited"):
                        asyncio.run(service(*args))

    def test_voice_media_denial_stops_before_download(self):
        from handlers.chat import handle_voice_audio

        bot = SimpleNamespace(get_me=AsyncMock())
        message = SimpleNamespace(
            bot=bot,
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=2, type="group", is_forum=False),
            voice=SimpleNamespace(file_id="voice"),
            audio=None,
            video_note=None,
            reply_to_message=None,
            caption="",
            message_thread_id=None,
            reply=AsyncMock(),
        )
        with patch("handlers.chat.check_membership", new=AsyncMock(return_value=False)):
            asyncio.run(handle_voice_audio(cast(Any, message)))

        bot.get_me.assert_not_awaited()

    def test_auto_voice_failure_restores_text_reply(self):
        import handlers.chat as chat_handler

        message = SimpleNamespace(
            bot=object(),
            chat=SimpleNamespace(id=7),
            message_id=8,
        )
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        rich_send = AsyncMock(return_value=object())
        sonnet = AsyncMock(return_value="Tagged Sonnet answer")
        deepseek = AsyncMock(return_value={})

        async def scenario():
            with (
                patch.object(chat_handler, "generate_text_with_openrouter", sonnet, create=True),
                patch("services.deepseek_service.deepseek_chat", new=deepseek),
                patch("database.voice.get_voices", new=AsyncMock(return_value=[])),
                patch("handlers.chat.send_rich_message", new=rich_send),
            ):
                await chat_handler._voice_reply(cast(Any, message), "Полный ответ", cast(Any, status), {})

        asyncio.run(scenario())
        self.assertEqual(rich_send.await_args.kwargs["text"], "Полный ответ")
        self.assertEqual(sonnet.await_args.kwargs["model"], "anthropic/claude-sonnet-5")
        deepseek.assert_not_awaited()
        status.delete.assert_awaited_once()

    def test_video_download_failure_does_not_fake_text_analysis(self):
        from handlers.chat import handle_video

        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=99, username="bot")),
            download=AsyncMock(side_effect=RuntimeError("download failed")),
        )
        message = SimpleNamespace(
            bot=bot,
            chat=SimpleNamespace(id=7, type="private", is_forum=False),
            from_user=SimpleNamespace(id=1, first_name="User", username=None),
            reply_to_message=None,
            caption="Что в видео?",
            content_type="video",
            video=SimpleNamespace(file_size=100, file_id="file"),
            animation=None,
            document=None,
            message_thread_id=None,
            reply=AsyncMock(),
        )
        text_model = AsyncMock(return_value="fake analysis")
        with (
            patch("handlers.chat.check_membership", new=AsyncMock(return_value=True)),
            patch("handlers.chat.generate_text_with_gemini", new=text_model),
        ):
            asyncio.run(handle_video(cast(Any, message)))

        text_model.assert_not_awaited()
        self.assertIn("download failed", message.reply.await_args.args[0])

    def test_tts_back_keeps_config_when_message_edit_fails(self):
        from handlers.media_tts import handle_tts_back
        from state import pending_tts_configs, pending_tts_requests

        config = {"user_id": 1, "chat_id": 2, "prompt": "text", "cfg": {}, "model": "m", "label": "M"}
        pending_tts_configs["req"] = config
        callback = SimpleNamespace(
            data="ttsback:req",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(side_effect=RuntimeError("edit failed"))),
        )
        try:
            asyncio.run(handle_tts_back(cast(Any, callback)))
            self.assertIs(pending_tts_configs.get("req"), config)
            self.assertNotIn("req", pending_tts_requests)
            self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        finally:
            pending_tts_configs.pop("req", None)
            pending_tts_requests.pop("req", None)

    def test_nsfw_back_keeps_config_when_message_edit_fails(self):
        from handlers.media_gen import handle_nsfw_back
        from state import pending_image_requests, pending_nsfw_configs

        request = {"user_id": 1, "chat_id": 2, "images_bytes": []}
        config = {"user_id": 1, "chat_id": 2, "request_data": request}
        pending_nsfw_configs["req"] = config
        callback = SimpleNamespace(
            data="nsfwback:req",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(side_effect=RuntimeError("edit failed"))),
        )
        try:
            asyncio.run(handle_nsfw_back(cast(Any, callback)))
            self.assertIs(pending_nsfw_configs.get("req"), config)
            self.assertNotIn("req", pending_image_requests)
            self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        finally:
            pending_nsfw_configs.pop("req", None)
            pending_image_requests.pop("req", None)

    def test_voice_music_cleans_state_when_result_send_fails(self):
        from handlers.voice import _voice_active_users, process_music

        user_id = 42
        _voice_active_users.add(user_id)
        state = AsyncMock()
        message = SimpleNamespace(
            text="music prompt",
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(is_forum=False),
            reply=AsyncMock(),
            reply_audio=AsyncMock(side_effect=RuntimeError("send failed")),
        )
        with patch("handlers.voice.elevenlabs_music", new=AsyncMock(return_value=b"audio")):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                asyncio.run(process_music(cast(Any, message), state))

        self.assertNotIn(user_id, _voice_active_users)
        state.clear.assert_awaited_once()

    def test_add_voice_propagates_database_failure(self):
        from database.voice import add_voice

        failing_db = AsyncMock()
        failing_db.__aenter__.side_effect = RuntimeError("db down")
        with patch("database.voice.get_db", return_value=failing_db):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                asyncio.run(add_voice(1, "name", "voice", "cloned"))

    def test_get_voice_settings_propagates_database_failure(self):
        from database.voice import get_settings

        failing_db = AsyncMock()
        failing_db.__aenter__.side_effect = RuntimeError("db down")
        with patch("database.voice.get_db", return_value=failing_db):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                asyncio.run(get_settings(1))

    def test_save_voice_settings_propagates_database_failure(self):
        from database.voice import save_settings

        failing_db = AsyncMock()
        failing_db.__aenter__.side_effect = RuntimeError("db down")
        current = {"tts_model": "eleven_v3", "stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0}
        with (
            patch("database.voice.get_settings", new=AsyncMock(return_value=current)),
            patch("database.voice.get_db", return_value=failing_db),
        ):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                asyncio.run(save_settings(1, speed=1.1))

    def test_voice_design_rolls_back_remote_voice_when_database_fails(self):
        from handlers.voice import _voice_active_users, process_voice_design

        user_id = 42
        _voice_active_users.add(user_id)
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            text="calm voice",
            from_user=SimpleNamespace(id=user_id),
            reply=AsyncMock(return_value=status),
        )
        state = AsyncMock()
        delete_remote = AsyncMock(return_value=True)
        with (
            patch("handlers.voice.elevenlabs_design_voice", new=AsyncMock(return_value="voice-id")),
            patch("handlers.voice.add_voice", new=AsyncMock(side_effect=RuntimeError("db down"))),
            patch("handlers.voice.elevenlabs_delete_voice", new=delete_remote),
        ):
            asyncio.run(process_voice_design(cast(Any, message), state))

        delete_remote.assert_awaited_once_with("voice-id")
        self.assertIn("db down", status.edit_text.await_args.args[0])
        self.assertNotIn(user_id, _voice_active_users)
        state.clear.assert_awaited_once()

    def test_main_text_uses_claude_sonnet_5_via_openrouter(self):
        import services.gemini_text as text_service

        sonnet = AsyncMock(return_value="sonnet reply")
        groq = AsyncMock(return_value="groq reply")
        deepseek = AsyncMock(return_value="deepseek reply")
        with (
            patch.object(text_service, "generate_text_with_openrouter", sonnet, create=True),
            patch.object(text_service, "deepseek_text", deepseek, create=True),
            patch("services.groq_service.generate_text_with_groq", new=groq),
            patch("services.gemini_text.get_history", new=AsyncMock(return_value=[])),
            patch("services.gemini_text.save_history", new=AsyncMock()),
        ):
            result = asyncio.run(text_service.generate_text_with_gemini(
                "обычный вопрос",
                987654,
                allow_web=False,
            ))

        self.assertEqual(result, "sonnet reply")
        self.assertEqual(sonnet.await_args.kwargs["model"], "anthropic/claude-sonnet-5")
        groq.assert_not_awaited()
        deepseek.assert_not_awaited()

    def test_main_text_uses_groq_only_after_sonnet_failure(self):
        import services.gemini_text as text_service

        sonnet = AsyncMock(side_effect=RuntimeError("openrouter down"))
        groq = AsyncMock(return_value="groq fallback")
        with (
            patch.object(text_service, "generate_text_with_openrouter", sonnet),
            patch("services.groq_service.generate_text_with_groq", new=groq),
            patch("services.gemini_text.get_history", new=AsyncMock(return_value=[])),
            patch("services.gemini_text.save_history", new=AsyncMock()),
        ):
            result = asyncio.run(text_service.generate_text_with_gemini(
                "обычный вопрос",
                987656,
                allow_web=False,
            ))

        self.assertEqual(result, "groq fallback")
        sonnet.assert_awaited_once()
        groq.assert_awaited_once()

    def test_explicit_web_answer_is_synthesized_by_sonnet(self):
        import services.gemini_text as text_service

        sonnet = AsyncMock(return_value="sonnet web answer")
        old_synthesis = AsyncMock(return_value="deepseek web answer")
        web_context = "Источник: официальный сайт\n" + ("данные " * 20)
        with (
            patch.object(text_service, "generate_text_with_openrouter", sonnet),
            patch.object(text_service, "synthesize_web_answer", old_synthesis, create=True),
            patch("services.gemini_text._extract_search_query", new=AsyncMock(return_value="новости")),
            patch("services.gemini_text.search_web_with_firecrawl", new=AsyncMock(return_value=(web_context, True))),
            patch("services.gemini_text.get_history", new=AsyncMock(return_value=[])),
            patch("services.gemini_text.save_history", new=AsyncMock()),
        ):
            result = asyncio.run(text_service.generate_text_with_gemini(
                "найди свежие новости",
                987657,
            ))

        self.assertEqual(result, "sonnet web answer")
        sonnet.assert_awaited_once()
        old_synthesis.assert_not_awaited()

    def test_explicit_web_empty_result_is_reported_without_model_call(self):
        import services.gemini_text as text_service

        sonnet = AsyncMock(return_value="should not be used")
        with (
            patch.object(text_service, "generate_text_with_openrouter", sonnet),
            patch("services.gemini_text._extract_search_query", new=AsyncMock(return_value="ничего")),
            patch("services.gemini_text.search_web_with_firecrawl", new=AsyncMock(return_value=("", True))),
            patch("services.gemini_text.get_history", new=AsyncMock(return_value=[])),
            patch("services.gemini_text.save_history", new=AsyncMock()),
        ):
            result = asyncio.run(text_service.generate_text_with_gemini(
                "найди ничего",
                987658,
            ))

        self.assertIn("поиск вернул пустоту", result)
        sonnet.assert_not_awaited()

    def test_openrouter_text_sends_exact_sonnet_model(self):
        from services import openrouter

        captured = {}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def json(self):
                return {"choices": [{"message": {"content": "sonnet reply"}}]}

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            def post(self, url, **kwargs):
                captured.update(url=url, **kwargs)
                return Response()

        with (
            patch("services.openrouter.load_openrouter_keys", new=AsyncMock(return_value=["test-key"])),
            patch("services.openrouter.aiohttp.ClientSession", return_value=Session()),
        ):
            result = asyncio.run(openrouter.generate_text_with_openrouter(
                "question",
                system_prompt="system",
                max_tokens=321,
            ))

        self.assertEqual(result, "sonnet reply")
        self.assertEqual(openrouter.OPENROUTER_TEXT_MODEL, "anthropic/claude-sonnet-5")
        self.assertEqual(captured["json"]["model"], "anthropic/claude-sonnet-5")
        self.assertEqual(captured["json"]["messages"], [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "question"},
        ])
        self.assertEqual(captured["json"]["max_tokens"], 321)
        self.assertEqual(captured["json"]["reasoning"], {"effort": "none"})

    def test_openrouter_text_cools_down_policy_blocked_key(self):
        from services import openrouter

        class Response:
            def __init__(self, status, payload):
                self.status = status
                self.payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def json(self):
                return self.payload

            async def text(self):
                return repr(self.payload)

        class Session:
            keys = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            def post(self, *_args, **kwargs):
                key = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
                self.keys.append(key)
                if key == "blocked":
                    return Response(404, {"error": {"message": "guardrail restrictions"}})
                return Response(200, {"choices": [{"message": {"content": "sonnet reply"}}]})

        session = Session()
        openrouter._TEXT_POLICY_DEAD.clear()
        try:
            with (
                patch("services.openrouter.load_openrouter_keys", new=AsyncMock(return_value=["blocked", "working"])),
                patch("services.openrouter.aiohttp.ClientSession", return_value=session),
            ):
                first = asyncio.run(openrouter.generate_text_with_openrouter("question"))
                second = asyncio.run(openrouter.generate_text_with_openrouter("question"))
        finally:
            openrouter._TEXT_POLICY_DEAD.clear()

        self.assertEqual((first, second), ("sonnet reply", "sonnet reply"))
        self.assertEqual(session.keys, ["blocked", "working", "working"])

    def test_openrouter_text_rejects_missing_keys(self):
        from services.openrouter import generate_text_with_openrouter

        with patch("services.openrouter.load_openrouter_keys", new=AsyncMock(return_value=[])):
            with self.assertRaisesRegex(RuntimeError, "нет доступных API-ключей"):
                asyncio.run(generate_text_with_openrouter("question"))

    def test_main_text_surfaces_sonnet_provider_error(self):
        import services.gemini_text as text_service

        with (
            patch.object(
                text_service,
                "generate_text_with_openrouter",
                new=AsyncMock(side_effect=RuntimeError("credits exhausted")),
            ),
            patch("services.gemini_text.get_history", new=AsyncMock(return_value=[])),
            patch("services.gemini_text.save_history", new=AsyncMock()),
        ):
            result = asyncio.run(text_service.generate_text_with_gemini(
                "обычный вопрос",
                987655,
                allow_web=False,
                is_owner=True,
            ))

        self.assertIn("Claude Sonnet 5", result)
        self.assertIn("credits exhausted", result)

    def test_primary_agent_uses_sonnet_tool_call_round_trip(self):
        import agent.loop as agent_loop

        tool_response = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "think", "arguments": '{"thought":"plan"}'},
                    }],
                },
            }],
        }
        final_response = {
            "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
        }
        sonnet = AsyncMock(side_effect=[tool_response, final_response])
        deepseek = AsyncMock(return_value=None)
        contents = [{"role": "user", "parts": [{"text": "do work"}]}]
        with (
            patch.object(agent_loop, "openrouter_chat", sonnet, create=True),
            patch.object(agent_loop, "deepseek_chat", deepseek, create=True),
        ):
            first = asyncio.run(agent_loop._sonnet_call(contents, is_owner=False))
            contents.extend([
                {"role": "model", "parts": first["content"]["parts"]},
                {"role": "user", "parts": [{
                    "functionResponse": {"name": "think", "response": {"result": "ok"}},
                }]},
            ])
            second = asyncio.run(agent_loop._sonnet_call(contents, is_owner=False))

        function_call = first["content"]["parts"][0]["functionCall"]
        self.assertEqual(function_call, {"name": "think", "args": {"thought": "plan"}})
        self.assertEqual(second["content"]["parts"][0]["text"], "done")
        sent_messages = sonnet.await_args_list[1].kwargs["messages"]
        assistant_message = next(message for message in sent_messages if message["role"] == "assistant")
        tool_message = next(message for message in sent_messages if message["role"] == "tool")
        tool_call = assistant_message["tool_calls"][0]
        self.assertEqual(__import__("json").loads(tool_call["function"]["arguments"]), {"thought": "plan"})
        self.assertEqual(tool_message["tool_call_id"], tool_call["id"])
        self.assertEqual(sonnet.await_args.kwargs["model"], "anthropic/claude-sonnet-5")
        self.assertTrue(sonnet.await_args.kwargs["tools"])
        deepseek.assert_not_awaited()

    def test_run_agent_does_not_require_gemini_keys(self):
        import agent.loop as agent_loop

        workspace = SimpleNamespace(host_path="/tmp/agent-test", preload=lambda *_: None, cleanup=lambda: None)

        async def status(_text):
            return None

        with (
            patch.object(agent_loop, "AgentWorkspace", return_value=workspace),
            patch.object(agent_loop, "_sonnet_call", new=AsyncMock(return_value={
                "content": {"role": "model", "parts": [{"text": "sonnet agent reply"}]},
                "_finish": "stop",
            })),
            patch.object(agent_loop, "load_keys", new=AsyncMock(return_value=[]), create=True),
        ):
            result, project = asyncio.run(agent_loop.run_agent(
                "обычный вопрос",
                998877,
                "User",
                status,
            ))

        self.assertEqual(result, "sonnet agent reply")
        self.assertIsNone(project)

    def test_agent_intent_classifier_never_calls_deepseek(self):
        import agent.loop as agent_loop

        sonnet = AsyncMock(return_value="false")
        deepseek = AsyncMock(return_value=None)
        with (
            patch.object(agent_loop, "generate_text_with_openrouter", sonnet, create=True),
            patch.object(agent_loop, "deepseek_chat", deepseek, create=True),
        ):
            result = asyncio.run(agent_loop.classify_agent_intent("как твои дела"))

        self.assertFalse(result)
        self.assertEqual(sonnet.await_args.kwargs["model"], "anthropic/claude-sonnet-5")
        deepseek.assert_not_awaited()


    def test_regular_chat_prompt_is_structured(self):
        from config import SYSTEM_PROMPT

        self.assertIn("СТИЛЬ И ХАРАКТЕР:", SYSTEM_PROMPT)
        self.assertIn("ФОРМАТ ОТВЕТА:", SYSTEM_PROMPT)
        self.assertIn("ПОЛЕЗНОСТЬ:", SYSTEM_PROMPT)
        self.assertNotIn("абсолютно аморальная", SYSTEM_PROMPT)

    def test_agent_prompt_routes_tasks_without_exposing_reasoning(self):
        from agent.prompts import _SYSTEM, _build_system

        effective_prompt = _build_system(is_owner=False)
        for prompt in (_SYSTEM, effective_prompt):
            self.assertIn("МАРШРУТИЗАЦИЯ:", prompt)
            self.assertIn("Не раскрывай внутренние рассуждения", prompt)
            self.assertIn("РЕЗУЛЬТАТ И ОШИБКИ:", prompt)
            self.assertNotIn("Перед каждой командой/инструментом вызывай think", prompt)
            self.assertNotIn("CRITICAL SYSTEM OVERRIDE", prompt)

    def test_think_tool_shows_neutral_status_only(self):
        from agent.loop import _execute_tool
        from agent.safety import _DebounceHook, _ToolBudget

        statuses = []

        async def status(text):
            statuses.append(text)

        result = asyncio.run(_execute_tool(
            "think",
            {"thought": "SECRET_REASONING"},
            _DebounceHook(),
            _ToolBudget(),
            status,
            None,
            cast(Any, None),
        ))

        self.assertEqual(result, ("ok", None))
        self.assertEqual(statuses, ["Планирую следующий шаг..."])
        self.assertNotIn("SECRET_REASONING", "".join(statuses))


    def test_agent_web_search_status_uses_news_emoji_and_query(self):
        import agent.loop as agent_loop

        statuses = []
        workspace = SimpleNamespace(host_path="/tmp/agent-status-test", preload=lambda *_: None, cleanup=lambda: None)
        sonnet = AsyncMock(side_effect=[
            {
                "content": {"role": "model", "parts": [{"functionCall": {
                    "name": "web_search",
                    "args": {"query": "главные новости нейросетей <b>июль</b> 2026"},
                }}]},
                "_finish": "tool_calls",
            },
            {
                "content": {"role": "model", "parts": [{"text": "готово"}]},
                "_finish": "stop",
            },
        ])

        async def status(text):
            statuses.append(text)

        with (
            patch.object(agent_loop, "AgentWorkspace", return_value=workspace),
            patch.object(agent_loop, "_sonnet_call", sonnet),
            patch.object(agent_loop, "_fc_search", new=AsyncMock(return_value="результаты")),
        ):
            result, project = asyncio.run(agent_loop.run_agent(
                "чекни, что у ИИ нового",
                112233,
                "User",
                status,
            ))

        rich_status = next((text for text in statuses if "Веб-поиск" in text), "")
        self.assertEqual((result, project), ("готово", None))
        self.assertIn('<tg-emoji emoji-id="5231012545799666522">🔍</tg-emoji>', rich_status)
        self.assertIn("<b>Веб-поиск</b>", rich_status)
        self.assertIn("<i>главные новости нейросетей &lt;b&gt;июль&lt;/b&gt; 2026</i>", rich_status)
        self.assertNotIn("<b>июль</b>", "\n".join(statuses))
        self.assertIn("Работаю", rich_status)
        self.assertNotIn("Шаг 1/120", rich_status)

    def test_openrouter_vision_sends_image_to_configured_sonnet(self):
        from services import openrouter

        chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "На фото кот"}}],
        })
        with patch.object(openrouter, "openrouter_chat", chat):
            result = asyncio.run(openrouter.analyze_image_with_openrouter(
                b"\xff\xd8\xffimage",
                "Что на фото?",
            ))

        self.assertEqual(result, "На фото кот")
        payload = chat.await_args.kwargs
        self.assertEqual(payload["model"], "anthropic/claude-sonnet-5")
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Что на фото?"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_media_image_analysis_routes_to_sonnet(self):
        import handlers.media_in as media_in

        message = SimpleNamespace(
            chat=SimpleNamespace(id=876543),
            from_user=SimpleNamespace(id=1, first_name="User", username=None),
            reply=AsyncMock(),
        )
        sonnet = AsyncMock(return_value="На фото кот")
        nvidia = AsyncMock(return_value="Старый ответ")
        with (
            patch.object(media_in, "classify_agent_intent", new=AsyncMock(return_value=True)),
            patch.object(media_in, "_analyze_photo_with_status", sonnet, create=True),
            patch("services.nvidia_vision.analyze_image", new=nvidia),
            patch.object(media_in, "add_user_stat", new=AsyncMock()),
            patch.object(media_in, "log_prompt", new=AsyncMock()),
        ):
            handled = asyncio.run(media_in._media_to_agent(
                cast(Any, message),
                b"image",
                "photo.jpg",
                "Что на фото?",
                {},
            ))

        self.assertTrue(handled)
        sonnet.assert_awaited_once()
        nvidia.assert_not_awaited()

    def test_direct_photo_analysis_routes_to_sonnet(self):
        import handlers.chat as chat_handler

        downloaded = SimpleNamespace(read=lambda: b"image")
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=99, username="bot")),
            get_file=AsyncMock(return_value=SimpleNamespace(file_path="photo.jpg")),
            download_file=AsyncMock(return_value=downloaded),
        )
        message = SimpleNamespace(
            bot=bot,
            chat=SimpleNamespace(id=765432, type="private", is_forum=False),
            from_user=SimpleNamespace(id=1, first_name="User", username=None),
            reply_to_message=None,
            caption="Что на фото?",
            media_group_id=None,
            message_thread_id=None,
            photo=[SimpleNamespace(file_id="photo")],
            reply=AsyncMock(),
        )
        sonnet = AsyncMock(return_value="На фото кот")
        nvidia = AsyncMock(return_value="Старый ответ")
        with (
            patch.object(chat_handler, "check_membership", new=AsyncMock(return_value=True)),
            patch.object(chat_handler, "_media_to_agent", new=AsyncMock(return_value=False)),
            patch.object(chat_handler, "_analyze_photo_with_status", sonnet, create=True),
            patch("services.nvidia_vision.analyze_image", new=nvidia),
            patch.object(chat_handler, "add_user_stat", new=AsyncMock()),
            patch.object(chat_handler, "log_prompt", new=AsyncMock()),
        ):
            asyncio.run(chat_handler.handle_album_photo(cast(Any, message)))

        sonnet.assert_awaited_once()
        nvidia.assert_not_awaited()
        self.assertEqual(message.reply.await_args_list[-1].args[0], "На фото кот")

    def test_direct_photo_failure_does_not_fake_text_analysis(self):
        import handlers.chat as chat_handler

        downloaded = SimpleNamespace(read=lambda: b"image")
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=99, username="bot")),
            get_file=AsyncMock(return_value=SimpleNamespace(file_path="photo.jpg")),
            download_file=AsyncMock(return_value=downloaded),
        )
        message = SimpleNamespace(
            bot=bot,
            chat=SimpleNamespace(id=765433, type="private", is_forum=False),
            from_user=SimpleNamespace(id=1, first_name="User", username=None),
            reply_to_message=None,
            caption="Что на фото?",
            media_group_id=None,
            message_thread_id=None,
            photo=[SimpleNamespace(file_id="photo")],
            reply=AsyncMock(),
        )
        text_only = AsyncMock(return_value="Выдуманный ответ")
        with (
            patch.object(chat_handler, "check_membership", new=AsyncMock(return_value=True)),
            patch.object(chat_handler, "_media_to_agent", new=AsyncMock(return_value=False)),
            patch.object(
                chat_handler,
                "_analyze_photo_with_status",
                new=AsyncMock(side_effect=RuntimeError("vision down")),
                create=True,
            ),
            patch("services.nvidia_vision.analyze_image", new=AsyncMock(side_effect=RuntimeError("vision down"))),
            patch.object(chat_handler, "generate_text_with_gemini", text_only),
        ):
            asyncio.run(chat_handler.handle_album_photo(cast(Any, message)))

        text_only.assert_not_awaited()
        failure = message.reply.await_args_list[-1].args[0]
        self.assertIn("RuntimeError", failure)
        self.assertIn("vision down", failure)

    def test_photo_analysis_status_uses_premium_eyes_and_cleans_up(self):
        import handlers.media_in as media_in

        status = SimpleNamespace(delete=AsyncMock())
        message = SimpleNamespace(reply=AsyncMock(return_value=status))
        vision = AsyncMock(return_value="На фото кот")
        with patch.object(media_in, "analyze_image_with_openrouter", vision, create=True):
            result = asyncio.run(media_in._analyze_photo_with_status(
                cast(Any, message),
                b"image",
                "Что на фото?",
                {},
            ))

        self.assertEqual(result, "На фото кот")
        rendered = message.reply.await_args.args[0]
        self.assertIn('<tg-emoji emoji-id="5210956306952758910">👀</tg-emoji>', rendered)
        self.assertEqual(message.reply.await_args.kwargs["parse_mode"], "HTML")
        self.assertIn("<b>Анализирую фото</b>", rendered)
        self.assertIn("<i>Claude Sonnet 5</i>", rendered)
        self.assertIn('<tg-emoji emoji-id="5386367538735104399">⌛</tg-emoji>', rendered)
        status.delete.assert_awaited_once()

    def test_photo_analysis_status_is_deleted_when_sonnet_fails(self):
        import handlers.media_in as media_in

        status = SimpleNamespace(delete=AsyncMock())
        message = SimpleNamespace(reply=AsyncMock(return_value=status))
        vision = AsyncMock(side_effect=RuntimeError("vision down"))
        with (
            patch.object(media_in, "analyze_image_with_openrouter", vision, create=True),
            self.assertRaisesRegex(RuntimeError, "vision down"),
        ):
            asyncio.run(media_in._analyze_photo_with_status(
                cast(Any, message),
                b"image",
                "Что на фото?",
                {},
            ))

        status.delete.assert_awaited_once()

    def test_gemini_post_retries_on_api_key_invalid_400(self):
        from shared_types import gemini_post

        valid = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

        call_count = 0

        class FakeResponse:
            def __init__(self, status, body):
                self.status = status
                self._body = body

            async def text(self):
                return self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def json(self):
                return self._body

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            def post(self, url, **kwargs):
                nonlocal call_count
                call_count += 1
                body = (
                    '{"error":{"details":[{"reason":"API_KEY_INVALID"}]}}'
                    if call_count == 1
                    else valid
                )
                return FakeResponse(400 if call_count == 1 else 200, body)

        with (
            patch("keys.load_keys", new=AsyncMock(return_value=["bad-key", "good-key"])),
            patch("keys.remove_key", new=lambda *_: None),
            patch("aiohttp.ClientSession", return_value=FakeSession()),
        ):
            data, key, err = asyncio.run(gemini_post("models/test:generateContent", {}))

        self.assertIsNotNone(data)
        self.assertEqual(key, "good-key")
        self.assertIsNone(err)
        self.assertEqual(call_count, 2)

    def test_project_json_rejects_non_object_root(self):
        from services.code_service import _extract_project_json

        for raw in ("[]", "null", '"text"'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "object"):
                _extract_project_json(raw)

    def test_mvsep_missing_token_fails_before_network(self):
        from services import mvsep_service

        with (
            patch.object(mvsep_service, "MVSEP_API_TOKEN", ""),
            patch.object(mvsep_service.aiohttp, "ClientSession", side_effect=AssertionError("network called")) as session,
        ):
            result, error = asyncio.run(mvsep_service.mvsep_separate(b"audio"))

        self.assertIsNone(result)
        self.assertIn("MVSEP_API_TOKEN", error)
        session.assert_not_called()

    def test_upscaler_missing_client_id_fails_before_network(self):
        from services import upscale_service

        with (
            patch.object(upscale_service, "UPSCALE_CLIENT_ID", ""),
            patch.object(upscale_service.aiohttp, "ClientSession", side_effect=AssertionError("network called")) as session,
        ):
            result, error = asyncio.run(upscale_service.upscale_image(b"image"))

        self.assertIsNone(result)
        self.assertIn("UPSCALE_CLIENT_ID", error)
        session.assert_not_called()

    def test_project_generation_retries_malformed_model_json(self):
        import services.code_service as code_service

        malformed = {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"files":[{"path":"app.py","content":"print(1)"}]'}]}}]
        }
        valid = {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": (
                '{"project_name":"demo","summary":"Готово","run_instructions":"python app.py",'
                '"files":[{"path":"app.py","content":"print(1)"}]}'
            )}]}}]
        }
        post = AsyncMock(side_effect=[(malformed, "key-1", None), (valid, "key-2", None)])

        with (
            patch.object(code_service, "load_keys", new=AsyncMock(return_value=["key"])),
            patch.object(code_service, "gemini_post", post),
        ):
            project = asyncio.run(code_service.generate_project_with_gemini("сделай app"))

        self.assertTrue(project["ok"])
        self.assertEqual(project["project_name"], "demo")
        self.assertEqual(post.await_count, 2)

    def test_project_generation_returns_real_json_error_after_all_models_fail(self):
        import services.code_service as code_service

        malformed = {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"files":['}]}}]
        }
        post = AsyncMock(return_value=(malformed, "key", None))

        with (
            patch.object(code_service, "load_keys", new=AsyncMock(return_value=["key"])),
            patch.object(code_service, "gemini_post", post),
        ):
            project = asyncio.run(code_service.generate_project_with_gemini("сделай app"))

        self.assertFalse(project["ok"])
        self.assertIn("JSONDecodeError", project["error"])
        self.assertIn("Expecting", project["error"])
        self.assertEqual(post.await_count, 3)

if __name__ == "__main__":
    unittest.main()
