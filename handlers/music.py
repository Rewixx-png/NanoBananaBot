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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
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
from services.audio_metadata import embed_audio_metadata
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

# Active Suno polls: request_id → {event: asyncio.Event, user_id: int}
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
    rows = [
        [InlineKeyboardButton(text=f"{PROVIDERS[key]['label']}: {PROVIDERS[key]['desc']}",
                              callback_data=f"musicprov:{request_id}:{key}")]
        for key in ('suno', 'lyria')
    ]
    rows.append([InlineKeyboardButton(text='Изменить идею', callback_data=f'musicedit:{request_id}')])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"musiccancel:{request_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _model_keyboard(request_id: str, provider: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{info['label']}: {info['desc']}",
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


def _suno_cancel_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Отменить', callback_data=f'sunostop:{request_id}')
    ]])


_META_QUESTIONS = {
    'title': '<b>Треки готовы.</b>\nИзменить название трека?',
    'author': 'Изменить имя автора?',
    'lyrics': 'Изменить метаданные текста?',
}
_META_PROMPTS = {
    'title': (
        'Пришли новое название трека.\n'
        'Одно название — для обоих вариантов, два через <code>|</code> — отдельно.'
    ),
    'author': (
        'Пришли новое имя автора.\n'
        'Одно имя — для обоих вариантов, два через <code>|</code> — отдельно.'
    ),
    'lyrics': (
        'Пришли новый текст песни одним сообщением.\n'
        'Он попадёт в ID3-метаданные трека (USLT) и в сообщение с текстом.'
    ),
}
_META_STATES = {
    'title': SunoMetaState.title,
    'author': SunoMetaState.author,
    'lyrics': SunoMetaState.lyrics,
}
_META_NEXT = {'title': 'author', 'author': 'lyrics', 'lyrics': None}


def _suno_meta_keyboard(request_id: str, field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Изменить', callback_data=f'sunometa:{request_id}:{field}:edit'),
        InlineKeyboardButton(text='Оставить', callback_data=f'sunometa:{request_id}:{field}:keep'),
    ]])


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
        bot, meta, _META_QUESTIONS[field],
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
            meta_parts.append(f"{int(float(track['duration']))}с")
        meta_parts.append(escape(str(meta.get('label') or 'Suno')))
        parts.append(' · '.join(meta_parts))
        if track.get('tags'):
            parts.append(escape(str(track['tags'])[:220]))

        kwargs = {}
        if track.get('cover'):
            kwargs['thumbnail'] = BufferedInputFile(track['cover'], filename='cover.jpg')
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
    text = (_task_header(d) + "<b>Что делаем?</b>"
            "\n\nSuno: песня с вокалом и текстом."
            "\nLyria: инструментал.")
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
            InlineKeyboardButton(text='Отмена', callback_data=f'musiccancel:{request_id}')
        ]])
        sent = await safe_send(
            message.bot.send_message,
            chat_id=message.chat.id,
            text=("<b>Что за трек?</b>\n\n"
                  "Опиши жанр, настроение, инструменты или текст песни и пришли это сообщением.\n\n"
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
        InlineKeyboardButton(text='Отмена', callback_data=f'musiccancel:{request_id}')
    ]])
    try:
        await callback.message.edit_text(
            "<b>Новое описание</b>\nПришли, что за трек.",
            reply_markup=keyboard,
            parse_mode='HTML',
        )
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


# ── Suno settings screen ──────────────────────────────────────────────────
# Fields the user types as text rather than picks from buttons.
_SUNO_TEXT_FIELDS = {
    'style': 'Пришли <b>стиль</b>: жанр, инструменты, манеру. Идея уже учтена в описании.\n\nПример: <code>мрачный synthwave, аналоговые синты, без вокала</code>',
    'lyrics': 'Пришли <b>текст песни</b> следующим сообщением.\n\nМожно с тегами структуры: <code>[Verse]</code>, <code>[Chorus]</code>, <code>[Bridge]</code>.',
    'title': 'Пришли <b>название трека</b> следующим сообщением (до 80 символов).',
    'neg': 'Пришли стили, которые <b>исключить</b>.\n\nПример: <code>heavy metal, drums, distorted guitar</code>',
}

_LYRIA_TEXT_FIELDS = {
    'style': 'Пришли <b>стиль</b> (жанр, инструменты) следующим сообщением.\n\nПример: <code>sovietwave, аналоговые синты, тёплый ламповый звук</code>',
    'mood': 'Пришли <b>настроение</b> следующим сообщением.\n\nПример: <code>nostalgic and melancholic</code>',
    'structure': 'Пришли <b>структуру</b> или текст следующим сообщением.\n\nПример: <code>[Verse]\\nгород спит\\n[Chorus]\\nмы горим</code>',
}

_SUNO_NUMERIC_FIELDS = {'style_weight': float, 'weirdness': float, 'audio_weight': float,
                        'variety': int, 'duration': int}


# Every settings callback is `<provider><suffix>:<request_id>:…`.
_SETTINGS_SUFFIXES = ('cfg', 'input', 'back', 'cancel', 'gen', 'all', 'quick')


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


def _lyrics_collect_text(buffer: str) -> str:
    count = len(buffer or '')
    return (
        "<b>Текст песни</b>\n"
        "Пришли текст одним или несколькими сообщениями. Можно отправить .txt файлом.\n\n"
        f"Собрано: {count} / {SUNO_LYRICS_MAX_CHARS} символов.\n"
        "Когда закончишь, нажми «Готово»."
    )


def _lyrics_collect_keyboard(request_id: str, has_text: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text='Готово', callback_data=f'sunolyrdone:{request_id}')]
    if has_text:
        row.append(InlineKeyboardButton(text='Очистить', callback_data=f'sunolyrclear:{request_id}'))
    rows = [row]
    rows.append([InlineKeyboardButton(text='Назад', callback_data=f'sunolyrback:{request_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        html = (f"<b>Текст песни: {safe_title}</b>\n"
                f"<blockquote expandable>{safe_text}</blockquote>")
        await safe_send(bot.send_message, chat_id=chat_id, text=html,
                        parse_mode='HTML', **reply_kwargs)
    else:
        document = BufferedInputFile(text.encode('utf-8'), filename='lyrics.txt')
        await safe_send(bot.send_document, chat_id=chat_id, document=document,
                        caption=f"Текст песни: {title}"[:1024], **reply_kwargs)


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
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Отмена', callback_data=f'{provider}cancel:{request_id}')
    ]])
    await _edit_or_send(
        callback.message.bot, d, request_id,
        _SETTINGS[provider]['text_fields'].get(field, 'Пришли значение следующим сообщением:'),
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
    await callback.answer()
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
    if action not in ('edit', 'keep'):
        await callback.answer('Некорректная кнопка.', show_alert=True)
        return
    await callback.answer()
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
        try:
            await callback.message.edit_text(
                text, reply_markup=_suno_cancel_keyboard(request_id),
            )
        except Exception:
            pass

    try:
        await callback.message.edit_text(
            f"<b>Генерирую через {escape(d['label'])}...</b>\n\n"
            "Suno обычно отдаёт два варианта за 1–3 минуты. Кнопка «Отменить» остановит ожидание.",
            parse_mode="HTML", reply_markup=_suno_cancel_keyboard(request_id))
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
    # 'Lyria 3.5' -> 'Lyria 3.5' for the audio metadata
    plain_label = model_info['label'].split(' ', 1)[-1]
    try:
        await callback.message.edit_text(
            f"<b>Генерирую музыку...</b>\n"
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

    await safe_send(
        callback.message.bot.send_audio,
        chat_id=d['chat_id'],
        caption="Музыка",
        audio=BufferedInputFile(audio_bytes, filename=f"lyria_{d['model']}_{request_id}.mp3"),
        title=plain_label,
        performer='Hatani AI',
        parse_mode="HTML",
        **reply_kwargs,
    )
    if lyrics and not lyrics.strip().startswith(('<', '[[')):
        await _send_lyrics(callback.message.bot, d['chat_id'],
                           d.get('message_thread_id'), plain_label, lyrics)
