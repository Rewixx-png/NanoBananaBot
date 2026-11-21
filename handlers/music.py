"""Music generation command handler — /music.

Two levels: pick a provider (Suno or Lyria by Google), then a model of that
provider, then that provider's own settings screen.
"""

import asyncio
import logging
import re
import uuid
from html import escape

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
)

from config import SUNO_LYRICS_MAX_CHARS
from handlers.common import (
    safe_send,
    _track_user,
    _suno_cfg_text,
    _suno_cfg_keyboard,
    _suno_quick_text,
    _suno_quick_keyboard,
    _SUNO_CFG_DEFAULTS,
    _lyria_cfg_text,
    _lyria_cfg_keyboard,
    _LYRIA_CFG_DEFAULTS,
)
from handlers.music_ui import (
    field_input_keyboard,
    field_input_text,
    lyria_progress_text,
    lyrics_collect_keyboard as _lyrics_collect_keyboard,
    lyrics_collect_text as _lyrics_collect_text,
    music_ask_idea_keyboard,
    music_ask_idea_text,
    music_edit_idea_text,
    music_model_keyboard,
    music_model_text,
    music_provider_keyboard,
    music_provider_text,
    suno_cancel_keyboard as _suno_cancel_keyboard,
    suno_meta_keyboard as _suno_meta_keyboard,
    suno_meta_text,
    suno_progress_text,
    tg_icon,
    track_tags_keyboard,
    ui_button,
    _cut,
    _fmt_duration,
)
from services.audio_metadata import embed_audio_metadata
from services.music_service import generate_music, MUSIC_MODELS
from services.suno_service import SUNO_MODELS, generate_suno
from state import pending_suno_configs, pending_lyria_configs
from database.suno import get_suno_personas, get_suno_voices, save_suno_track
from handlers.music_studio import (
    _studio_awaiting,
    drop_studio_user_state,
    music_studio_router,
)
from handlers.music_studio_ui import (
    suno_refs_keyboard,
    suno_refs_text,
    track_compact_keyboard,
)
from services.suno_service import boost_music_style


logger = logging.getLogger(__name__)

music_router = Router()
music_router.include_router(music_studio_router)

# Cooldown (per-user, 20 seconds)
_cooldowns: dict[int, float] = {}
_COOLDOWN = 20

# Root request: request_id -> {chat_id, user_id, prompt, message_thread_id, msg_id}
_pending_music: dict[str, dict] = {}

# Users typing a field value: (chat_id, user_id) -> {request_id, field, msg_id}
_suno_awaiting_input: dict[tuple[int, int], dict] = {}
_lyria_awaiting_input: dict[tuple[int, int], dict] = {}
# Users typing the song idea after a bare /music, before a request exists.
_music_awaiting_idea: dict[tuple[int, int], dict] = {}

# Active Suno polls: request_id -> {event: asyncio.Event, user_id: int}
_suno_running: dict[str, dict] = {}

# Generated tracks waiting for the user to confirm metadata.
_suno_meta: dict[str, dict] = {}


class SunoMetaState(StatesGroup):
    title = State()
    author = State()
    lyrics = State()


PROVIDERS = {
    'suno': {
        'label': 'Suno V6',
        'desc': 'песня с вокалом',
        'models': SUNO_MODELS,
    },
    'lyria': {
        'label': 'Lyria by Google',
        'desc': 'инструментал',
        'models': MUSIC_MODELS,
    },
}


def _provider_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return music_provider_keyboard(request_id, PROVIDERS)


def _model_keyboard(request_id: str, provider: str) -> InlineKeyboardMarkup:
    return music_model_keyboard(request_id, provider, PROVIDERS[provider]['models'])


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
    drop_studio_user_state(uid)


def _reply_kwargs(thread_id) -> dict:
    return {'message_thread_id': thread_id} if thread_id else {}


_META_PROMPTS = {
    'title': (
        f"{tg_icon('pencil', '✎')} <b>Название трека</b>\n\n"
        'Пришли новое название.\n'
        'Одно — для обоих вариантов, два через <code>|</code> — раздельно.\n'
        '<i>/cancel — отменить</i>'
    ),
    'author': (
        f"{tg_icon('pencil', '✎')} <b>Имя автора</b>\n\n"
        'Пришли имя исполнителя.\n'
        'Одно — для обоих вариантов, два через <code>|</code> — раздельно.\n'
        '<i>/cancel — отменить</i>'
    ),
    'lyrics': (
        f"{tg_icon('pencil', '✎')} <b>Текст песни (теги USLT)</b>\n\n"
        'Пришли текст одним сообщением — запишется прямо в ID3-метаданные MP3.\n'
        '<i>/cancel — отменить</i>'
    ),
}
_META_STATES = {
    'title': SunoMetaState.title,
    'author': SunoMetaState.author,
    'lyrics': SunoMetaState.lyrics,
}
_META_NEXT = {'title': 'author', 'author': 'lyrics', 'lyrics': None}


def _split_meta_values(text: str, limit: int = 64) -> list[str]:
    values = [part.strip() for part in (text or '').split('|') if part.strip()]
    return [value[:limit] for value in values[:2]]


def _meta_value(values: list[str] | None, idx: int, fallback: str) -> str:
    if not values:
        return fallback
    return values[0] if len(values) == 1 else values[min(idx, len(values) - 1)]


async def _edit_suno_meta_message(bot, meta: dict, text: str, keyboard=None) -> None:
    try:
        await bot.edit_message_text(
            chat_id=meta['chat_id'], message_id=meta.get('msg_id'),
            text=text, reply_markup=keyboard, parse_mode='HTML',
        )
    except Exception:
        sent = await safe_send(
            bot.send_message, chat_id=meta['chat_id'], text=text,
            reply_markup=keyboard, parse_mode='HTML',
            **_reply_kwargs(meta.get('thread_id')),
        )
        if sent:
            meta['msg_id'] = sent.message_id


async def _ask_suno_meta(bot, meta: dict, field: str) -> None:
    meta['stage'] = field
    await _edit_suno_meta_message(
        bot, meta, suno_meta_text(meta, field),
        _suno_meta_keyboard(meta['request_id'], field),
    )


async def _send_suno_result(bot, meta: dict, state: FSMContext) -> None:
    """Send the two tracks and lyrics only after the metadata wizard."""
    _suno_meta.pop(meta['request_id'], None)
    await state.clear()
    try:
        await bot.delete_message(chat_id=meta['chat_id'], message_id=meta.get('msg_id'))
    except Exception:
        pass

    reply_kwargs = _reply_kwargs(meta.get('thread_id'))
    titles = meta.get('titles')
    authors = meta.get('authors')
    lyrics_override = meta.get('lyrics')
    sent_lyrics: set[str] = set()

    for idx, track in enumerate(meta['tracks'], 1):
        title = _meta_value(titles, idx - 1, track.get('title') or f'Suno {idx}')
        performer = _meta_value(authors, idx - 1, 'Suno')
        lyrics = lyrics_override if lyrics_override is not None else (track.get('prompt') or '').strip()
        tagged_audio = embed_audio_metadata(
            track['audio'],
            title=title,
            artist=performer,
            lyrics=lyrics,
        )
        if tagged_audio is not track['audio']:
            logger.info(f"Suno track {idx}: ID3 title/artist/lyrics embedded")

        parts = [f"<b>{escape(title)}</b>"]
        meta_parts = []
        if track.get('duration'):
            meta_parts.append(f"<code>{_fmt_duration(int(float(track['duration'])))}</code>")
        meta_parts.append(escape(str(meta.get('label') or 'Suno')))
        parts.append(' · '.join(meta_parts))
        if track.get('tags'):
            parts.append(f"<i>{escape(_cut(str(track['tags']), 220))}</i>")

        track_db_id = None
        if track.get('task_id') and track.get('id'):
            try:
                track_db_id = await save_suno_track(
                    meta['user_id'], meta['chat_id'],
                    str(track['task_id']), str(track['id']),
                    title=title, tags=str(track.get('tags') or ''),
                    duration=float(track.get('duration') or 0),
                    model=str(meta.get('model_id') or 'V6'),
                    prompt=lyrics, audio_url=str(track.get('audio_url') or ''),
                )
            except Exception as db_err:
                logger.warning(f"Suno track DB save failed: {db_err}")

        kwargs = {}
        if track.get('cover'):
            kwargs['thumbnail'] = BufferedInputFile(track['cover'], filename='cover.jpg')
        tags_kb = track_compact_keyboard(track_db_id, track.get('tags', '')) if track_db_id else track_tags_keyboard(track.get('tags', ''))
        if tags_kb:
            kwargs['reply_markup'] = tags_kb
        try:
            await safe_send(
                bot.send_audio,
                chat_id=meta['chat_id'],
                audio=BufferedInputFile(tagged_audio, filename=f"suno_{meta['request_id']}_{idx}.mp3"),
                caption='\n'.join(parts)[:1020],
                title=title[:64],
                performer=performer[:64],
                parse_mode='HTML',
                **kwargs,
                **reply_kwargs,
            )
        except Exception as e:
            logger.warning(f"Suno track {idx} send failed: {type(e).__name__}: {e}")
            await safe_send(bot.send_message, chat_id=meta['chat_id'],
                            text=f'Не удалось отправить трек {idx}.', **reply_kwargs)

        if lyrics and lyrics not in sent_lyrics:
            lyrics_title = titles[0] if titles else (track.get('title') or f'Suno {idx}')
            await _send_lyrics(bot, meta['chat_id'], meta.get('thread_id'), lyrics_title, lyrics)
            sent_lyrics.add(lyrics)


async def _cancel_suno_meta(message: types.Message, state: FSMContext, meta: dict) -> None:
    _suno_meta.pop(meta['request_id'], None)
    await state.clear()
    await _edit_suno_meta_message(message.bot, meta, 'Изменение метаданных отменено.')


async def _expire_suno_meta(request_id: str, seconds: int = 1800) -> None:
    await asyncio.sleep(seconds)
    _suno_meta.pop(request_id, None)


async def _show_provider_screen(bot, request_id: str, uid: int) -> None:
    """Render the provider choice for an existing request."""
    d = _pending_music.get(request_id)
    if not d:
        return
    text = music_provider_text(d.get('prompt', ''))
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
        _music_awaiting_idea[(message.chat.id, uid)] = {'request_id': request_id, 'user_id': uid}
        sent = await safe_send(
            message.bot.send_message,
            chat_id=message.chat.id,
            text=music_ask_idea_text(),
            reply_markup=music_ask_idea_keyboard(request_id),
            parse_mode='HTML',
            **reply_kwargs,
        )
        if sent:
            _pending_music[request_id]['msg_id'] = sent.message_id
        return

    sent = await safe_send(
        message.bot.send_message,
        chat_id=message.chat.id,
        text=music_provider_text(prompt),
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
    """Back from the model list or idea editor to the provider choice."""
    request_id = callback.data.split(':', 1)[1]
    d = _resolve(callback, request_id)
    if not d:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    _music_awaiting_idea.pop((d['chat_id'], d['user_id']), None)
    await callback.answer()
    if not (d.get('prompt') or '').strip():
        _music_awaiting_idea[(d['chat_id'], d['user_id'])] = {'request_id': request_id, 'user_id': d['user_id']}
        try:
            await callback.message.edit_text(
                music_ask_idea_text(),
                reply_markup=music_ask_idea_keyboard(request_id),
                parse_mode='HTML',
            )
        except Exception:
            pass
        return
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
        await callback.message.edit_text("Генерация отменена.")
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("musicedit:"))
async def music_edit_idea_callback(callback: types.CallbackQuery):
    """Let the user rewrite the idea without restarting /music."""
    request_id = callback.data.split(':', 1)[1]
    d = _pending_music.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    await callback.answer()
    _music_awaiting_idea[(d['chat_id'], d['user_id'])] = {
        'request_id': request_id, 'user_id': d['user_id'],
    }
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        ui_button('← Назад', callback_data=f'musicprovback:{request_id}'),
        ui_button('Отмена', callback_data=f'musiccancel:{request_id}'),
    ]])
    try:
        await callback.message.edit_text(
            music_edit_idea_text(d.get('prompt', '')),
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("musicprov:"))
async def music_provider_callback(callback: types.CallbackQuery):
    """Provider picked — list that provider's models."""
    _, request_id, provider = callback.data.split(':', 2)
    d = _resolve(callback, request_id)
    if not d:
        await callback.answer("Запрос устарел. Отправь /music заново.", show_alert=True)
        return
    if provider not in PROVIDERS:
        await callback.answer("Неизвестный провайдер.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_text(
            music_model_text(d.get('prompt', ''), PROVIDERS[provider]['label']),
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

    await callback.answer()
    entry = {**data, 'model': model_key, 'provider': provider,
             'label': info['label'], 'cfg': dict(defaults)}
    if provider == 'suno':
        entry['view'] = 'quick'
    configs[request_id] = entry
    render, keyboard = _render_settings(provider, request_id)
    try:
        await callback.message.edit_text(render, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        pass


# ── Settings screens (Suno & Lyria) ──────────────────────────────────────
_SUNO_TEXT_FIELDS = {
    'style': f"{tg_icon('pencil', '✎')} <b>Стиль</b>\nПришли жанр, инструменты и подачу.\n\n<i>Пример:</i> <code>мрачный synthwave, аналоговые синты, 118 bpm</code>",
    'lyrics': f"{tg_icon('pencil', '✎')} <b>Текст песни</b>\nПришли текст с тегами <code>[Verse]</code>, <code>[Chorus]</code>, <code>[Bridge]</code>.",
    'title': f"{tg_icon('pencil', '✎')} <b>Название трека</b>\nПришли название до 80 символов.",
    'neg': f"{tg_icon('pencil', '✎')} <b>Исключить из звука</b>\nУкажи жанры или инструменты, которых не должно быть.\n\n<i>Пример:</i> <code>heavy metal, drums, distorted guitar</code>",
}

_LYRIA_TEXT_FIELDS = {
    'style': f"{tg_icon('pencil', '✎')} <b>Стиль</b>\nПришли жанр и инструменты.\n\n<i>Пример:</i> <code>sovietwave, аналоговые синты, тёплая плёнка</code>",
    'mood': f"{tg_icon('pencil', '✎')} <b>Настроение</b>\nПришли атмосферу трека.\n\n<i>Пример:</i> <code>nostalgic and melancholic</code>",
    'structure': f"{tg_icon('pencil', '✎')} <b>Структура / текст</b>\nПришли секции или строки.\n\n<i>Пример:</i> <code>[Verse] город спит [Chorus] мы горим</code>",
}

_SUNO_NUMERIC_FIELDS = {'style_weight': float, 'weirdness': float, 'audio_weight': float,
                        'variety': int, 'duration': int}

_SETTINGS_SUFFIXES = ('cfg', 'input', 'inputback', 'clearfield', 'reset', 'back', 'cancel', 'gen', 'all', 'quick')


async def _settings_guard(callback: types.CallbackQuery):
    """Resolve the provider settings for this callback, answering errors itself."""
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
                                  if k not in ('cfg', 'label', 'model', 'provider', 'view')}
    await _edit_or_send(bot, d, request_id,
                        music_model_text(d.get('prompt', ''), PROVIDERS[provider]['label']),
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


def _render_settings(provider: str, request_id: str):
    """Return (text, keyboard) for the current Suno/Lyria view."""
    if provider == 'suno':
        d = pending_suno_configs.get(request_id, {})
        if d.get('view') == 'all':
            return _suno_cfg_text(request_id), _suno_cfg_keyboard(request_id)
        return _suno_quick_text(request_id), _suno_quick_keyboard(request_id)
    return _lyria_cfg_text(request_id), _lyria_cfg_keyboard(request_id)


def _append_lyrics(buffer: str, chunk: str,
                   limit: int = SUNO_LYRICS_MAX_CHARS) -> tuple[str, bool]:
    """Append one message/file to the lyrics buffer. Returns (buffer, truncated)."""
    buffer = buffer or ''
    chunk = (chunk or '').strip()
    if not chunk:
        return buffer, False
    separator = '\n' if buffer else ''
    room = limit - len(buffer) - len(separator)
    if room <= 0:
        return buffer, True
    addition = chunk[:room]
    return buffer + separator + addition, len(addition) < len(chunk)


async def _send_lyrics(bot, chat_id: int, thread_id, title: str, lyrics: str) -> None:
    """Send the full lyrics separately from the audio caption."""
    text = (lyrics or '').strip()
    if not text:
        return
    title = (title or 'Текст песни').strip()[:120]
    safe_title = escape(title)
    safe_text = escape(text)
    reply_kwargs = _reply_kwargs(thread_id)
    if len(safe_title) + len(safe_text) <= 3800:
        html = (f"<b>Текст · {safe_title}</b>\n"
                f"<blockquote expandable>{safe_text}</blockquote>")
        await safe_send(bot.send_message, chat_id=chat_id, text=html,
                        parse_mode='HTML', **reply_kwargs)
    else:
        document = BufferedInputFile(text.encode('utf-8'), filename='lyrics.txt')
        await safe_send(bot.send_document, chat_id=chat_id, document=document,
                        caption=f"Текст · {title}"[:1024], **reply_kwargs)


_SETTINGS = {
    'suno': {
        'store': pending_suno_configs,
        'awaiting': _suno_awaiting_input,
        'text_fields': _SUNO_TEXT_FIELDS,
        'numeric': _SUNO_NUMERIC_FIELDS,
    },
    'lyria': {
        'store': pending_lyria_configs,
        'awaiting': _lyria_awaiting_input,
        'text_fields': _LYRIA_TEXT_FIELDS,
        'numeric': {},
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
    text, keyboard = _render_settings(provider, request_id)
    await _edit_or_send(callback.message.bot, d, request_id, text, keyboard)


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
    if provider == 'suno' and field == 'lyrics':
        buffer = d['cfg'].get('lyrics') or ''
        _suno_awaiting_input[(d['chat_id'], d['user_id'])] = {
            'request_id': request_id, 'field': 'lyrics', 'msg_id': d.get('msg_id'),
            'multi': True, 'buffer': buffer,
        }
        await _edit_or_send(
            callback.message.bot, d, request_id,
            _lyrics_collect_text(buffer),
            _lyrics_collect_keyboard(request_id, bool(buffer)),
        )
        return
    _SETTINGS[provider]['awaiting'][(d['chat_id'], d['user_id'])] = {
        'request_id': request_id, 'field': field, 'msg_id': d.get('msg_id'),
    }
    current_val = str(d['cfg'].get(field) or '')
    base_prompt = _SETTINGS[provider]['text_fields'].get(field, 'Пришли значение следующим сообщением:')
    await _edit_or_send(
        callback.message.bot, d, request_id,
        field_input_text(base_prompt, current_val),
        field_input_keyboard(provider, request_id, field, bool(current_val.strip())),
    )


@music_router.callback_query(F.data.regexp(r"^(suno|lyria)inputback:"))
async def music_settings_input_back_callback(callback: types.CallbackQuery):
    """Return from field input to settings without cancelling the request."""
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    _SETTINGS[provider]['awaiting'].pop((d['chat_id'], d['user_id']), None)
    await callback.answer()
    text, keyboard = _render_settings(provider, request_id)
    await _edit_or_send(callback.message.bot, d, request_id, text, keyboard)


@music_router.callback_query(F.data.regexp(r"^(suno|lyria)clearfield:"))
async def music_settings_clear_field_callback(callback: types.CallbackQuery):
    """Clear a single text field and return to settings."""
    parts = callback.data.split(':', 2)
    if len(parts) != 3:
        await callback.answer()
        return
    (_, _, field) = parts
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    _SETTINGS[provider]['awaiting'].pop((d['chat_id'], d['user_id']), None)
    if field in d['cfg']:
        d['cfg'][field] = ''
    await callback.answer("Очищено")
    text, keyboard = _render_settings(provider, request_id)
    await _edit_or_send(callback.message.bot, d, request_id, text, keyboard)


@music_router.callback_query(F.data.startswith("sunoreset:"))
async def music_suno_reset_callback(callback: types.CallbackQuery):
    """Reset all Suno settings back to defaults."""
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    _suno_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    d['cfg'] = dict(_SUNO_CFG_DEFAULTS)
    await callback.answer("Настройки сброшены")
    text, keyboard = _render_settings(provider, request_id)
    await _edit_or_send(callback.message.bot, d, request_id, text, keyboard)


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
        await callback.message.edit_text("Генерация отменена.")
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("sunoall:"))
async def music_suno_all_callback(callback: types.CallbackQuery):
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    d['view'] = 'all'
    await callback.answer()
    await _edit_or_send(callback.message.bot, d, request_id,
                        _suno_cfg_text(request_id), _suno_cfg_keyboard(request_id))


@music_router.callback_query(F.data.startswith("sunoquick:"))
async def music_suno_quick_callback(callback: types.CallbackQuery):
    provider, request_id, d = await _settings_guard(callback)
    if not d:
        return
    d['view'] = 'quick'
    await callback.answer()
    await _edit_or_send(callback.message.bot, d, request_id,
                        _suno_quick_text(request_id), _suno_quick_keyboard(request_id))



@music_router.callback_query(F.data.startswith("sunoboost:"))
async def music_suno_boost_callback(callback: types.CallbackQuery):
    """Boost the current Suno style via POST /api/v1/style/generate."""
    request_id = callback.data.split(':', 1)[1]
    d = pending_suno_configs.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        await callback.answer("Запрос устарел. Открой /music заново.", show_alert=True)
        return
    base = (d['cfg'].get('style') or d.get('prompt') or '').strip()
    if not base:
        await callback.answer("Сначала задай стиль или идею.", show_alert=True)
        return
    await callback.answer("Усиливаю стиль…")
    data, err = await boost_music_style(base)
    if err or not data or not (data.get('result') or '').strip():
        await callback.answer(f"Ошибка: {str(err or 'пусто')[:140]}", show_alert=True)
        return
    d['cfg']['style'] = str(data['result']).strip()[:1000]
    text, kb = _render_settings('suno', request_id)
    await _edit_or_send(callback.message.bot, d, request_id, text, kb)


@music_router.callback_query(F.data.startswith("sunopersona:"))
async def music_suno_persona_callback(callback: types.CallbackQuery):
    """Pick a saved style Persona or custom Suno Voice for this generation."""
    request_id = callback.data.split(':', 1)[1]
    d = pending_suno_configs.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    await callback.answer()
    personas = await get_suno_personas(d['user_id'])
    voices = await get_suno_voices(d['user_id'])
    rows = []
    if d['cfg'].get('persona_id'):
        rows.append([ui_button("Убрать персону / голос", callback_data=f"sunosetp:{request_id}:clear", style="danger")])
    for v in voices[:6]:
        rows.append([ui_button(f"Голос · {_cut(v['name'], 26)}", callback_data=f"sunosetp:{request_id}:v:{v['id']}")])
    for p in personas[:6]:
        rows.append([ui_button(f"Персона · {_cut(p['name'], 26)}", callback_data=f"sunosetp:{request_id}:p:{p['id']}")])
    rows.append([ui_button("← Все настройки", callback_data=f"sunoall:{request_id}")])
    msg = (
        f"{tg_icon('microphone', '🎙')} <b>Выбор персоны или голоса</b>\n\nВыбери профиль:"
        if (personas or voices) else
        f"{tg_icon('microphone', '🎙')} <b>Персоны и голоса</b>\n\nПока пусто. Создай персону из готового трека или обучи голос в «Suno Студия»."
    )
    await _edit_or_send(callback.message.bot, d, request_id, msg, InlineKeyboardMarkup(inline_keyboard=rows))


@music_router.callback_query(F.data.startswith("sunosetp:"))
async def music_suno_set_persona_callback(callback: types.CallbackQuery):
    parts = callback.data.split(':', 4)
    request_id = parts[1]
    d = pending_suno_configs.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    kind = parts[2]
    if kind == 'clear':
        d['cfg'].pop('persona_id', None)
        d['cfg'].pop('persona_model', None)
        d['cfg'].pop('persona_label', None)
    elif kind == 'v' and len(parts) >= 4:
        voices = await get_suno_voices(d['user_id'])
        v = next((x for x in voices if str(x['id']) == parts[3]), None)
        if v:
            d['cfg']['persona_id'] = v['voice_id']
            d['cfg']['persona_model'] = 'voice_persona'
            d['cfg']['persona_label'] = f"Голос: {v['name']}"
    elif kind == 'p' and len(parts) >= 4:
        personas = await get_suno_personas(d['user_id'])
        p = next((x for x in personas if str(x['id']) == parts[3]), None)
        if p:
            d['cfg']['persona_id'] = p['persona_id']
            d['cfg']['persona_model'] = 'style_persona'
            d['cfg']['persona_label'] = f"Персона: {p['name']}"
    await callback.answer()
    d['view'] = 'all'
    text, kb = _render_settings('suno', request_id)
    await _edit_or_send(callback.message.bot, d, request_id, text, kb)


@music_router.callback_query(F.data.startswith("sunorefs:"))
async def music_suno_refs_callback(callback: types.CallbackQuery):
    request_id = callback.data.split(':', 1)[1]
    d = pending_suno_configs.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    _studio_awaiting.pop((d['chat_id'], d['user_id']), None)
    await callback.answer()
    await _edit_or_send(callback.message.bot, d, request_id, suno_refs_text(request_id, d), suno_refs_keyboard(request_id, d))


@music_router.callback_query(F.data.startswith("sunoref:"))
async def music_suno_ref_add_callback(callback: types.CallbackQuery):
    _, request_id, ref_kind = callback.data.split(':', 2)
    d = pending_suno_configs.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    await callback.answer()
    _studio_awaiting[(d['chat_id'], d['user_id'])] = {
        'mode': 'suno_ref_upload', 'request_id': request_id, 'ref_kind': ref_kind,
        'msg_id': d.get('msg_id'), 'thread_id': d.get('message_thread_id'),
    }
    hints = {
        'image': "Пришли фото (jpeg/png/webp/bmp, до 10 МБ) или прямую ссылку.",
        'video': "Пришли видео (mp4/mov/webm, до 100 МБ, до 241 сек) или ссылку.",
        'audio': "Пришли аудио/голосовое (6 сек – 30 мин) или ссылку.",
        'midi': "Пришли MIDI-файл (.mid/.midi, до 256 КБ) или ссылку.",
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Референсы", callback_data=f"sunorefs:{request_id}")]])
    await _edit_or_send(callback.message.bot, d, request_id, f"{tg_icon('attachment', '📎')} <b>Референс ({ref_kind})</b>\n\n{hints.get(ref_kind, 'Пришли файл:')}", kb)


@music_router.callback_query(F.data.startswith("sunorefclear:"))
async def music_suno_ref_clear_callback(callback: types.CallbackQuery):
    request_id = callback.data.split(':', 1)[1]
    d = pending_suno_configs.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    for k in ('image_urls', 'video_urls', 'audio_urls', 'midi_urls'):
        d['cfg'].pop(k, None)
    d['cfg']['custom_mode'] = '1'
    await callback.answer("Референсы очищены")
    await _edit_or_send(callback.message.bot, d, request_id, suno_refs_text(request_id, d), suno_refs_keyboard(request_id, d))

def _suno_lyrics_guard(callback: types.CallbackQuery):
    request_id = callback.data.split(':', 1)[1]
    d = pending_suno_configs.get(request_id)
    if not d or callback.from_user.id != d['user_id']:
        return None, None, None
    wait = _suno_awaiting_input.get((d['chat_id'], d['user_id']))
    if not wait or not wait.get('multi'):
        return None, None, None
    return request_id, d, wait


@music_router.callback_query(F.data.startswith("sunolyrdone:"))
async def music_lyrics_done_callback(callback: types.CallbackQuery):
    request_id, d, wait = _suno_lyrics_guard(callback)
    if not d:
        await callback.answer("Запрос устарел. Открой /music заново.", show_alert=True)
        return
    text = (wait.get('buffer') or '').strip()
    d['cfg']['lyrics'] = text
    _suno_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    await callback.answer("Текст сохранён." if text else "Текст удалён.")
    render, keyboard = _render_settings('suno', request_id)
    await _edit_or_send(callback.message.bot, d, request_id, render, keyboard)


@music_router.callback_query(F.data.startswith("sunolyrclear:"))
async def music_lyrics_clear_callback(callback: types.CallbackQuery):
    request_id, d, wait = _suno_lyrics_guard(callback)
    if not d:
        await callback.answer("Запрос устарел. Открой /music заново.", show_alert=True)
        return
    wait['buffer'] = ''
    await callback.answer("Буфер очищен")
    await _edit_or_send(callback.message.bot, d, request_id,
                        _lyrics_collect_text(''), _lyrics_collect_keyboard(request_id, False))


@music_router.callback_query(F.data.startswith("sunolyrback:"))
async def music_lyrics_back_callback(callback: types.CallbackQuery):
    request_id, d, wait = _suno_lyrics_guard(callback)
    if not d:
        await callback.answer("Запрос устарел. Открой /music заново.", show_alert=True)
        return
    _suno_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    await callback.answer()
    render, keyboard = _render_settings('suno', request_id)
    await _edit_or_send(callback.message.bot, d, request_id, render, keyboard)


def _cooldown_remaining(uid: int) -> int:
    now = asyncio.get_event_loop().time()
    if uid in _cooldowns and now - _cooldowns[uid] < _COOLDOWN:
        return int(_COOLDOWN - (now - _cooldowns[uid]))
    return 0


@music_router.callback_query(F.data.startswith("sunostop:"))
async def music_suno_stop_callback(callback: types.CallbackQuery):
    """Stop waiting for an active Suno task; the remote render is abandoned."""
    request_id = callback.data.split(':', 1)[1]
    entry = _suno_running.get(request_id)
    if not entry:
        await callback.answer("Генерация уже завершена.", show_alert=True)
        return
    if entry.get('user_id') != callback.from_user.id:
        await callback.answer("Это не твоя генерация.", show_alert=True)
        return
    entry['event'].set()
    await callback.answer("Останавливаю…")
    try:
        await callback.message.edit_text("Останавливаю Suno…", reply_markup=None)
    except Exception:
        pass


async def _suno_meta_input(message: types.Message, state: FSMContext, field: str) -> None:
    data = await state.get_data()
    meta = _suno_meta.get(data.get('suno_meta_id'))
    if not meta or meta.get('user_id') != message.from_user.id:
        await state.clear()
        await message.reply('Запрос на изменение метаданных устарел.')
        return
    text = (message.text or '').strip()
    if text.lower() == '/cancel':
        await _cancel_suno_meta(message, state, meta)
        return
    if field in ('title', 'author'):
        values = _split_meta_values(text)
        if not values:
            await _edit_suno_meta_message(message.bot, meta, _META_PROMPTS[field])
            return
        meta['titles' if field == 'title' else 'authors'] = values
        await state.clear()
        await _ask_suno_meta(message.bot, meta, _META_NEXT[field])
        return
    if not text:
        await _edit_suno_meta_message(message.bot, meta, _META_PROMPTS[field])
        return
    if len(text) > SUNO_LYRICS_MAX_CHARS:
        await _edit_suno_meta_message(
            message.bot, meta,
            f'Текст слишком длинный: максимум {SUNO_LYRICS_MAX_CHARS} символов.',
        )
        return
    meta['lyrics'] = text
    await _send_suno_result(message.bot, meta, state)


@music_router.message(SunoMetaState.title)
async def suno_meta_title_input(message: types.Message, state: FSMContext):
    await _suno_meta_input(message, state, 'title')


@music_router.message(SunoMetaState.author)
async def suno_meta_author_input(message: types.Message, state: FSMContext):
    await _suno_meta_input(message, state, 'author')


@music_router.message(SunoMetaState.lyrics)
async def suno_meta_lyrics_input(message: types.Message, state: FSMContext):
    await _suno_meta_input(message, state, 'lyrics')


@music_router.callback_query(F.data.startswith("sunometa:"))
async def music_suno_meta_callback(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(':', 3)
    if len(parts) != 4:
        await callback.answer('Некорректная кнопка.', show_alert=True)
        return
    _, request_id, field, action = parts
    meta = _suno_meta.get(request_id)
    if not meta or meta.get('user_id') != callback.from_user.id:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if field not in _META_STATES or meta.get('stage') != field:
        await callback.answer('Этот шаг уже пройден.', show_alert=True)
        return
    if action not in ('edit', 'keep', 'skip'):
        await callback.answer('Некорректная кнопка.', show_alert=True)
        return
    await callback.answer()
    if action == 'skip':
        await _send_suno_result(callback.message.bot, meta, state)
        return
    if action == 'edit':
        await state.set_state(_META_STATES[field])
        await state.update_data(suno_meta_id=request_id)
        await _edit_suno_meta_message(callback.message.bot, meta, _META_PROMPTS[field])
        return
    if field == 'title':
        meta['titles'] = None
        await _ask_suno_meta(callback.message.bot, meta, 'author')
    elif field == 'author':
        meta['authors'] = None
        await _ask_suno_meta(callback.message.bot, meta, 'lyrics')
    else:
        await _send_suno_result(callback.message.bot, meta, state)


@music_router.callback_query(F.data.startswith("sunogen:"))
async def suno_generate_callback(callback: types.CallbackQuery, state: FSMContext):
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
    for old_id, old_meta in list(_suno_meta.items()):
        if old_meta.get('user_id') == d['user_id']:
            _suno_meta.pop(old_id, None)
    await state.clear()

    cancel_event = asyncio.Event()
    _suno_running[request_id] = {'event': cancel_event, 'user_id': d['user_id']}

    async def _status(text: str):
        m = re.search(r'\((\d+)с\)', text or '')
        waited = int(m.group(1)) if m else 0
        try:
            await callback.message.edit_text(
                suno_progress_text(d['label'], waited, text),
                parse_mode='HTML',
                reply_markup=_suno_cancel_keyboard(request_id),
            )
        except Exception:
            pass

    try:
        await callback.message.edit_text(
            suno_progress_text(d['label'], 0),
            parse_mode="HTML",
            reply_markup=_suno_cancel_keyboard(request_id),
        )
    except Exception:
        pass

    try:
        tracks, error = await generate_suno(
            d['prompt'], model_key=d['model'], cfg=d['cfg'],
            status_cb=_status, cancel_event=cancel_event,
        )
    finally:
        _suno_running.pop(request_id, None)

    if cancel_event.is_set():
        _cooldowns.pop(d['user_id'], None)
        logger.info(f'Suno generation cancelled: {request_id}')
        try:
            await callback.message.edit_text("Генерация Suno отменена.", reply_markup=None)
        except Exception:
            pass
        return

    if error or not tracks:
        try:
            logger.error(f'Suno generation failed: {error}')
            await callback.message.edit_text(
                "<b>Ошибка Suno.</b> Попробуй ещё раз.", parse_mode="HTML", reply_markup=None)
        except Exception:
            pass
        return

    meta = {
        'request_id': request_id,
        'user_id': d['user_id'],
        'chat_id': d['chat_id'],
        'thread_id': d.get('message_thread_id'),
        'label': d['label'],
        'model_id': SUNO_MODELS.get(d.get('model', ''), {}).get('id', 'V6'),
        'tracks': tracks,
        'msg_id': callback.message.message_id,
        'titles': None,
        'authors': None,
        'lyrics': None,
    }
    _suno_meta[request_id] = meta
    asyncio.create_task(_expire_suno_meta(request_id))
    await _ask_suno_meta(callback.message.bot, meta, 'title')


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
    plain_label = model_info['label'].split(' ', 1)[-1]
    try:
        await callback.message.edit_text(
            lyria_progress_text(str(model_info['label']), d['prompt']),
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
                "<b>Ошибка генерации.</b> Попробуй ещё раз.", parse_mode="HTML")
        except Exception:
            pass
        return

    reply_kwargs = _reply_kwargs(d.get('message_thread_id'))

    if not audio_bytes:
        try:
            await callback.message.edit_text("Пустой ответ от модели.")
        except Exception:
            pass
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    caption = f"<b>{escape(plain_label)}</b>\n<i>{escape(_cut(d['prompt'], 140))}</i>"
    await safe_send(
        callback.message.bot.send_audio,
        chat_id=d['chat_id'],
        caption=caption,
        audio=BufferedInputFile(audio_bytes, filename=f"lyria_{d['model']}_{request_id}.mp3"),
        title=plain_label,
        performer='Hatani AI',
        parse_mode="HTML",
        **reply_kwargs,
    )
    if lyrics and not lyrics.strip().startswith(('<', '[[')):
        await _send_lyrics(callback.message.bot, d['chat_id'],
                           d.get('message_thread_id'), plain_label, lyrics)
