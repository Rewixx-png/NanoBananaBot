"""Music generation command handler — /music.

Two levels: pick a provider (Lyria by Google, or Suno), then a model of that
provider, then that provider's own settings screen.
"""

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
    _lyria_cfg_text,
    _lyria_cfg_keyboard,
    _LYRIA_CFG_DEFAULTS,
)
from services.music_service import generate_music, MUSIC_MODELS
from services.suno_service import SUNO_MODELS, generate_suno
from state import pending_suno_configs, pending_lyria_configs


logger = logging.getLogger(__name__)

music_router = Router()

# Cooldown (per-user, 20 seconds)
_cooldowns: dict[int, float] = {}
_COOLDOWN = 20

# Root request: request_id → {chat_id, user_id, prompt, message_thread_id, msg_id}
_pending_music: dict[str, dict] = {}

# Users typing a field value: (chat_id, user_id) → {request_id, field, msg_id}
_suno_awaiting_input: dict[tuple[int, int], dict] = {}
_lyria_awaiting_input: dict[tuple[int, int], dict] = {}
# Users typing the song idea after a bare /music, before a request exists.
_music_awaiting_idea: dict[tuple[int, int], dict] = {}

PROVIDERS = {
    'lyria': {
        'label': '🎵 Lyria by Google',
        'desc': 'Быстро, инструментал и структура',
        'models': MUSIC_MODELS,
    },
    'suno': {
        'label': '🎤 Suno V6',
        'desc': 'Песни с вокалом и текстом, свои настройки',
        'models': SUNO_MODELS,
    },
}


def _provider_keyboard(request_id: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p['label']} — {p['desc']}",
                              callback_data=f"musicprov:{request_id}:{key}")]
        for key, p in PROVIDERS.items()
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"musiccancel:{request_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _model_keyboard(request_id: str, provider: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{info['label']} — {info['desc']}",
                              callback_data=f"musicsel:{request_id}:{provider}:{mk}")]
        for mk, info in PROVIDERS[provider]['models'].items()
    ]
    rows.append([
        InlineKeyboardButton(text='← Назад', callback_data=f"musicprovback:{request_id}"),
        InlineKeyboardButton(text='Отмена', callback_data=f"musiccancel:{request_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _task_header(data: dict) -> str:
    return f"<b>Задача:</b> {escape(data.get('prompt', '')[:500])}\n\n"


def _drop_user_state(uid: int) -> None:
    """Forget everything this user had pending, so only one flow is live."""
    for rid, d in list(_pending_music.items()):
        if d.get('user_id') == uid:
            _pending_music.pop(rid, None)
    for store, awaiting in ((pending_suno_configs, _suno_awaiting_input),
                            (pending_lyria_configs, _lyria_awaiting_input)):
        for rid, d in list(store.items()):
            if d.get('user_id') == uid:
                store.pop(rid, None)
        for key in [k for k, v in awaiting.items() if v.get('user_id', uid) == uid or k[1] == uid]:
            awaiting.pop(key, None)
    for key in [k for k in _music_awaiting_idea if k[1] == uid]:
        _music_awaiting_idea.pop(key, None)


def _reply_kwargs(thread_id) -> dict:
    return {'message_thread_id': thread_id} if thread_id else {}


async def _show_provider_screen(bot, request_id: str, uid: int) -> None:
    """Render the provider choice for an existing request."""
    d = _pending_music.get(request_id)
    if not d:
        return
    text = (_task_header(d) + "Через что делать?"
            "\n\n<b>Lyria</b> — быстро, свои настройки стиля и структуры."
            "\n<b>Suno</b> — песни с вокалом, свои настройки и модель.")
    kwargs = _reply_kwargs(d.get('message_thread_id'))
    if d.get('msg_id'):
        try:
            await bot.edit_message_text(chat_id=d['chat_id'], message_id=d['msg_id'],
                                        text=text, reply_markup=_provider_keyboard(request_id),
                                        parse_mode='HTML')
            return
        except Exception:
            pass
    sent = await safe_send(bot.send_message, chat_id=d['chat_id'], text=text,
                           reply_markup=_provider_keyboard(request_id), parse_mode='HTML', **kwargs)
    if sent:
        d['msg_id'] = sent.message_id


async def handle_music_idea(message: types.Message, reply_kwargs: dict) -> bool:
    """Consume the idea typed after a bare /music. Returns True if consumed."""
    key = (message.chat.id, message.from_user.id)
    if key not in _music_awaiting_idea:
        return False
    wait = _music_awaiting_idea.pop(key)
    request_id = wait['request_id']
    d = _pending_music.get(request_id)
    if not d:
        return False
    d['prompt'] = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    await _show_provider_screen(message.bot, request_id, message.from_user.id)
    return True


@music_router.message(Command("music"))
async def cmd_music(message: types.Message):
    """Handle /music — with a description it offers providers, bare it asks for one."""
    _track_user(message)
    uid = message.from_user.id

    args = message.text.strip()
    cmd_end = args.find(" ")
    prompt = args[cmd_end:].strip() if cmd_end != -1 else ""

    _drop_user_state(uid)

    request_id = uuid.uuid4().hex[:12]
    thread_id = message.message_thread_id if message.chat.is_forum else None
    _pending_music[request_id] = {
        'chat_id': message.chat.id,
        'user_id': uid,
        'prompt': prompt,
        'message_thread_id': thread_id,
    }

    reply_kwargs = _reply_kwargs(thread_id)

    if not prompt:
        # Bare /music: take the idea as the next message, then offer providers.
        _music_awaiting_idea[(message.chat.id, uid)] = {'request_id': request_id, 'user_id': uid}
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='❌ Отмена', callback_data=f'musiccancel:{request_id}')
        ]])
        sent = await safe_send(
            message.bot.send_message,
            chat_id=message.chat.id,
            text=("🎵 <b>Что за трек?</b>\n\n"
                  "Опиши жанр, настроение, инструменты или текст песни — пришли это сообщением.\n\n"
                  "Например: <code>мрачный synthwave без вокала</code>"),
            reply_markup=cancel_kb,
            parse_mode='HTML',
            **reply_kwargs,
        )
        if sent:
            _pending_music[request_id]['msg_id'] = sent.message_id
        return

    sent = await safe_send(
        message.bot.send_message,
        chat_id=message.chat.id,
        text=_task_header(_pending_music[request_id]) + "Через что делать?",
        reply_markup=_provider_keyboard(request_id),
        parse_mode='HTML',
        **reply_kwargs,
    )
    if sent:
        _pending_music[request_id]['msg_id'] = sent.message_id


def _resolve(callback: types.CallbackQuery, request_id: str):
    """Return the pending request for this callback, or None after answering."""
    d = _pending_music.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        return None
    return d


@music_router.callback_query(F.data.startswith("musicprovback:"))
async def music_provider_back_callback(callback: types.CallbackQuery):
    """Back from the model list to the provider choice."""
    request_id = callback.data.split(':', 1)[1]
    if not _resolve(callback, request_id):
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    await callback.answer()
    await _show_provider_screen(callback.message.bot, request_id, callback.from_user.id)


@music_router.callback_query(F.data.startswith("musiccancel:"))
async def music_cancel_callback(callback: types.CallbackQuery):
    request_id = callback.data.split(':', 1)[1]
    d = _pending_music.get(request_id)
    if not d:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer("Эту кнопку может нажать только автор запроса.", show_alert=True)
        return
    for store, awaiting in ((pending_suno_configs, _suno_awaiting_input),
                            (pending_lyria_configs, _lyria_awaiting_input)):
        store.pop(request_id, None)
        awaiting.pop((d.get('chat_id'), callback.from_user.id), None)
    _music_awaiting_idea.pop((d.get('chat_id'), callback.from_user.id), None)
    _pending_music.pop(request_id, None)
    await callback.answer()
    try:
        await callback.message.edit_text("🎵 Генерация отменена.")
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("musicprov:"))
async def music_provider_callback(callback: types.CallbackQuery):
    """Provider picked — list that provider's models."""
    _, request_id, provider = callback.data.split(':', 2)
    if not _resolve(callback, request_id):
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    if provider not in PROVIDERS:
        await callback.answer("Неизвестный провайдер.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_text(
            f"Выбери модель <b>{escape(PROVIDERS[provider]['label'])}</b>:",
            reply_markup=_model_keyboard(request_id, provider),
            parse_mode='HTML',
        )
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("musicsel:"))
async def music_model_callback(callback: types.CallbackQuery):
    """Model picked — open that provider's settings screen."""
    _, request_id, provider, model_key = callback.data.split(':', 3)
    data = _resolve(callback, request_id)
    if not data:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    if provider not in PROVIDERS or model_key not in PROVIDERS[provider]['models']:
        await callback.answer("Неизвестная модель.", show_alert=True)
        return

    info = PROVIDERS[provider]['models'][model_key]
    configs = pending_suno_configs if provider == 'suno' else pending_lyria_configs
    defaults = _SUNO_CFG_DEFAULTS if provider == 'suno' else _LYRIA_CFG_DEFAULTS
    render = _suno_cfg_text if provider == 'suno' else _lyria_cfg_text
    keyboard = _suno_cfg_keyboard if provider == 'suno' else _lyria_cfg_keyboard

    await callback.answer()
    configs[request_id] = {**data, 'model': model_key, 'provider': provider,
                           'label': info['label'], 'cfg': dict(defaults)}
    try:
        await callback.message.edit_text(render(request_id), reply_markup=keyboard(request_id),
                                         parse_mode='HTML')
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

_LYRIA_TEXT_FIELDS = {
    'style': '✏️ Пришли <b>стиль</b> (жанр, инструменты) следующим сообщением.\n\nПример: <code>sovietwave, аналоговые синты, тёплый ламповый звук</code>',
    'mood': '🎭 Пришли <b>настроение</b> следующим сообщением.\n\nПример: <code>nostalgic and melancholic</code>',
    'structure': '📝 Пришли <b>структуру</b> или текст следующим сообщением.\n\nПример: <code>[Verse]\\nгород спит\\n[Chorus]\\nмы горим</code>',
}

_SUNO_NUMERIC_FIELDS = {'style_weight': float, 'weirdness': float, 'audio_weight': float,
                        'variety': int, 'duration': int}


# Every settings callback is `<provider><suffix>:<request_id>:…`.
_SETTINGS_SUFFIXES = ('cfg', 'input', 'back', 'cancel', 'gen')


async def _settings_guard(callback: types.CallbackQuery):
    """Resolve the provider settings for this callback, answering errors itself.

    The provider is part of the callback prefix (`sunocfg:` → `suno`), so one
    handler set serves both providers instead of a closure per provider.
    """
    prefix, _, rest = callback.data.partition(':')
    provider = next((p for p in _SETTINGS if prefix.startswith(p)), None)
    if provider is None or prefix[len(provider):] not in _SETTINGS_SUFFIXES:
        await callback.answer("Неизвестный провайдер.", show_alert=True)
        return None, None, None
    request_id = rest.split(':', 1)[0]
    store = _SETTINGS[provider]['store']
    d = store.get(request_id)
    if not d:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return None, None, None
    if callback.from_user.id != d['user_id']:
        await callback.answer("Эту кнопку может нажать только автор запроса.", show_alert=True)
        return None, None, None
    return provider, request_id, d


async def _restore_model_screen(bot, request_id: str, provider: str) -> None:
    """Back out of a settings screen to that provider's model list."""
    d = _SETTINGS[provider]['store'].pop(request_id, None)
    if not d:
        return
    _pending_music[request_id] = {k: v for k, v in d.items()
                                  if k not in ('cfg', 'label', 'model', 'provider')}
    await _edit_or_send(bot, d, request_id,
                        _task_header(d) + f"Выбери модель <b>{escape(PROVIDERS[provider]['label'])}</b>:",
                        _model_keyboard(request_id, provider))


async def _edit_or_send(bot, d: dict, request_id: str, text: str, keyboard) -> None:
    try:
        await bot.edit_message_text(chat_id=d['chat_id'], message_id=d.get('msg_id'),
                                    text=text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        sent = await safe_send(bot.send_message, chat_id=d['chat_id'], text=text,
                               reply_markup=keyboard, parse_mode='HTML')
        if sent:
            d['msg_id'] = sent.message_id


_SETTINGS = {
    'suno': {
        'store': pending_suno_configs,
        'awaiting': _suno_awaiting_input,
        'text_fields': _SUNO_TEXT_FIELDS,
        'numeric': _SUNO_NUMERIC_FIELDS,
        'render': _suno_cfg_text,
        'keyboard': _suno_cfg_keyboard,
    },
    'lyria': {
        'store': pending_lyria_configs,
        'awaiting': _lyria_awaiting_input,
        'text_fields': _LYRIA_TEXT_FIELDS,
        'numeric': {},
        'render': _lyria_cfg_text,
        'keyboard': _lyria_cfg_keyboard,
    },
}


@music_router.callback_query(F.data.regexp(r"^(suno|lyria)cfg:"))
async def music_settings_callback(callback: types.CallbackQuery):
    """Toggle a setting on either provider's screen."""
    parts = callback.data.split(':', 3)
    if len(parts) != 4:
        await callback.answer()
        return
    (_, request_id, field, value) = parts
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    convert = _SETTINGS[provider]['numeric'].get(field)
    d['cfg'][field] = convert(value) if convert else value
    await callback.answer()
    await _edit_or_send(callback.message.bot, d, request_id,
                        _SETTINGS[provider]['render'](request_id),
                        _SETTINGS[provider]['keyboard'](request_id))


@music_router.callback_query(F.data.regexp(r"^(suno|lyria)input:"))
async def music_settings_input_callback(callback: types.CallbackQuery):
    """Ask the user to type a text value for one setting."""
    parts = callback.data.split(':', 2)
    if len(parts) != 3:
        await callback.answer()
        return
    (_, request_id, field) = parts
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    await callback.answer()
    _SETTINGS[provider]['awaiting'][(d['chat_id'], d['user_id'])] = {
        'request_id': request_id, 'field': field, 'msg_id': d.get('msg_id'),
    }
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='❌ Отмена', callback_data=f'{provider}cancel:{request_id}')
    ]])
    await _edit_or_send(
        callback.message.bot, d, request_id,
        _SETTINGS[provider]['text_fields'].get(field, '✏️ Пришли значение следующим сообщением:'),
        cancel_kb,
    )


@music_router.callback_query(F.data.regexp(r"^(suno|lyria)back:"))
async def music_settings_back_callback(callback: types.CallbackQuery):
    """Back from settings to the provider's model list."""
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    _SETTINGS[provider]['awaiting'].pop((d['chat_id'], d['user_id']), None)
    await callback.answer()
    await _restore_model_screen(callback.message.bot, request_id, provider)


@music_router.callback_query(F.data.regexp(r"^(suno|lyria)cancel:"))
async def music_settings_cancel_callback(callback: types.CallbackQuery):
    """Cancel a settings screen — drops the whole request."""
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    _SETTINGS[provider]['awaiting'].pop((d['chat_id'], d['user_id']), None)
    _SETTINGS[provider]['store'].pop(request_id, None)
    _pending_music.pop(request_id, None)
    await callback.answer()
    try:
        await callback.message.edit_text("🎵 Генерация отменена.")
    except Exception:
        pass


def _cooldown_remaining(uid: int) -> int:
    now = asyncio.get_event_loop().time()
    if uid in _cooldowns and now - _cooldowns[uid] < _COOLDOWN:
        return int(_COOLDOWN - (now - _cooldowns[uid]))
    return 0


@music_router.callback_query(F.data.startswith("sunogen:"))
async def suno_generate_callback(callback: types.CallbackQuery):
    """Generate with Suno using the collected settings."""
    _, request_id, d = await _settings_guard(callback)
    if not d:
        return
    remaining = _cooldown_remaining(d['user_id'])
    if remaining:
        await callback.answer(f"Кулдаун {remaining}с", show_alert=True)
        return
    await callback.answer()
    _cooldowns[d['user_id']] = asyncio.get_event_loop().time()
    _suno_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    pending_suno_configs.pop(request_id, None)
    _pending_music.pop(request_id, None)

    async def _status(text: str):
        try:
            await callback.message.edit_text(text)
        except Exception:
            pass

    try:
        await callback.message.edit_text(
            f"🎼 <b>Генерирую через {escape(d['label'])}...</b>\n\n"
            "Suno обычно отдаёт два варианта, это занимает минуту-две.", parse_mode="HTML")
    except Exception:
        pass

    tracks, error = await generate_suno(d['prompt'], model_key=d['model'], cfg=d['cfg'], status_cb=_status)

    reply_kwargs = _reply_kwargs(d.get('message_thread_id'))

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
        parts = [f"<b>{escape(title)}</b>"]
        meta = []
        if track['duration']:
            meta.append(f"⏱ {int(float(track['duration']))}с")
        meta.append(escape(d['label']))
        parts.append(' · '.join(meta))
        if track['tags']:
            parts.append(f"🎼 {escape(track['tags'][:220])}")
        if track['prompt']:
            lyrics = track['prompt'].strip()
            if lyrics:
                parts.append(f"📝 <blockquote expandable>{escape(lyrics[:700])}</blockquote>")

        kwargs = {}
        if track.get('cover'):
            kwargs['thumbnail'] = BufferedInputFile(track['cover'], filename='cover.jpg')
        try:
            await safe_send(
                callback.message.bot.send_audio,
                chat_id=d['chat_id'],
                audio=BufferedInputFile(track['audio'], filename=f"suno_{request_id}_{idx}.mp3"),
                caption='\n'.join(parts)[:1020],
                title=title[:64],
                performer='Suno',
                parse_mode="HTML",
                **kwargs,
                **reply_kwargs,
            )
        except Exception as e:
            logger.warning(f"Suno track {idx} send failed: {type(e).__name__}: {e}")
            await safe_send(callback.message.bot.send_message, chat_id=d['chat_id'],
                            text=f'❌ Не смог отправить трек {idx}: {type(e).__name__}', **reply_kwargs)


@music_router.callback_query(F.data.startswith("lyriagen:"))
async def lyria_generate_callback(callback: types.CallbackQuery):
    """Generate with Lyria using the collected settings."""
    _, request_id, d = await _settings_guard(callback)
    if not d:
        return
    remaining = _cooldown_remaining(d['user_id'])
    if remaining:
        await callback.answer(f"Кулдаун {remaining}с", show_alert=True)
        return
    await callback.answer()
    _cooldowns[d['user_id']] = asyncio.get_event_loop().time()
    _lyria_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    pending_lyria_configs.pop(request_id, None)
    _pending_music.pop(request_id, None)

    model_info = MUSIC_MODELS[d['model']]
    # '🎵 Lyria 3.5' -> 'Lyria 3.5' for the audio metadata
    plain_label = model_info['label'].split(' ', 1)[-1]
    try:
        await callback.message.edit_text(
            f"🎵 <b>Генерирую музыку...</b>\n"
            f"Модель: {escape(str(model_info['label']))}\n"
            f"Промпт: {escape(d['prompt'][:300])}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    audio_bytes, lyrics, error = await generate_music(
        prompt=d['prompt'], model_key=d['model'], cfg=d['cfg'],
    )

    if error:
        try:
            await callback.message.edit_text(
                f"🎵 <b>Ошибка генерации:</b>\n{escape(str(error))}", parse_mode="HTML")
        except Exception:
            pass
        return

    reply_kwargs = _reply_kwargs(d.get('message_thread_id'))

    if not audio_bytes:
        try:
            await callback.message.edit_text("🎵 Пустой ответ от модели.")
        except Exception:
            pass
        return

    caption = ""
    if lyrics and not lyrics.strip().startswith(('<', '[[')):
        caption = f"<b>Текст песни:</b>\n<blockquote expandable>{escape(lyrics[:1000])}</blockquote>"

    try:
        await callback.message.delete()
    except Exception:
        pass

    await safe_send(
        callback.message.bot.send_audio,
        chat_id=d['chat_id'],
        caption=caption or "🎵",
        audio=BufferedInputFile(audio_bytes, filename=f"lyria_{d['model']}_{request_id}.mp3"),
        title=plain_label,
        performer='Hatani AI',
        parse_mode="HTML",
        **reply_kwargs,
    )
