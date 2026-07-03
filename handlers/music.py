"""Lyria music generation command handler — /music."""

import asyncio
import logging
import uuid

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from config import FULL_ACCESS_CHAT_ID
from handlers.common import safe_send, _track_user, _maybe_send_random_chat_media
from services.music_service import generate_music, MUSIC_MODELS, MUSIC_MODEL_LIST

logger = logging.getLogger(__name__)

music_router = Router()

# Cooldown (per-user, 60 seconds)
_cooldowns: dict[int, float] = {}
_COOLDOWN = 60

# Pending requests: request_id → {chat_id, user_id, prompt, msg_id}
_pending_music: dict[str, dict] = {}


def _music_model_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard for Lyria model selection."""
    buttons = []
    for i, mk in enumerate(MUSIC_MODEL_LIST):
        info = MUSIC_MODELS[mk]
        buttons.append([
            InlineKeyboardButton(
                text=f"{info['label']} — {info['desc']}",
                callback_data=f"musicsel:{request_id}:{mk}"
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"musicsel:{request_id}:cancel"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@music_router.message(Command("music"))
async def cmd_music(message: types.Message):
    """Handle /music command — prompt user for lyrics/prompt, then select model."""
    _track_user(message)

    uid = message.from_user.id

    args = message.text.strip()
    cmd_end = args.find(" ")
    if cmd_end == -1:
        prompt = ""
    else:
        prompt = args[cmd_end:].strip()

    if not prompt:
        await message.reply(
            "🎵 <b>Lyria Music Generator</b>\n\n"
            "Напиши текст песни или опиши что хочешь услышать.\n"
            "Примеры:\n"
            "<code>/music Рэп про то как Нано взломал пентагон</code>\n"
            "<code>/music Грустная поп-баллада о несчастной любви и пиве</code>\n"
            "<code>/music Orchestral cinematic piece, 2 minutes</code>\n\n"
            "Можно указать: жанр, настроение, инструменты, темп, структуру.",
            parse_mode="HTML",
        )
        return

    # Clean up old pending requests for this user
    for rid in list(_pending_music.keys()):
        if _pending_music[rid].get("user_id") == uid:
            del _pending_music[rid]

    # Show model selection keyboard
    request_id = uuid.uuid4().hex[:12]
    _pending_music[request_id] = {
        "chat_id": message.chat.id,
        "user_id": uid,
        "prompt": prompt,
        "model": "lyria-clip",
    }

    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs["message_thread_id"] = message.message_thread_id

    sent = await safe_send(
        message.bot.send_message,
        chat_id=message.chat.id,
        text=f"🎵 <b>Текст:</b> {prompt[:500]}\n\nВыбери модель генерации:",
        reply_markup=_music_model_keyboard(request_id),
        parse_mode="HTML",
        **reply_kwargs,
    )
    if sent:
        _pending_music[request_id]["msg_id"] = sent.message_id


@music_router.callback_query(F.data.startswith("musicsel:"))
async def music_model_callback(callback: types.CallbackQuery):
    """Handle Lyria model selection and trigger generation."""
    try:
        await callback.answer()
    except Exception:
        pass

    _, request_id, choice = callback.data.split(":", 2)
    data = _pending_music.pop(request_id, None)
    if not data:
        try:
            await callback.message.edit_text("🎵 Запрос устарел. Отправь /music заново.")
        except Exception:
            pass
        return

    if choice == "cancel":
        try:
            await callback.message.edit_text("🎵 Генерация отменена.")
        except Exception:
            pass
        return

    if choice not in MUSIC_MODELS:
        try:
            await callback.message.edit_text(f"🎵 Неизвестная модель: {choice}")
        except Exception:
            pass
        return
    # Cooldown check
    now = asyncio.get_event_loop().time()
    uid = callback.from_user.id
    if uid in _cooldowns and now - _cooldowns[uid] < _COOLDOWN:
        remaining = int(_COOLDOWN - (now - _cooldowns[uid]))
        try:
            await callback.answer(f"Кулдаун {remaining}с", show_alert=True)
        except Exception:
            pass
        return
    _cooldowns[uid] = now

    model_info = MUSIC_MODELS[choice]
    prompt = data["prompt"]
    # Update message with progress
    try:
        await callback.message.edit_text(
            f"🎵 <b>Генерирую музыку...</b>\n"
            f"Модель: {model_info['label']}\n"
            f"Промпт: {prompt[:300]}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Generate
    audio_bytes, lyrics, error = await generate_music(
        prompt=prompt,
        model_key=choice,
    )

    if error:
        try:
            await callback.message.edit_text(
                f"🎵 <b>Ошибка генерации:</b>\n{error}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    reply_kwargs = {}
    if callback.message.chat.is_forum and callback.message.message_thread_id:
        reply_kwargs["message_thread_id"] = callback.message.message_thread_id

    # Send audio
    if audio_bytes:
        caption = ""
        if lyrics:
            caption = f"<b>Текст песни:</b>\n<blockquote expandable>{lyrics[:1000]}</blockquote>"

        try:
            await callback.message.delete()
        except Exception:
            pass

        filename = f"lyria_{choice}_{request_id}.mp3"
        await safe_send(
            callback.message.bot.send_audio,
            chat_id=data["chat_id"],
            caption=caption or "🎵",
            audio=BufferedInputFile(audio_bytes, filename=filename),
            title=f"Lyria — {model_info['label']}",
            performer="Hatani AI",
            parse_mode="HTML",
            **reply_kwargs,
        )
    else:
        try:
            await callback.message.edit_text("🎵 Пустой ответ от модели.")
        except Exception:
            pass
