"""
/voice command — ElevenLabs TTS, voice cloning, music, SFX, isolator, STT.
"""
import asyncio
import logging
import os as _os
import subprocess
import tempfile
import time
import uuid
from html import escape as escape_html


from aiogram import F, Router, types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from services.elevenlabs_service import (
    elevenlabs_tts, elevenlabs_add_voice, elevenlabs_delete_voice,
    elevenlabs_get_voice, elevenlabs_music,
    elevenlabs_sfx, elevenlabs_voice_isolator, elevenlabs_stt,
    elevenlabs_design_voice, elevenlabs_voice_changer,
)
from services.mvsep_service import mvsep_separate, mvsep_download
from database.voice import (
    add_voice, get_voices, get_voice_by_id, delete_voice,
    get_settings, save_settings,
)
from utils import check_membership
from handlers.common import _track_user, safe_send

logger = logging.getLogger(__name__)
voice_router = Router()

class VoiceState(StatesGroup):
    waiting_for_audio = State()
    waiting_for_name = State()
    waiting_for_tts_text = State()
    waiting_for_music_prompt = State()
    waiting_for_sfx_prompt = State()
    waiting_for_voice_design_prompt = State()
    waiting_for_isolator_audio = State()
    waiting_for_stt_audio = State()
    waiting_for_change_audio = State()
    waiting_for_change_voice = State()
    waiting_for_changer_audio = State()
    waiting_for_changer_voice = State()

_AUDIO_FOR_CLONE: dict[int, bytes] = {}
_voice_active_users: set[int] = set()

async def _clear_voice_state(user_id: int, state: FSMContext) -> None:
    _voice_active_users.discard(user_id)
    _AUDIO_FOR_CLONE.pop(user_id, None)
    _CHANGER_AUDIO.pop(user_id, None)
    _CHANGE_AUDIO.pop(user_id, None)
    await state.clear()

async def _persist_remote_voice(user_id: int, name: str, voice_id: str, tier: str, duration_sec: float) -> None:
    try:
        await add_voice(user_id, name, voice_id, tier, duration_sec)
    except Exception as database_error:
        try:
            await elevenlabs_delete_voice(voice_id)
        except Exception as rollback_error:
            raise RuntimeError(
                f"Local voice save failed: {database_error}; remote rollback failed: {rollback_error}"
            ) from database_error
        raise RuntimeError(f"Local voice save failed: {database_error}; remote voice was removed") from database_error

async def _show_voice_failure(callback: types.CallbackQuery, operation: str, error: Exception) -> None:
    logger.error(f"{operation} failed: {type(error).__name__}: {error}", exc_info=True)
    await safe_send(
        callback.message.edit_text,
        f"{operation}: {type(error).__name__}: {escape_html(str(error))}",
        reply_markup=_main_menu_keyboard(),
        parse_mode="HTML",
    )

async def _enter_voice_flow(callback: types.CallbackQuery, state: FSMContext, user_id: int, next_state: State, text: str) -> None:
    try:
        _voice_active_users.add(user_id)
        await state.set_state(next_state)
        await callback.message.edit_text(text, parse_mode="HTML")
    except Exception as e:
        try:
            await _clear_voice_state(user_id, state)
        except Exception as cleanup_error:
            logger.error(f"Voice flow rollback failed: {type(cleanup_error).__name__}: {cleanup_error}", exc_info=True)
        logger.error(f"Voice flow entry failed: {type(e).__name__}: {e}", exc_info=True)
        await safe_send(
            callback.bot.send_message,
            chat_id=callback.message.chat.id,
            text=f"Не удалось открыть Voice Lab: {type(e).__name__}: {e}. Повтори /voice.",
        )

def is_voice_active(user_id: int) -> bool:
    return user_id in _voice_active_users

def _reply_kwargs(message: Message) -> dict:
    kw = {}
    if message.chat.is_forum and message.message_thread_id:
        kw["message_thread_id"] = message.message_thread_id
    return kw

VOICE_MENU_TEXT = (
    "<b>Голос и аудио</b>\n"
    "Выбирай операцию. Формат и следующий шаг написаны на каждом экране — читай, а не тыкай вслепую."
)


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Озвучить текст", callback_data="voice:tts")],
        [InlineKeyboardButton(text="Мои голоса", callback_data="voice:my")],
        [InlineKeyboardButton(text="Очистить шум", callback_data="voice:iso")],
        [InlineKeyboardButton(text="Voice Changer: песня", callback_data="voice:changer")],
        [InlineKeyboardButton(text="Сменить голос: речь", callback_data="voice:change")],
        [InlineKeyboardButton(text="Распознать речь", callback_data="voice:stt")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="voice:settings")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="menu:home"), InlineKeyboardButton(text="✕ Закрыть", callback_data="menu:close")],
    ])

def _settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    ml = {"eleven_v3": "V3", "eleven_flash_v2_5": "Flash", "eleven_multilingual_v2": "Multi"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Модель: {ml.get(settings['tts_model'], settings['tts_model'])}", callback_data="voice:set_model")],
        [InlineKeyboardButton(text=f"Стабильность: {int(settings['stability']*100)}%", callback_data="voice:set_stability")],
        [InlineKeyboardButton(text=f"Похожесть: {int(settings['similarity_boost']*100)}%", callback_data="voice:set_similarity")],
        [InlineKeyboardButton(text=f"Стиль: {int(settings['style']*100)}%", callback_data="voice:set_style")],
        [InlineKeyboardButton(text=f"Скорость: {settings['speed']}x", callback_data="voice:set_speed")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="voice:main")],
    ])

_DEFAULT_VOICES = (
    ("Rachel", "21m00Tcm4TlvDq8ikWAM"),
    ("Domi", "AZnzlk1XvdvUeBnXmlld"),
    ("Bella", "EXAVITQu4vr4xnSDxMaL"),
    ("Antoni", "ErXwobaYiN019PkySvjV"),
    ("Elli", "MF3mGyEYCl7XYWbV9V6O"),
    ("Josh", "TxGEqnHWrfWFTfGW9XjX"),
    ("Arnold", "VR6AewLTigWG4xSOukaG"),
    ("Adam", "pNInz6obpgDQGcFmaJgB"),
    ("Sam", "yoZ06aMxZJJ28mfd3POQ"),
)
_DEFAULT_VOICE_IDS = frozenset(voice_id for _, voice_id in _DEFAULT_VOICES)
_VOICE_CHANGER_DEFAULTS = tuple(voice for voice in _DEFAULT_VOICES if voice[0] in {"Rachel", "Bella", "Adam"})


async def _can_use_voice(user_id: int, voice_id: str) -> bool:
    return voice_id in _DEFAULT_VOICE_IDS or await get_voice_by_id(user_id, voice_id) is not None

def _tts_voice_keyboard(user_voices: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for v in user_voices[:20]:
        buttons.append([InlineKeyboardButton(text=f"🎙 {v['name']} ({v['tier']})", callback_data=f"voice:tts_sel:{v['voice_id']}")])
    for name, voice_id in _DEFAULT_VOICES:
        buttons.append([InlineKeyboardButton(text=f"🔊 {name}", callback_data=f"voice:tts_sel:{voice_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="voice:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def _voice_info_keyboard(voice_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎧 Прослушать", callback_data=f"voice:preview:{voice_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"voice:delete_ask:{voice_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="voice:my")],
    ])



async def _download_tg_file(bot, file_id) -> bytes:
    downloaded = await bot.download(file_id)
    return downloaded.read()
# ── /voice command ──────────────────────────────────────────────────────────

_voice_auto_chats: set[int] = set()


def is_voice_auto(chat_id: int) -> bool:
    return chat_id in _voice_auto_chats


@voice_router.message(Command("voice_on"))
async def cmd_voice_on(message: Message):
    if message.from_user.id != 7485721661:
        await message.reply("Только владелец может включить авто-озвучку.", **_reply_kwargs(message))
        return
    _voice_auto_chats.add(message.chat.id)
    await message.reply("🔊 Авто-озвучка Hu Tao включена. Ответы будут озвучиваться.", **_reply_kwargs(message))


@voice_router.message(Command("voice_off"))
async def cmd_voice_off(message: Message):
    if message.from_user.id != 7485721661:
        await message.reply("Только владелец может выключить.", **_reply_kwargs(message))
        return
    _voice_auto_chats.discard(message.chat.id)
    await message.reply("🔇 Авто-озвучка выключена.", **_reply_kwargs(message))


@voice_router.message(Command("voice"))
async def cmd_voice(message: Message, state: FSMContext):
    args = (message.text or "").strip().split()
    if len(args) >= 2:
        sub = args[1].lower()
        if sub == "on":
            await cmd_voice_on(message)
            return
        if sub == "off":
            await cmd_voice_off(message)
            return
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member and message.chat.type != "private":
        await message.reply("Доступ закрыт: сначала вступи в обязательную беседу, затем повтори /voice.")
        return
    await _clear_voice_state(message.from_user.id, state)
    await message.reply(VOICE_MENU_TEXT, reply_markup=_main_menu_keyboard(), parse_mode="HTML", **_reply_kwargs(message))

# ── Callback dispatcher ─────────────────────────────────────────────────────

@voice_router.callback_query(F.data.startswith("voice:"))
async def voice_callback(callback: types.CallbackQuery, state: FSMContext):
    data = callback.data or ""
    uid = callback.from_user.id
    if data.startswith("voice:tts_sel:"):
        selected_voice_id = data.split(":", 2)[2]
        if not await _can_use_voice(uid, selected_voice_id):
            await callback.answer("Этот голос не принадлежит тебе.", show_alert=True)
            return
    owned_voice = None
    for prefix in ("voice:voice_info:", "voice:preview:", "voice:delete_ask:", "voice:delete_confirm:"):
        if data.startswith(prefix):
            owned_voice = await get_voice_by_id(uid, data.removeprefix(prefix))
            if not owned_voice:
                await callback.answer("Голос не найден в твоей библиотеке.", show_alert=True)
                return
            break
    await callback.answer()

    if data == "voice:main":
        await _clear_voice_state(uid, state)
        await callback.message.edit_text(VOICE_MENU_TEXT, reply_markup=_main_menu_keyboard(), parse_mode="HTML")

    elif data == "voice:cancel":
        await _clear_voice_state(uid, state)
        from handlers.core import _main_menu_keyboard as global_menu
        await callback.message.edit_text(
            "<b>Hatani AI</b>\nОтменил операцию. Выбирай, что делать дальше.",
            reply_markup=global_menu(),
            parse_mode="HTML",
        )

    elif data == "voice:tts":
        await _enter_voice_flow(
            callback, state, uid, VoiceState.waiting_for_tts_text,
            "<b>Озвучить текст</b>\nПришли текст одним сообщением. Для отмены отправь /cancel.",
        )

    elif data.startswith("voice:tts_sel:"):
        voice_id = data.split(":", 2)[2]
        tts_text = (await state.get_data()).get("tts_text", "")
        if not tts_text:
            await callback.message.edit_text("Сначала введи текст, блять.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
            return
        try:
            settings = await get_settings(uid)
            msg = await callback.message.edit_text("🎤 Генерирую")
        except Exception as e:
            logger.error(f"TTS startup failed: {type(e).__name__}: {e}", exc_info=True)
            await safe_send(
                callback.bot.send_message,
                chat_id=callback.message.chat.id,
                text=f"Не удалось запустить TTS: {type(e).__name__}: {e}. Текст сохранён — выбери голос ещё раз.",
            )
            return
        await _clear_voice_state(uid, state)
        async def _tts_dots():
            for dots in [".","..","..."]:
                try: await msg.edit_text(f"🎤 Генерирую{dots}")
                except Exception: break
                await asyncio.sleep(0.4)
        dots_task = asyncio.create_task(_tts_dots())
        try:
            audio = await asyncio.wait_for(
                elevenlabs_tts(tts_text, voice_id, model=settings["tts_model"],
                               stability=settings["stability"], similarity_boost=settings["similarity_boost"],
                               style=settings["style"], speed=settings["speed"]),
                timeout=90,
            )
        except asyncio.TimeoutError:
            dots_task.cancel()
            await msg.edit_text("❌ TTS завис на 90 секунд, пиздец.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
            return
        except Exception as e:
            dots_task.cancel()
            await msg.edit_text(f"❌ TTS: {escape_html(str(e))}", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
            return
        dots_task.cancel()
        if audio:
            await callback.message.reply_voice(BufferedInputFile(audio, "tts.mp3"), caption=f"Текст: {tts_text[:100]}", **_reply_kwargs(callback.message))
            await msg.delete()
        else:
            await msg.edit_text("Блять, TTS не сработал. Смотри логи.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")

    elif data == "voice:my":
        voices = await get_voices(uid)
        if not voices:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить голос", callback_data="voice:clone")],
                [InlineKeyboardButton(text="🤖 Voice Design", callback_data="voice:design")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="voice:main")],
            ])
            await callback.message.edit_text("🎙 <b>Мои голоса</b>\n\nУ тебя пока нет своих голосов. Вот что можно:\n\n➕ <b>Добавить голос</b> — пришли ГС на 1-5 минут\n🤖 <b>Voice Design</b> — сгенерировать голос по описанию\n🎤 <b>Text-to-Speech</b> — встроенные голоса ElevenLabs", reply_markup=kb, parse_mode="HTML")
        else:
            buttons = []
            for v in voices:
                buttons.append([InlineKeyboardButton(text=f"🎙 {v['name']} ({v['tier']}, {v.get('duration_sec', 0) or 0:.0f}с)", callback_data=f"voice:voice_info:{v['voice_id']}")])
            buttons.append([InlineKeyboardButton(text="➕ Добавить голос", callback_data="voice:clone")])
            buttons.append([InlineKeyboardButton(text="🤖 Voice Design", callback_data="voice:design")])
            buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="voice:main")])
            await callback.message.edit_text("🎙 <b>Мои голоса:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

    elif data.startswith("voice:voice_info:"):
        assert owned_voice is not None
        voice_id = owned_voice["voice_id"]
        name = owned_voice["name"]
        voice_data = await elevenlabs_get_voice(voice_id)
        info = voice_data or {}
        await callback.message.edit_text(
            f"🎙 <b>{escape_html(str(name))}</b>\nID: {escape_html(str(voice_id))}\nКатегория: {escape_html(str(info.get('category', '?')))}\n",
            reply_markup=_voice_info_keyboard(voice_id),
            parse_mode="HTML",
        )

    elif data.startswith("voice:preview:"):
        voice_id = owned_voice["voice_id"]
        settings = await get_settings(uid)
        try:
            audio = await elevenlabs_tts("Привет, я твой клонированный голос.", voice_id, model=settings["tts_model"])
        except Exception as e:
            await callback.message.edit_text(
                f"Не удалось создать превью: {type(e).__name__}: {escape_html(str(e))}",
                reply_markup=_voice_info_keyboard(voice_id),
                parse_mode="HTML",
            )
            return
        if audio:
            await callback.message.reply_voice(BufferedInputFile(audio, "preview.mp3"), caption="Тестовая фраза", **_reply_kwargs(callback.message))
        else:
            await callback.message.edit_text(
                "ElevenLabs вернул пустое аудио. Повтори предпрослушивание.",
                reply_markup=_voice_info_keyboard(voice_id),
            )

    elif data.startswith("voice:delete_ask:"):
        assert owned_voice is not None
        voice_id = owned_voice["voice_id"]
        name = owned_voice["name"]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Удалить навсегда", callback_data=f"voice:delete_confirm:{voice_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"voice:voice_info:{voice_id}")],
        ])
        await callback.message.edit_text(
            f"Удалить голос <b>{escape_html(str(name))}</b>? Это действие нельзя отменить.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    elif data.startswith("voice:delete_confirm:"):
        assert owned_voice is not None
        voice_id = owned_voice["voice_id"]
        name = owned_voice["name"]
        try:
            await elevenlabs_delete_voice(voice_id)
        except Exception as e:
            await callback.message.edit_text(
                f"Не удалось удалить <b>{escape_html(str(name))}</b>: {type(e).__name__}: {escape_html(str(e))}",
                reply_markup=_voice_info_keyboard(voice_id),
                parse_mode="HTML",
            )
            return
        try:
            deleted_db = await delete_voice(uid, voice_id)
        except Exception as e:
            text = f"Голос удалён из ElevenLabs, но локальная запись осталась: {type(e).__name__}: {escape_html(str(e))}"
        else:
            text = f"Голос <b>{escape_html(str(name))}</b> удалён." if deleted_db else f"Голос {escape_html(str(voice_id))} удалён из ElevenLabs; локальная запись уже отсутствовала."
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К голосам", callback_data="voice:my")]]),
            parse_mode="HTML",
        )

    elif data == "voice:clone":
        await _enter_voice_flow(
            callback, state, uid, VoiceState.waiting_for_audio,
            "🎙 <b>Добавить голос</b>\nПришли голосовое сообщение 1-5 минут.\nДля PVC нужно 30+ мин (только Creator+).\nИли /cancel.",
        )

    elif data == "voice:design":
        await _enter_voice_flow(
            callback, state, uid, VoiceState.waiting_for_voice_design_prompt,
            "🤖 <b>Voice Design</b>\nОпиши голос словами. Пример:\n«Глубокий мужской голос с хрипотцой».\nИли /cancel.",
        )

    elif data == "voice:music":
        await _enter_voice_flow(callback, state, uid, VoiceState.waiting_for_music_prompt, "🎵 Опиши какая музыка нужна. Или /cancel.")

    elif data == "voice:sfx":
        await _enter_voice_flow(callback, state, uid, VoiceState.waiting_for_sfx_prompt, "🔊 Опиши звуковой эффект. Или /cancel.")

    elif data == "voice:iso":
        await _enter_voice_flow(callback, state, uid, VoiceState.waiting_for_isolator_audio, "🧹 Пришли аудио (mp3/ogg/wav) — уберу шум и фон. Или /cancel.")

    elif data == "voice:stt":
        await _enter_voice_flow(callback, state, uid, VoiceState.waiting_for_stt_audio, "📝 Пришли аудио (голосовое) — переведу в текст. Или /cancel.")

    elif data == "voice:changer":
        await _enter_voice_flow(
            callback, state, uid, VoiceState.waiting_for_changer_audio,
            "🔄 <b>Voice Changer</b>\nПришли аудио (песню, речь — до 5 мин).\nЯ выделю голос и переозвучу его выбранным голосом.\n/cancel для отмены.",
        )

    elif data == "voice:change":
        await _enter_voice_flow(
            callback, state, uid, VoiceState.waiting_for_change_audio,
            "🎭 <b>Сменить голос (речь)</b>\nПришли аудио с речью (без музыки).\nЯ переозвучу его выбранным голосом.\n/cancel для отмены.",
        )

    elif data == "voice:settings":
        try:
            settings = await get_settings(uid)
        except Exception as e:
            await _show_voice_failure(callback, "Не удалось загрузить настройки", e)
            return
        await callback.message.edit_text("⚙️ <b>Настройки TTS:</b>", reply_markup=_settings_keyboard(settings), parse_mode="HTML")

    elif data == "voice:set_model":
        models = [("V3 (эмоции+теги)","eleven_v3"),("Flash (быстрый)","eleven_flash_v2_5"),("Multilingual","eleven_multilingual_v2")]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"voice:set_model_val:{val}")] for label, val in models
        ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="voice:settings")]])
        await callback.message.edit_text("Выбери модель:", reply_markup=kb, parse_mode="HTML")

    elif data.startswith("voice:set_model_val:"):
        value = data.split(":", 2)[2]
        try:
            await save_settings(uid, tts_model=value)
            settings = await get_settings(uid)
        except Exception as e:
            await _show_voice_failure(callback, "Не удалось сохранить модель", e)
            return
        await callback.message.edit_text("Модель обновлена.", reply_markup=_settings_keyboard(settings), parse_mode="HTML")

    elif data in ("voice:set_stability", "voice:set_similarity", "voice:set_style", "voice:set_speed"):
        field_map = {"voice:set_stability":"stability","voice:set_similarity":"similarity_boost","voice:set_style":"style","voice:set_speed":"speed"}
        field = field_map[data]
        try:
            settings = await get_settings(uid)
        except Exception as e:
            await _show_voice_failure(callback, "Не удалось загрузить настройки", e)
            return
        current = settings[field]
        emoji_map = {"stability":{0.0:"🎲",0.4:"⚖️",0.8:"🎯"},"similarity_boost":{0.0:"🌊",0.4:"🎭",0.8:"🎯"},"style":{},"speed":{}}
        emojis = emoji_map.get(field,{})
        label_map = {"stability":"Стабильность","similarity_boost":"Похожесть","style":"Стиль","speed":"Скорость"}
        vals = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        rows = []
        for row_vals in (vals[:3], vals[3:]):
            btns = []
            for v in row_vals:
                emoji = ""
                for threshold, icon in sorted(emojis.items(), reverse=True):
                    if v >= threshold: emoji = icon; break
                check = "✓ " if abs(v - current) < 0.05 else ""
                suffix = "%" if field != "speed" else "x"
                display = f"{int(v*100)}{suffix}" if field != "speed" else f"{v}x"
                btns.append(InlineKeyboardButton(text=f"{check}{emoji} {display}" if emoji else f"{check}{display}", callback_data=f"voice:set_{field}_val:{v}"))
            rows.append(btns)
        rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="voice:settings")])
        await callback.message.edit_text(f"<b>{label_map[field]}</b>\nТекущее: {int(current*100)}%", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")

    elif data.startswith("voice:set_") and "_val:" in data:
        field_str = data.removeprefix("voice:set_").split("_val:")[0]
        value = float(data.rsplit(":", 1)[1])
        try:
            await save_settings(uid, **{field_str: value})
            settings = await get_settings(uid)
        except Exception as e:
            await _show_voice_failure(callback, "Не удалось сохранить настройку", e)
            return
        await callback.message.edit_text("Обновлено.", reply_markup=_settings_keyboard(settings), parse_mode="HTML")

# ── FSM handlers ────────────────────────────────────────────────────────────

@voice_router.message(VoiceState.waiting_for_tts_text)
async def process_tts_text(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    text = message.text or ""
    if not text.strip():
        await message.reply("Текст нужен, ебанат.")
        return
    await state.update_data(tts_text=text)
    uid = message.from_user.id
    user_voices = await get_voices(uid)
    await message.reply(f"Текст: «{escape_html(text[:100])}{'…' if len(text) > 100 else ''}»\nВыбери голос:", reply_markup=_tts_voice_keyboard(user_voices), parse_mode="HTML")

@voice_router.message(VoiceState.waiting_for_audio)
async def process_clone_audio(message: Message, state: FSMContext):
    logger.info(f"VOICE CLONE: got audio voice={bool(message.voice)} audio={bool(message.audio)} doc={bool(message.document)} user={message.from_user.id}")
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    if not message.voice and not message.audio and not (message.document and message.document.mime_type and 'audio' in message.document.mime_type):
        await message.reply("Блять, мне нужно голосовое или аудиофайл, а не вот это вот.")
        return
    if message.voice:
        file_id = message.voice.file_id
    elif message.audio:
        file_id = message.audio.file_id
    else:
        file_id = message.document.file_id
    try:
        audio_bytes = await _download_tg_file(message.bot, file_id)
    except Exception as e:
        logger.error(f"VOICE CLONE download failed: {e}", exc_info=True)
        await message.reply(f"Telegram не отдал аудио: {type(e).__name__}: {e}. Повтори /voice с MP3, OGG или WAV.", **_reply_kwargs(message))
        return
    status_msg = await message.reply("🧹 Чищу аудио от шума ")
    import time as _time
    _start = _time.monotonic()
    async def _progress_dots():
        frames = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
        i = 0
        while True:
            try:
                spin = frames[i % 10]
                elapsed = int(_time.monotonic() - _start)
                pos = i % 20
                if pos > 10: pos = 20 - pos
                bar = "█" * pos + "░" * (10 - pos)
                await status_msg.edit_text(f"🧹 Чищу аудио {spin} [{bar}] {elapsed}с")
                i += 1
                await asyncio.sleep(0.5)
            except Exception:
                break
    progress_task = asyncio.create_task(_progress_dots())
    try:
        cleaned_audio = await elevenlabs_voice_isolator(audio_bytes)
    except Exception as e:
        logger.error(f"Voice isolator crashed: {type(e).__name__}: {e}")
        cleaned_audio = None
    progress_task.cancel()
    if cleaned_audio:
        await status_msg.edit_text("✅ Аудио очищено! Теперь введи имя для голоса (на английском, до 64 символов):")
        _AUDIO_FOR_CLONE[message.from_user.id] = cleaned_audio
        await state.update_data(clone_audio_bytes=cleaned_audio)
    else:
        _elapsed = int(_time.monotonic() - _start)
        await status_msg.edit_text(f"⚠️ Очистка упала через {_elapsed}с. Использую как есть. Введи имя:")
        _AUDIO_FOR_CLONE[message.from_user.id] = audio_bytes
        await state.update_data(clone_audio_bytes=audio_bytes)
    await state.set_state(VoiceState.waiting_for_name)

@voice_router.message(VoiceState.waiting_for_name)
async def process_clone_name(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    name = (message.text or "").strip()[:64]
    if not name:
        await message.reply("Имя нужно, ебанат.")
        return
    audio_bytes = _AUDIO_FOR_CLONE.pop(message.from_user.id, None)
    if not audio_bytes:
        data = await state.get_data()
        audio_bytes = data.get("clone_audio_bytes")
        if not audio_bytes:
            await message.reply("Не нашёл аудио. Начни заново с /voice → Добавить голос.")
            await _clear_voice_state(message.from_user.id, state)
            return
    try:
        msg = await message.reply("🎙 Клонирую голос, жди...")
        with tempfile.NamedTemporaryFile(suffix='.in', delete=False) as file:
            file.write(audio_bytes)
            input_path = file.name
        output_path = input_path + '.mp3'
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-i', input_path, '-t', '300', '-ac', '1', '-b:a', '64k', output_path],
                capture_output=True,
                timeout=30,
            )
            if _os.path.exists(output_path):
                with open(output_path, 'rb') as file:
                    audio_bytes = file.read()
        finally:
            _os.unlink(input_path)
            if _os.path.exists(output_path):
                _os.unlink(output_path)
        try:
            voice_id = await elevenlabs_add_voice(name, audio_bytes)
        except Exception as e:
            await msg.edit_text(f"Не удалось клонировать голос: {type(e).__name__}: {escape_html(str(e))}", parse_mode="HTML")
            return
        if not voice_id:
            await msg.edit_text("ElevenLabs не создал голос. Повтори позже.", parse_mode="HTML")
            return
        try:
            await _persist_remote_voice(message.from_user.id, name, voice_id, "cloned", len(audio_bytes) / 16000)
        except Exception as e:
            await msg.edit_text(f"Не удалось сохранить голос: {type(e).__name__}: {escape_html(str(e))}", parse_mode="HTML")
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎧 Прослушать", callback_data=f"voice:preview:{voice_id}")],
            [InlineKeyboardButton(text="🔙 К голосам", callback_data="voice:my")],
        ])
        await msg.edit_text(f"Голос <b>{escape_html(name)}</b> добавлен, сука!", reply_markup=keyboard, parse_mode="HTML")
    finally:
        await _clear_voice_state(message.from_user.id, state)

@voice_router.message(VoiceState.waiting_for_voice_design_prompt)
async def process_voice_design(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    prompt = (message.text or "").strip()
    if not prompt:
        await message.reply("Опиши голос, ебанат.")
        return
    try:
        msg = await message.reply("🤖 Генерирую голос по описанию...")
        name = f"designed_{uuid.uuid4().hex[:8]}"
        try:
            voice_id = await elevenlabs_design_voice(name, prompt)
        except Exception as e:
            await msg.edit_text(f"Не удалось создать голос: {type(e).__name__}: {escape_html(str(e))}", parse_mode="HTML")
            return
        if not voice_id:
            await msg.edit_text("ElevenLabs не создал голос. Попробуй другое описание.", parse_mode="HTML")
            return
        try:
            await _persist_remote_voice(message.from_user.id, name, voice_id, "designed", 0)
        except Exception as e:
            await msg.edit_text(f"Не удалось сохранить голос: {type(e).__name__}: {escape_html(str(e))}", parse_mode="HTML")
            return
        await msg.edit_text(f"Голос <b>{escape_html(name)}</b> сгенерирован! ID: {escape_html(voice_id)}", parse_mode="HTML")
    finally:
        await _clear_voice_state(message.from_user.id, state)

@voice_router.message(VoiceState.waiting_for_music_prompt)
async def process_music(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    prompt = (message.text or "").strip()
    if not prompt:
        await message.reply("Опиши музыку, ебанат.")
        return
    try:
        msg = await message.reply("🎵 Генерирую музыку (до минуты)...")
        try:
            audio = await elevenlabs_music(prompt)
        except Exception as e:
            await msg.edit_text(f"Не удалось создать музыку: {type(e).__name__}: {escape_html(str(e))}", parse_mode="HTML")
            return
        if audio:
            await message.reply_audio(BufferedInputFile(audio, "music.mp3"), title=prompt[:64], performer="ElevenLabs Music", **_reply_kwargs(message))
            await msg.delete()
        else:
            await msg.edit_text("ElevenLabs вернул пустое аудио. Повтори позже.", parse_mode="HTML")
    finally:
        await _clear_voice_state(message.from_user.id, state)

@voice_router.message(VoiceState.waiting_for_sfx_prompt)
async def process_sfx(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    prompt = (message.text or "").strip()
    if not prompt:
        await message.reply("Опиши звук, ебанат.")
        return
    try:
        msg = await message.reply("🔊 Генерирую звук...")
        try:
            audio = await elevenlabs_sfx(prompt)
        except Exception as e:
            await msg.edit_text(f"Не удалось создать звук: {type(e).__name__}: {escape_html(str(e))}", parse_mode="HTML")
            return
        if audio:
            await message.reply_audio(BufferedInputFile(audio, "sfx.mp3"), title=prompt[:64], **_reply_kwargs(message))
            await msg.delete()
        else:
            await msg.edit_text("ElevenLabs вернул пустой звук. Повтори позже.", parse_mode="HTML")
    finally:
        await _clear_voice_state(message.from_user.id, state)

@voice_router.message(VoiceState.waiting_for_isolator_audio)
async def process_isolator(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    if not message.voice and not message.audio:
        await message.reply("Пришли аудиофайл для очистки.")
        return
    try:
        file_id = message.voice.file_id if message.voice else message.audio.file_id
        msg = await message.reply("🧹 Убираю шум...")
        try:
            audio_bytes = await _download_tg_file(message.bot, file_id)
        except Exception as e:
            logger.error(f"VOICE ISOLATOR download failed: {e}", exc_info=True)
            await msg.edit_text(f"❌ Telegram не отдал аудио: {type(e).__name__}: {e}. Отправь другой MP3, OGG или WAV.")
            return
        try:
            cleaned = await elevenlabs_voice_isolator(audio_bytes)
        except Exception as e:
            await msg.edit_text(f"Не удалось очистить аудио: {type(e).__name__}: {escape_html(str(e))}", parse_mode="HTML")
            return
        if cleaned:
            await message.reply_audio(BufferedInputFile(cleaned, "cleaned.mp3"), title="Очищенный звук", **_reply_kwargs(message))
            await msg.delete()
        else:
            await msg.edit_text("ElevenLabs вернул пустое аудио. Попробуй другой формат.", parse_mode="HTML")
    finally:
        await _clear_voice_state(message.from_user.id, state)

@voice_router.message(VoiceState.waiting_for_stt_audio)
async def process_stt(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    if not message.voice and not message.audio:
        await message.reply("Пришли аудио для распознавания.")
        return
    try:
        file_id = message.voice.file_id if message.voice else message.audio.file_id
        msg = await message.reply("📝 Распознаю речь...")
        try:
            audio_bytes = await _download_tg_file(message.bot, file_id)
        except Exception as e:
            logger.error(f"STT download failed: {e}", exc_info=True)
            await msg.edit_text(f"❌ Telegram не отдал аудио: {type(e).__name__}: {e}. Отправь другой MP3, OGG или WAV.")
            return
        text, err = await elevenlabs_stt(audio_bytes)
        if text:
            await msg.edit_text(f"<b>Распознано:</b>\n<pre>{escape_html(text[:3800])}</pre>", parse_mode="HTML")
        else:
            await msg.edit_text(f"❌ STT: {escape_html(str(err or 'неизвестная ошибка'))}", parse_mode="HTML")
    finally:
        await _clear_voice_state(message.from_user.id, state)


# ── Voice Changer FSM ───────────────────────────────────────────────────────

_CHANGER_AUDIO: dict[int, bytes] = {}

def _voice_picker_keyboard(voices: list[dict], prefix: str = "vc") -> InlineKeyboardMarkup:
    """Build keyboard to pick a target voice for voice changer."""
    buttons = []
    for v in voices[:30]:
        buttons.append([InlineKeyboardButton(text=f"🎙 {v['name']}", callback_data=f"{prefix}:{v['voice_id']}")])
    for name, voice_id in _VOICE_CHANGER_DEFAULTS:
        if not any(voice["voice_id"] == voice_id for voice in voices):
            buttons.append([InlineKeyboardButton(text=f"🔊 {name} (встр.)", callback_data=f"{prefix}:{voice_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="voice:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)








@voice_router.message(VoiceState.waiting_for_changer_audio)
async def process_changer_audio(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    if not message.voice and not message.audio and not message.document:
        await message.reply("Пришли аудиофайл (песню, речь — до 5 мин).")
        return
    uid = message.from_user.id
    transitioned = False
    try:
        file_id = message.voice.file_id if message.voice else (message.audio.file_id if message.audio else message.document.file_id)
        msg = await message.reply("🎵 Загружаю аудио...")
        try:
            audio_bytes = await _download_tg_file(message.bot, file_id)
        except Exception as e:
            logger.error(f"VOICE CHANGER download failed: {e}", exc_info=True)
            await message.reply(f"Telegram не отдал аудио: {type(e).__name__}: {e}. Начни Voice Changer заново с файлом MP3, OGG или WAV.", **_reply_kwargs(message))
            return

        await msg.edit_text("🎵 MVSEP разделяет... (обычно 30-90с)")
        started = time.monotonic()

        async def tick():
            while True:
                await asyncio.sleep(5)
                try:
                    elapsed = int(time.monotonic() - started)
                    await msg.edit_text(f"🎵 MVSEP разделяет... {elapsed}с")
                except Exception:
                    break

        tick_task = asyncio.create_task(tick())
        try:
            result, err = await mvsep_separate(audio_bytes)
        except Exception as e:
            await msg.edit_text(f"❌ MVSEP: {type(e).__name__}: {e}", **_reply_kwargs(message))
            return
        finally:
            tick_task.cancel()
        if not result:
            await msg.edit_text(f"❌ MVSEP: {err or 'не смог.'}", **_reply_kwargs(message))
            return

        await msg.edit_text("📥 Скачиваю результат...")
        try:
            vocals = await mvsep_download(result["vocal_url"])
            instrumental = await mvsep_download(result["inst_url"])
        except Exception as e:
            await msg.edit_text(f"❌ MVSEP download: {type(e).__name__}: {e}", **_reply_kwargs(message))
            return
        if not vocals or not instrumental:
            await msg.edit_text("❌ MVSEP вернул пустые дорожки. Повтори позже.", **_reply_kwargs(message))
            return

        _CHANGER_AUDIO[uid] = {"vocals": vocals, "instrumental": instrumental}
        await state.set_state(VoiceState.waiting_for_changer_voice)
        await msg.edit_text("✅ Разделил! Выбери целевой голос:")
        voices = await get_voices(uid)
        await message.reply("Выбери голос для переозвучки:", reply_markup=_voice_picker_keyboard(voices, "vc"))
        transitioned = True
    finally:
        if not transitioned:
            await _clear_voice_state(uid, state)


@voice_router.callback_query(F.data.startswith("vc:"))
async def process_changer_voice_select(callback: types.CallbackQuery, state: FSMContext):
    target_voice_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    if not await _can_use_voice(uid, target_voice_id):
        await callback.answer("Этот голос не принадлежит тебе.", show_alert=True)
        return
    await callback.answer()
    stored = _CHANGER_AUDIO.pop(uid, None)
    if not stored:
        try:
            await callback.message.edit_text("Аудио потерялось, начни заново.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        finally:
            await _clear_voice_state(uid, state)
        return
    try:
        msg = await callback.message.edit_text("🔄 Меняю голос вокала...")
        changed_vocals, err = await elevenlabs_voice_changer(stored["vocals"], target_voice_id)
        if not changed_vocals:
            await msg.edit_text(f"❌ Voice changer: {escape_html(str(err or 'неизвестная ошибка'))}", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
            return
        await msg.edit_text("🎚 Микширую с инструменталом...")
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as file:
            file.write(stored["instrumental"])
            instrumental_path = file.name
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as file:
            file.write(changed_vocals)
            vocals_path = file.name
        output_path = tempfile.mktemp(suffix='.mp3')
        try:
            subprocess.run([
                'ffmpeg', '-y', '-i', instrumental_path, '-i', vocals_path,
                '-filter_complex', '[0:a]loudnorm=I=-16:TP=-1.5:LRA=11:linear=true[inst];[1:a]loudnorm=I=-16:TP=-1.5:LRA=11:linear=true[voc];[inst][voc]amix=inputs=2:duration=first,volume=2.0',
                '-b:a', '128k', output_path,
            ], capture_output=True, timeout=30)
            if not _os.path.exists(output_path):
                raise RuntimeError("ffmpeg output missing")
            with open(output_path, 'rb') as file:
                final = file.read()
            await callback.message.reply_audio(
                BufferedInputFile(final, "changed_song.mp3"),
                title="Voice Changed",
                performer="ElevenLabs + Demucs",
                **_reply_kwargs(callback.message),
            )
        except Exception as e:
            logger.error(f"Mix failed: {e}", exc_info=True)
            await callback.message.reply_voice(
                BufferedInputFile(changed_vocals, "changed_vocals.mp3"),
                caption=f"⚠️ Микс не собран: {type(e).__name__}: {e}. Отправляю только вокал.",
                **_reply_kwargs(callback.message),
            )
        finally:
            _os.unlink(instrumental_path)
            _os.unlink(vocals_path)
            if _os.path.exists(output_path):
                _os.unlink(output_path)
        await msg.delete()
    finally:
        await _clear_voice_state(uid, state)


# ── Simple Voice Change (speech only, no MVSEP) ─────────────────────────────

_CHANGE_AUDIO: dict[int, bytes] = {}


@voice_router.message(VoiceState.waiting_for_change_audio)
async def process_change_audio(message: Message, state: FSMContext):
    if message.text and '/cancel' in message.text:
        await _clear_voice_state(message.from_user.id, state)
        await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    if not message.voice and not message.audio:
        await message.reply("Пришли голосовое или аудиофайл с речью.")
        return
    uid = message.from_user.id
    transitioned = False
    try:
        file_id = message.voice.file_id if message.voice else message.audio.file_id
        msg = await message.reply("📥 Скачиваю...")
        try:
            audio_bytes = await _download_tg_file(message.bot, file_id)
        except Exception as e:
            logger.error(f"CHANGE VOICE download failed: {e}", exc_info=True)
            await message.reply(f"Telegram не отдал аудио: {type(e).__name__}: {e}. Начни смену голоса заново с MP3, OGG или WAV.", **_reply_kwargs(message))
            return
        _CHANGE_AUDIO[uid] = audio_bytes
        await state.set_state(VoiceState.waiting_for_change_voice)
        await msg.edit_text("✅ Выбери целевой голос:")
        voices = await get_voices(uid)
        await message.reply("Выбери голос:", reply_markup=_voice_picker_keyboard(voices, "change"))
        transitioned = True
    finally:
        if not transitioned:
            await _clear_voice_state(uid, state)


@voice_router.callback_query(F.data.startswith("change:"))
async def process_change_voice_select(callback: types.CallbackQuery, state: FSMContext):
    target_voice_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    if not await _can_use_voice(uid, target_voice_id):
        await callback.answer("Этот голос не принадлежит тебе.", show_alert=True)
        return
    await callback.answer()
    audio = _CHANGE_AUDIO.pop(uid, None)
    if not audio:
        try:
            await callback.message.edit_text("Аудио потерялось.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        finally:
            await _clear_voice_state(uid, state)
        return
    try:
        msg = await callback.message.edit_text("🔄 Меняю голос...")
        result, err = await elevenlabs_voice_changer(audio, target_voice_id)
        if result:
            await callback.message.reply_voice(BufferedInputFile(result, "changed.mp3"), caption="✅", **_reply_kwargs(callback.message))
            await msg.delete()
        else:
            await msg.edit_text(f"❌ Voice changer: {escape_html(str(err or 'неизвестная ошибка'))}", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
    finally:
        await _clear_voice_state(uid, state)
@voice_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await _clear_voice_state(message.from_user.id, state)
    await message.reply("Отменил всё нахуй.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
