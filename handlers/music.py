"""Lyria music generation command handler — /music."""

import asyncio
import logging
import uuid
from html import escape

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers.common import (
    safe_send,
    _track_user,
    _suno_cfg_text,
    _suno_cfg_keyboard,
    _SUNO_CFG_DEFAULTS,
)
from services.music_service import generate_music, MUSIC_MODELS, MUSIC_MODEL_LIST
from services.suno_service import SUNO_MODELS, generate_suno
from state import pending_suno_configs


logger = logging.getLogger(__name__)

music_router = Router()

# Cooldown (per-user, 20 seconds)
_cooldowns: dict[int, float] = {}
_COOLDOWN = 20

# Pending requests: request_id → {chat_id, user_id, prompt, msg_id}
_pending_music: dict[str, dict] = {}

# Users typing a Suno field value: (chat_id, user_id) → {request_id, field, msg_id}
_suno_awaiting_input: dict[tuple[int, int], dict] = {}

# Buttons on the first screen: Lyria first (no setup), then Suno (has settings).
_MUSIC_PROVIDERS = {**MUSIC_MODELS, **SUNO_MODELS}


def _music_model_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard for music model selection (Lyria + Suno)."""
    buttons = []
    for mk, info in _MUSIC_PROVIDERS.items():
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
            "<b>Музыка</b>\n"
            "Описание пустое. Напиши жанр, настроение, инструменты или текст песни.\n\n"
            "Примеры:\n"
            "<code>/music мрачный synthwave без вокала</code>\n"
            "<code>/music рэп про Нано, который взломал Пентагон</code>\n\n"
            "Чем точнее запрос, тем меньше музыкальной каши получишь.",
            parse_mode="HTML",
        )
        return

    # Clean up old pending requests for this user
    for rid in list(_pending_music.keys()):
        if _pending_music[rid].get("user_id") == uid:
            del _pending_music[rid]
    for rid in list(pending_suno_configs.keys()):
        if pending_suno_configs[rid].get("user_id") == uid:
            del pending_suno_configs[rid]
        _suno_awaiting_input.pop((pending_suno_configs.get(rid, {}).get('chat_id'), uid), None)

    # Show model selection keyboard
    request_id = uuid.uuid4().hex[:12]
    _pending_music[request_id] = {
        "chat_id": message.chat.id,
        "user_id": uid,
        "prompt": prompt,
        "model": "lyria-clip",
        "message_thread_id": message.message_thread_id if message.chat.is_forum else None,
    }

    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs["message_thread_id"] = message.message_thread_id

    sent = await safe_send(
        message.bot.send_message,
        chat_id=message.chat.id,
        text=f"<b>Задача:</b> {escape(prompt[:500])}\n\nВыбирай модель. Для короткого результата бери Clip.",
        reply_markup=_music_model_keyboard(request_id),
        parse_mode="HTML",
        **reply_kwargs,
    )
    if sent:
        _pending_music[request_id]["msg_id"] = sent.message_id


@music_router.callback_query(F.data.startswith("musicsel:"))
async def music_model_callback(callback: types.CallbackQuery):
    """Handle Lyria model selection and trigger generation."""

    _, request_id, choice = callback.data.split(":", 2)
    data = _pending_music.get(request_id)
    if not data:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        try:
            await callback.message.edit_text("🎵 Запрос устарел. Отправь /music заново.")
        except Exception:
            pass
        return

    if callback.from_user.id != data["user_id"]:
        await callback.answer("Эту кнопку может нажать только автор запроса.", show_alert=True)
        return

    if choice == "cancel":
        await callback.answer()
        _pending_music.pop(request_id, None)
        try:
            await callback.message.edit_text("🎵 Генерация отменена.")
        except Exception:
            pass
        return

    if choice not in _MUSIC_PROVIDERS:
        await callback.answer(f"Неизвестная модель: {choice}", show_alert=True)
        try:
            await callback.message.edit_text(f"🎵 Неизвестная модель: {choice}")
        except Exception:
            pass
        return

    # Suno models open a settings screen first; Lyria has nothing to configure.
    if choice in SUNO_MODELS:
        await callback.answer()
        _pending_music.pop(request_id, None)
        info = SUNO_MODELS[choice]
        pending_suno_configs[request_id] = {
            **data,
            'model': choice,
            'label': info['label'],
            'cfg': dict(_SUNO_CFG_DEFAULTS),
        }
        try:
            await callback.message.edit_text(
                _suno_cfg_text(request_id),
                reply_markup=_suno_cfg_keyboard(request_id),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # Cooldown check
    now = asyncio.get_event_loop().time()
    uid = data["user_id"]
    if uid in _cooldowns and now - _cooldowns[uid] < _COOLDOWN:
        remaining = int(_COOLDOWN - (now - _cooldowns[uid]))
        try:
            await callback.answer(f"Кулдаун {remaining}с", show_alert=True)
        except Exception:
            pass
        return
    await callback.answer()
    _cooldowns[uid] = now
    _pending_music.pop(request_id, None)

    model_info = MUSIC_MODELS[choice]
    prompt = data["prompt"]
    # Update message with progress
    try:
        await callback.message.edit_text(
            f"🎵 <b>Генерирую музыку...</b>\n"
            f"Модель: {escape(str(model_info['label']))}\n"
            f"Промпт: {escape(prompt[:300])}",
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
                f"🎵 <b>Ошибка генерации:</b>\n{escape(str(error))}",
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
            caption = f"<b>Текст песни:</b>\n<blockquote expandable>{escape(lyrics[:1000])}</blockquote>"

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


# ── Suno settings screen ──────────────────────────────────────────────────
# Fields the user types as text rather than picks from buttons.
_SUNO_TEXT_FIELDS = {
    'style': '✏️ Пришли <b>стиль</b> (жанр, инструменты, вайб) следующим сообщением.\n\nПример: <code>мрачный synthwave, аналоговые синты, без вокала</code>',
    'lyrics': '📝 Пришли <b>текст песни</b> следующим сообщением.\n\nМожно с тегами структуры: <code>[Verse]</code>, <code>[Chorus]</code>, <code>[Bridge]</code>.',
    'title': '🏷 Пришли <b>название трека</b> следующим сообщением (до 80 символов).',
    'neg': '🚫 Пришли стили, которые <b>исключить</b>.\n\nПример: <code>heavy metal, drums, distorted guitar</code>',
}

_SUNO_NUMERIC_FIELDS = {'style_weight': float, 'weirdness': float, 'audio_weight': float,
                        'variety': int, 'duration': int}


async def _suno_guard(callback: types.CallbackQuery):
    """Resolve the pending config for a Suno callback, answering errors itself.

    Returns (request_id, config); both None when the callback was rejected.
    """
    request_id = callback.data.split(':', 1)[1]
    d = pending_suno_configs.get(request_id)
    if not d:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return None, None
    if callback.from_user.id != d['user_id']:
        await callback.answer("Эту кнопку может нажать только автор запроса.", show_alert=True)
        return None, None
    return request_id, d


@music_router.callback_query(F.data.startswith("sunocfg:"))
async def suno_config_callback(callback: types.CallbackQuery):
    """Toggle a Suno setting."""
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer()
        return
    (_, request_id, field, value) = parts
    d = pending_suno_configs.get(request_id)
    if not d:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    if callback.from_user.id != d["user_id"]:
        await callback.answer("Эту кнопку может нажать только автор запроса.", show_alert=True)
        return
    convert = _SUNO_NUMERIC_FIELDS.get(field)
    d['cfg'][field] = convert(value) if convert else value
    await callback.answer()
    try:
        await callback.message.edit_text(_suno_cfg_text(request_id),
                                         reply_markup=_suno_cfg_keyboard(request_id),
                                         parse_mode="HTML")
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("sunoinput:"))
async def suno_input_callback(callback: types.CallbackQuery):
    """Ask for a text value for one Suno setting."""
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    (_, request_id, field) = parts
    d = pending_suno_configs.get(request_id)
    if not d:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    if callback.from_user.id != d["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return
    await callback.answer()
    _suno_awaiting_input[(d['chat_id'], d['user_id'])] = {
        'request_id': request_id, 'field': field, 'msg_id': callback.message.message_id,
    }
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='❌ Отмена', callback_data=f'sunocancel:{request_id}')
    ]])
    try:
        await callback.message.edit_text(
            _SUNO_TEXT_FIELDS.get(field, '✏️ Пришли значение следующим сообщением:'),
            reply_markup=cancel_kb, parse_mode="HTML",
        )
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("sunoback:"))
async def suno_back_callback(callback: types.CallbackQuery):
    """Return to the model list."""
    request_id, d = await _suno_guard(callback)
    if not d:
        return
    _suno_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    pending_suno_configs.pop(request_id, None)
    request = {k: v for k, v in d.items() if k not in ('cfg', 'label', 'model')}
    _pending_music[request_id] = request
    await callback.answer()
    try:
        await callback.message.edit_text(
            f"<b>Задача:</b> {escape(request['prompt'][:500])}\n\nВыбирай модель. Для короткого результата бери Clip.",
            reply_markup=_music_model_keyboard(request_id), parse_mode="HTML",
        )
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("sunocancel:"))
async def suno_cancel_callback(callback: types.CallbackQuery):
    """Drop the pending Suno request."""
    request_id, d = await _suno_guard(callback)
    if not d:
        return
    _suno_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    pending_suno_configs.pop(request_id, None)
    await callback.answer()
    try:
        await callback.message.edit_text("🎵 Генерация отменена.")
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("sunogen:"))
async def suno_generate_callback(callback: types.CallbackQuery):
    """Generate with Suno using the collected settings."""
    request_id, d = await _suno_guard(callback)
    if not d:
        return
    now = asyncio.get_event_loop().time()
    uid = d['user_id']
    if uid in _cooldowns and now - _cooldowns[uid] < _COOLDOWN:
        remaining = int(_COOLDOWN - (now - _cooldowns[uid]))
        await callback.answer(f"Кулдаун {remaining}с", show_alert=True)
        return
    await callback.answer()
    _cooldowns[uid] = now
    _suno_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    pending_suno_configs.pop(request_id, None)

    cfg = d['cfg']
    model_key = d['model']
    label = d['label']

    async def _status(text: str):
        try:
            await callback.message.edit_text(text)
        except Exception:
            pass

    try:
        await callback.message.edit_text(f"🎼 <b>Генерирую через {escape(label)}...</b>\n\nSuno обычно отдаёт два варианта, это занимает минуту-две.", parse_mode="HTML")
    except Exception:
        pass

    tracks, error = await generate_suno(d['prompt'], model_key=model_key, cfg=cfg, status_cb=_status)

    reply_kwargs = {}
    if d.get('message_thread_id'):
        reply_kwargs['message_thread_id'] = d['message_thread_id']

    if error or not tracks:
        try:
            await callback.message.edit_text(f"🎼 <b>Ошибка Suno:</b>\n{escape(str(error))}", parse_mode="HTML")
        except Exception:
            pass
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    for idx, track in enumerate(tracks, 1):
        title = track['title'] or f'Suno {idx}'
        caption_parts = [f"<b>{escape(title)}</b>"]
        meta = []
        if track['duration']:
            meta.append(f"⏱ {int(float(track['duration']))}с")
        meta.append(escape(label))
        caption_parts.append(' · '.join(meta))
        if track['tags']:
            caption_parts.append(f"🎼 {escape(track['tags'][:220])}")
        if track['prompt']:
            lyrics = track['prompt'].strip()
            if lyrics:
                caption_parts.append(f"📝 <blockquote expandable>{escape(lyrics[:700])}</blockquote>")
        caption = '\n'.join(caption_parts)[:1020]

        kwargs = {}
        if track.get('cover'):
            kwargs['thumbnail'] = BufferedInputFile(track['cover'], filename='cover.jpg')
        try:
            await safe_send(
                callback.message.bot.send_audio,
                chat_id=d['chat_id'],
                audio=BufferedInputFile(track['audio'], filename=f"suno_{request_id}_{idx}.mp3"),
                caption=caption,
                title=title[:64],
                performer='Suno',
                parse_mode="HTML",
                **kwargs,
                **reply_kwargs,
            )
        except Exception as e:
            logger.warning(f"Suno track {idx} send failed: {type(e).__name__}: {e}")
            await safe_send(
                callback.message.bot.send_message,
                chat_id=d['chat_id'],
                text=f'❌ Не смог отправить трек {idx}: {type(e).__name__}',
                **reply_kwargs,
            )
