"""Suno Studio router: hub, lyrics, style boost, sounds, speech, audio studio, utilities."""

import asyncio
import json
import logging
import re
import uuid
from html import escape
from typing import Any, Optional

from aiogram import F, Router, types
from aiogram.filters import BaseFilter
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from database.suno import (
    get_suno_personas,
    get_suno_voices,
    save_suno_track,
)
from handlers.common import safe_send
from handlers.music_studio_ui import (
    studio_action_keyboard,
    studio_action_text,
    studio_audio_keyboard,
    studio_audio_text,
    studio_hub_keyboard,
    studio_hub_text,
    studio_sounds_keyboard,
    studio_sounds_text,
    studio_speech_keyboard,
    studio_speech_text,
    studio_utils_keyboard,
    studio_utils_text,
    track_compact_keyboard,
)
from handlers.music_ui import (
    _cut,
    _fmt_duration,
    suno_cancel_keyboard,
    tg_icon,
    ui_button,
)
from services.audio_metadata import embed_audio_metadata
from services.suno_service import (
    add_instrumental_suno,
    add_vocals_suno,
    boost_music_style,
    extend_suno,
    generate_lyrics,
    generate_sounds,
    generate_speech,
    get_music_record_info,
    get_remaining_credits,
    mashup_suno,
    recover_audio,
    replace_section_suno,
    upload_cover_suno,
    upload_extend_suno,
    upload_file_base64,
    upload_file_stream,
    upload_file_url,
)

logger = logging.getLogger(__name__)
music_studio_router = Router()

# sid -> session state dict
_studio_sessions: dict[str, dict[str, Any]] = {}
# (chat_id, user_id) -> awaiting input descriptor
_studio_awaiting: dict[tuple[int, int], dict[str, Any]] = {}
# run_id -> {event: asyncio.Event, user_id: int}
_studio_running: dict[str, dict[str, Any]] = {}


def drop_studio_user_state(uid: int) -> None:
    for k in [k for k in _studio_awaiting if k[1] == uid]:
        _studio_awaiting.pop(k, None)
    for sid, s in list(_studio_sessions.items()):
        if s.get('user_id') == uid:
            _studio_sessions.pop(sid, None)


def _reply_kw(thread_id) -> dict:
    return {'message_thread_id': thread_id} if thread_id else {}


def _new_sid() -> str:
    return uuid.uuid4().hex[:10]


async def _edit_or_send(bot, chat_id: int, msg_id: Optional[int], text: str, kb=None, thread_id=None) -> Optional[int]:
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, reply_markup=kb, parse_mode='HTML',
            )
            return msg_id
        except Exception:
            pass
    sent = await safe_send(
        bot.send_message, chat_id=chat_id, text=text,
        reply_markup=kb, parse_mode='HTML', **_reply_kw(thread_id),
    )
    return sent.message_id if sent else msg_id


def _stop_kb(run_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        ui_button("Остановить", callback_data=f"ss:stop:{run_id}", style="danger"),
    ]])


def _progress_html(title: str, status: str) -> str:
    return f"{tg_icon('working', '⏳')} <b>{escape(title)}</b>\n<i>{escape(status)}</i>"


async def _run_with_progress(
    bot,
    chat_id: int,
    msg_id: Optional[int],
    user_id: int,
    title: str,
    coro_factory,
    thread_id=None,
):
    """Run an async Suno operation with live status edits and a Cancel button."""
    run_id = _new_sid()
    cancel_event = asyncio.Event()
    _studio_running[run_id] = {'event': cancel_event, 'user_id': user_id}
    msg_id = await _edit_or_send(bot, chat_id, msg_id, _progress_html(title, 'Отправляю запрос…'), _stop_kb(run_id), thread_id)

    async def _cb(status_text: str):
        await _edit_or_send(bot, chat_id, msg_id, _progress_html(title, status_text), _stop_kb(run_id), thread_id)

    try:
        res, err = await coro_factory(_cb, cancel_event)
    finally:
        _studio_running.pop(run_id, None)

    if cancel_event.is_set():
        await _edit_or_send(bot, chat_id, msg_id, f"<b>{escape(title)}</b> — отменено.", studio_hub_keyboard(), thread_id)
        return None, None, True, msg_id
    if err:
        await _edit_or_send(
            bot, chat_id, msg_id,
            f"<b>Ошибка ({escape(title)}):</b>\n<code>{escape(_cut(str(err), 350))}</code>",
            studio_hub_keyboard(), thread_id,
        )
        return None, err, False, msg_id
    return res, None, False, msg_id


async def _deliver_suno_tracks(
    bot,
    chat_id: int,
    user_id: int,
    tracks: list[dict],
    *,
    label: str = 'Suno',
    model: str = 'V6',
    thread_id=None,
) -> None:
    """Save generated tracks to DB and send audio messages with compact Track Actions."""
    rkw = _reply_kw(thread_id)
    for idx, tr in enumerate(tracks, 1):
        title = tr.get('title') or f'{label} {idx}'
        lyrics = (tr.get('prompt') or '').strip()
        audio_bytes = embed_audio_metadata(tr['audio'], title=title, artist='Suno', lyrics=lyrics)
        db_id = await save_suno_track(
            user_id, chat_id,
            tr.get('task_id') or '', tr.get('id') or '',
            title=title, tags=tr.get('tags') or '',
            duration=float(tr.get('duration') or 0),
            model=model, prompt=lyrics, audio_url=tr.get('audio_url') or '',
        )
        parts = [f"<b>{escape(title)}</b>"]
        meta_bits = []
        if tr.get('duration'):
            meta_bits.append(f"<code>{_fmt_duration(int(float(tr['duration'])))}</code>")
        meta_bits.append(escape(label))
        parts.append(' · '.join(meta_bits))
        if tr.get('tags'):
            parts.append(f"<i>{escape(_cut(str(tr['tags']), 200))}</i>")
        kw: dict[str, Any] = {}
        if tr.get('cover'):
            kw['thumbnail'] = BufferedInputFile(tr['cover'], filename='cover.jpg')
        kb = track_compact_keyboard(db_id, tr.get('tags') or '')
        if kb:
            kw['reply_markup'] = kb
        await safe_send(
            bot.send_audio,
            chat_id=chat_id,
            audio=BufferedInputFile(audio_bytes, filename=f"suno_{idx}.mp3"),
            caption='\n'.join(parts)[:1020],
            title=title[:64],
            performer='Suno',
            parse_mode='HTML',
            **kw,
            **rkw,
        )


async def _extract_and_upload_media(
    message: types.Message,
    *,
    accept: tuple[str, ...] = ('audio', 'voice', 'document', 'url'),
    use_base64_small: bool = False,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract media or URL from a Telegram message and upload to Suno File Upload API.

    Returns ``(download_url, display_label, error)``.
    """
    text = (message.text or message.caption or '').strip()
    if 'url' in accept and re.match(r'^https?://\S+$', text):
        url = text.split()[0]
        info, err = await upload_file_url(url, upload_path='suno/uploads')
        if info and info.get('downloadUrl'):
            return info['downloadUrl'], info.get('fileName') or url.rsplit('/', 1)[-1][:40], None
        # Fall back to direct URL if remote host blocked re-upload
        return url, url.rsplit('/', 1)[-1][:40] or 'URL', None

    file_id = None
    filename = 'file.bin'
    mime = None
    file_size = 0

    if 'audio' in accept and message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or 'audio.mp3'
        mime = message.audio.mime_type or 'audio/mpeg'
        file_size = message.audio.file_size or 0
    elif 'voice' in accept and message.voice:
        file_id = message.voice.file_id
        filename = 'voice.ogg'
        mime = message.voice.mime_type or 'audio/ogg'
        file_size = message.voice.file_size or 0
    elif 'video' in accept and message.video:
        file_id = message.video.file_id
        filename = message.video.file_name or 'video.mp4'
        mime = message.video.mime_type or 'video/mp4'
        file_size = message.video.file_size or 0
    elif 'photo' in accept and message.photo:
        ph = message.photo[-1]
        file_id = ph.file_id
        filename = 'image.jpg'
        mime = 'image/jpeg'
        file_size = ph.file_size or 0
    elif 'document' in accept and message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or 'file.bin'
        mime = message.document.mime_type or 'application/octet-stream'
        file_size = message.document.file_size or 0

    if not file_id:
        return None, None, 'Пришли подходящий файл или прямую HTTP(S)-ссылку.'

    try:
        file_info = await message.bot.get_file(file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        raw = downloaded.read()
    except Exception as e:
        return None, None, f'Не удалось скачать файл из Telegram: {type(e).__name__}: {e}'

    if use_base64_small and len(raw) <= 1_500_000:
        info, err = await upload_file_base64(raw, filename, upload_path='suno/uploads', mime_type=mime)
    else:
        info, err = await upload_file_stream(raw, filename, upload_path='suno/uploads')
    if err or not info:
        return None, None, err or 'Не удалось загрузить файл на сервер Suno.'
    return info['downloadUrl'], filename, None


# ── Active-input filter so uploads/text route cleanly before chat_router ──

class StudioAwaitingFilter(BaseFilter):
    async def __call__(self, message: types.Message) -> bool:
        if not message.from_user:
            return False
        return (message.chat.id, message.from_user.id) in _studio_awaiting


# ── Hub & Common Callbacks ───────────────────────────────────────────────

@music_studio_router.callback_query(F.data == "ss:noop")
async def ss_noop(callback: types.CallbackQuery):
    await callback.answer()


@music_studio_router.callback_query(F.data == "ss:close")
async def ss_close(callback: types.CallbackQuery):
    _studio_awaiting.pop((callback.message.chat.id, callback.from_user.id), None)
    await callback.answer()
    try:
        await callback.message.edit_text("Студия закрыта.", reply_markup=None)
    except Exception:
        pass


@music_studio_router.callback_query(F.data.startswith("ss:hub"))
async def ss_hub(callback: types.CallbackQuery):
    parts = callback.data.split(':', 2)
    back_rid = parts[2] if len(parts) > 2 else ''
    _studio_awaiting.pop((callback.message.chat.id, callback.from_user.id), None)
    from handlers.music import _music_awaiting_idea
    _music_awaiting_idea.pop((callback.message.chat.id, callback.from_user.id), None)
    await callback.answer()
    try:
        await callback.message.edit_text(
            studio_hub_text(), reply_markup=studio_hub_keyboard(back_rid), parse_mode='HTML',
        )
    except Exception:
        pass


@music_studio_router.callback_query(F.data == "ss:audio")
async def ss_audio(callback: types.CallbackQuery):
    _studio_awaiting.pop((callback.message.chat.id, callback.from_user.id), None)
    await callback.answer()
    try:
        await callback.message.edit_text(
            studio_audio_text(), reply_markup=studio_audio_keyboard(), parse_mode='HTML',
        )
    except Exception:
        pass


@music_studio_router.callback_query(F.data.startswith("ss:stop:"))
async def ss_stop(callback: types.CallbackQuery):
    run_id = callback.data.split(':', 2)[2]
    entry = _studio_running.get(run_id)
    if not entry:
        await callback.answer("Задача уже завершена.", show_alert=True)
        return
    if entry['user_id'] != callback.from_user.id:
        await callback.answer("Это не твоя задача.", show_alert=True)
        return
    entry['event'].set()
    await callback.answer("Останавливаю…")


# ── Lyrics Generation (POST /api/v1/lyrics) ──────────────────────────────

@music_studio_router.callback_query(F.data == "ss:lyrics")
async def ss_lyrics_start(callback: types.CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    uid = callback.from_user.id
    _studio_awaiting[(chat_id, uid)] = {
        'mode': 'lyrics',
        'msg_id': callback.message.message_id,
        'thread_id': callback.message.message_thread_id if callback.message.chat.is_forum else None,
    }
    text = (
        f"{tg_icon('pencil', '✎')} <b>Генерация текста песни</b>\n\n"
        "Опиши тему, настроение и структуру одним сообщением.\n"
        "<i>Пример:</i> <code>меланхоличный инди-рок про ночной дождь в Питере, куплет-припев-бридж</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Студия", callback_data="ss:hub")]])
    await _edit_or_send(callback.bot, chat_id, callback.message.message_id, text, kb)


@music_studio_router.callback_query(F.data.startswith("ss:uselyr:"))
async def ss_use_lyrics_in_song(callback: types.CallbackQuery):
    """Open Suno Quick settings pre-filled with generated lyrics."""
    sid = callback.data.split(':', 2)[2]
    sess = _studio_sessions.get(sid)
    if not sess or sess.get('user_id') != callback.from_user.id:
        await callback.answer("Данные устарели.", show_alert=True)
        return
    await callback.answer("Текст подставлен в /music")
    from handlers.common import _SUNO_CFG_DEFAULTS
    from handlers.music import _pending_music, _render_settings
    from state import pending_suno_configs
    req_id = uuid.uuid4().hex[:12]
    cfg = dict(_SUNO_CFG_DEFAULTS)
    cfg['lyrics'] = sess.get('lyrics', '')
    if sess.get('title'):
        cfg['title'] = sess['title'][:80]
    thread_id = callback.message.message_thread_id if callback.message.chat.is_forum else None
    base = {
        'chat_id': callback.message.chat.id, 'user_id': callback.from_user.id,
        'prompt': sess.get('title') or 'песня по готовому тексту',
        'message_thread_id': thread_id, 'msg_id': callback.message.message_id,
    }
    _pending_music[req_id] = base
    pending_suno_configs[req_id] = {
        **base, 'model': 'suno-v6', 'provider': 'suno',
        'label': 'Suno V6', 'cfg': cfg, 'view': 'quick',
    }
    text, kb = _render_settings('suno', req_id)
    await _edit_or_send(callback.bot, callback.message.chat.id, callback.message.message_id, text, kb, thread_id)


# ── Style Boost (POST /api/v1/style/generate) ────────────────────────────

@music_studio_router.callback_query(F.data == "ss:boost")
async def ss_boost_start(callback: types.CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    uid = callback.from_user.id
    _studio_awaiting[(chat_id, uid)] = {
        'mode': 'boost',
        'msg_id': callback.message.message_id,
        'thread_id': callback.message.message_thread_id if callback.message.chat.is_forum else None,
    }
    text = (
        f"{tg_icon('sparkle', '✦')} <b>Буст стиля</b>\n\n"
        "Пришли краткие теги или жанр — Suno развернёт их в детальное описание стиля.\n"
        "<i>Пример:</i> <code>deep house, emotional, female vocal</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Студия", callback_data="ss:hub")]])
    await _edit_or_send(callback.bot, chat_id, callback.message.message_id, text, kb)


# ── Sounds / SFX (POST /api/v1/generate/sounds) ──────────────────────────

@music_studio_router.callback_query(F.data == "ss:sounds")
async def ss_sounds_open(callback: types.CallbackQuery):
    await callback.answer()
    sid = _new_sid()
    sess = {
        'kind': 'sounds', 'user_id': callback.from_user.id,
        'chat_id': callback.message.chat.id, 'msg_id': callback.message.message_id,
        'thread_id': callback.message.message_thread_id if callback.message.chat.is_forum else None,
        'prompt': '', 'model': 'V6', 'sound_loop': False,
        'sound_tempo': None, 'sound_key': 'Any', 'grab_lyrics': False,
    }
    _studio_sessions[sid] = sess
    await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_sounds_text(sid, sess), studio_sounds_keyboard(sid, sess))


@music_studio_router.callback_query(F.data.startswith("sss:"))
async def ss_sounds_cb(callback: types.CallbackQuery):
    parts = callback.data.split(':', 4)
    action, sid = parts[1], parts[2]
    sess = _studio_sessions.get(sid)
    if not sess or sess.get('user_id') != callback.from_user.id:
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    if action == 'set' and len(parts) == 5:
        field, val = parts[3], parts[4]
        if field in ('sound_loop', 'grab_lyrics'):
            sess[field] = val == '1'
        else:
            sess[field] = val
        await callback.answer()
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_sounds_text(sid, sess), studio_sounds_keyboard(sid, sess))
        return

    if action == 'inp' and len(parts) == 4:
        field = parts[3]
        await callback.answer()
        _studio_awaiting[(sess['chat_id'], sess['user_id'])] = {
            'mode': 'sounds_field', 'sid': sid, 'field': field,
            'msg_id': sess['msg_id'], 'thread_id': sess.get('thread_id'),
        }
        prompt_msg = (
            "Пришли описание звука / SFX / лупа (до 500 символов)."
            if field == 'prompt' else
            "Пришли BPM (число от 1 до 300) или <code>0</code> для авто."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Назад", callback_data=f"sss:back:{sid}")]])
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], prompt_msg, kb)
        return

    if action == 'back':
        _studio_awaiting.pop((sess['chat_id'], sess['user_id']), None)
        await callback.answer()
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_sounds_text(sid, sess), studio_sounds_keyboard(sid, sess))
        return

    if action == 'run':
        if not (sess.get('prompt') or '').strip():
            await callback.answer("Сначала задай описание звука.", show_alert=True)
            return
        await callback.answer()
        tracks, err, cancelled, mid = await _run_with_progress(
            callback.bot, sess['chat_id'], sess['msg_id'], sess['user_id'],
            'Звуки / SFX',
            lambda cb, ev: generate_sounds(
                sess['prompt'], model_key=sess['model'],
                sound_loop=sess['sound_loop'], sound_tempo=sess['sound_tempo'],
                sound_key=sess['sound_key'], grab_lyrics=sess['grab_lyrics'],
                status_cb=cb, cancel_event=ev,
            ),
            sess.get('thread_id'),
        )
        if tracks:
            try:
                await callback.bot.delete_message(sess['chat_id'], mid)
            except Exception:
                pass
            await _deliver_suno_tracks(
                callback.bot, sess['chat_id'], sess['user_id'], tracks,
                label='Suno Sounds', model=sess['model'], thread_id=sess.get('thread_id'),
            )


# ── Speech (POST /api/v1/generate/speech) ────────────────────────────────

@music_studio_router.callback_query(F.data == "ss:speech")
async def ss_speech_open(callback: types.CallbackQuery):
    await callback.answer()
    sid = _new_sid()
    sess = {
        'kind': 'speech', 'user_id': callback.from_user.id,
        'chat_id': callback.message.chat.id, 'msg_id': callback.message.message_id,
        'thread_id': callback.message.message_thread_id if callback.message.chat.is_forum else None,
        'custom_mode': False, 'prompt': '', 'script': '', 'tone': '',
        'vocal_gender': '', 'variety': 1, 'background_music': False,
    }
    _studio_sessions[sid] = sess
    await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_speech_text(sid, sess), studio_speech_keyboard(sid, sess))


@music_studio_router.callback_query(F.data.startswith("ssp:"))
async def ss_speech_cb(callback: types.CallbackQuery):
    parts = callback.data.split(':', 4)
    action, sid = parts[1], parts[2]
    sess = _studio_sessions.get(sid)
    if not sess or sess.get('user_id') != callback.from_user.id:
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    if action == 'set' and len(parts) == 5:
        field, val = parts[3], parts[4]
        if field in ('custom_mode', 'background_music'):
            sess[field] = val == '1'
        elif field == 'variety':
            sess[field] = int(val)
        else:
            sess[field] = val
        await callback.answer()
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_speech_text(sid, sess), studio_speech_keyboard(sid, sess))
        return

    if action == 'inp' and len(parts) == 4:
        field = parts[3]
        await callback.answer()
        _studio_awaiting[(sess['chat_id'], sess['user_id'])] = {
            'mode': 'speech_field', 'sid': sid, 'field': field,
            'msg_id': sess['msg_id'], 'thread_id': sess.get('thread_id'),
        }
        prompts = {
            'prompt': "Пришли описание сцены (до 3000 симв.) — Suno сам придумает текст.",
            'script': "Пришли точный текст речи (до 5000 симв.).",
            'tone': "Пришли тон, эмоции, темп и акцент (до 1000 симв.).",
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Назад", callback_data=f"ssp:back:{sid}")]])
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], prompts.get(field, 'Пришли текст:'), kb)
        return

    if action == 'back':
        _studio_awaiting.pop((sess['chat_id'], sess['user_id']), None)
        await callback.answer()
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_speech_text(sid, sess), studio_speech_keyboard(sid, sess))
        return

    if action == 'run':
        adv = bool(sess.get('custom_mode'))
        if not adv and not (sess.get('prompt') or '').strip():
            await callback.answer("Задай описание сцены.", show_alert=True)
            return
        if adv and not (sess.get('script') or '').strip():
            await callback.answer("Задай текст речи.", show_alert=True)
            return
        await callback.answer()
        tracks, err, cancelled, mid = await _run_with_progress(
            callback.bot, sess['chat_id'], sess['msg_id'], sess['user_id'],
            'Suno Speech',
            lambda cb, ev: generate_speech(
                custom_mode=adv,
                prompt=sess.get('prompt', ''),
                script=sess.get('script', ''),
                tone=sess.get('tone', ''),
                variety=sess.get('variety'),
                vocal_gender=sess.get('vocal_gender', ''),
                background_music=sess.get('background_music'),
                status_cb=cb, cancel_event=ev,
            ),
            sess.get('thread_id'),
        )
        if tracks:
            try:
                await callback.bot.delete_message(sess['chat_id'], mid)
            except Exception:
                pass
            await _deliver_suno_tracks(
                callback.bot, sess['chat_id'], sess['user_id'], tracks,
                label='Suno Speech', model='V6_MINI', thread_id=sess.get('thread_id'),
            )


# ── Audio Studio Actions (Cover/Extend/AddInst/AddVoc/Mashup/Replace) ────

def create_action_session(
    kind: str,
    user_id: int,
    chat_id: int,
    msg_id: int,
    thread_id=None,
    **extra,
) -> tuple[str, dict]:
    sid = _new_sid()
    sess = {
        'kind': kind, 'user_id': user_id, 'chat_id': chat_id, 'msg_id': msg_id,
        'thread_id': thread_id, 'model': 'V6', 'instrumental': False,
        'style': '', 'lyrics': '', 'title': '', 'neg': '',
        'upload_url': '', 'urls': [], 'source_label': '',
        'task_id': '', 'audio_id': '', 'track_db_id': None,
        'continue_at': None, 'infill_start': None, 'infill_end': None,
        'prompt': '', 'full_lyrics': '', 'persona_id': '', 'persona_model': 'style_persona', 'persona_label': '',
        **extra,
    }
    _studio_sessions[sid] = sess
    return sid, sess


@music_studio_router.callback_query(F.data.startswith("ss:act:"))
async def ss_audio_action_open(callback: types.CallbackQuery):
    kind = callback.data.split(':', 2)[2]
    await callback.answer()
    thread_id = callback.message.message_thread_id if callback.message.chat.is_forum else None
    sid, sess = create_action_session(kind, callback.from_user.id, callback.message.chat.id, callback.message.message_id, thread_id)
    await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_action_text(sid, sess), studio_action_keyboard(sid, sess))


@music_studio_router.callback_query(F.data.startswith("ssa:"))
async def ss_action_cb(callback: types.CallbackQuery):
    parts = callback.data.split(':', 4)
    action, sid = parts[1], parts[2]
    sess = _studio_sessions.get(sid)
    if not sess or sess.get('user_id') != callback.from_user.id:
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    if action == 'set' and len(parts) == 5:
        field, val = parts[3], parts[4]
        if field == 'instrumental':
            sess[field] = val == '1'
        else:
            sess[field] = val
        await callback.answer()
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_action_text(sid, sess), studio_action_keyboard(sid, sess))
        return

    if action == 'back':
        _studio_awaiting.pop((sess['chat_id'], sess['user_id']), None)
        await callback.answer()
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_action_text(sid, sess), studio_action_keyboard(sid, sess))
        return

    if action == 'persona':
        await callback.answer()
        personas = await get_suno_personas(sess['user_id'])
        voices = await get_suno_voices(sess['user_id'])
        rows = []
        if sess.get('persona_id'):
            rows.append([ui_button("Убрать персону/голос", callback_data=f"ssa:setp:{sid}:clear", style="danger")])
        for v in voices[:6]:
            rows.append([ui_button(f"Голос · {_cut(v['name'], 26)}", callback_data=f"ssa:setp:{sid}:v:{v['id']}")])
        for p in personas[:6]:
            rows.append([ui_button(f"Персона · {_cut(p['name'], 26)}", callback_data=f"ssa:setp:{sid}:p:{p['id']}")])
        rows.append([ui_button("← Назад", callback_data=f"ssa:back:{sid}")])
        msg = "Выбери персону стиля или обученный голос:" if (personas or voices) else "У тебя пока нет сохранённых персон и голосов. Создай их в разделе «Голоса и персоны»."
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], msg, InlineKeyboardMarkup(inline_keyboard=rows))
        return

    if action == 'setp':
        sub = callback.data.split(':', 5)
        if sub[3] == 'clear':
            sess['persona_id'] = ''
            sess['persona_label'] = ''
        elif sub[3] == 'v':
            voices = await get_suno_voices(sess['user_id'])
            v = next((x for x in voices if str(x['id']) == sub[4]), None)
            if v:
                sess['persona_id'] = v['voice_id']
                sess['persona_model'] = 'voice_persona'
                sess['persona_label'] = f"Голос: {v['name']}"
        elif sub[3] == 'p':
            personas = await get_suno_personas(sess['user_id'])
            p = next((x for x in personas if str(x['id']) == sub[4]), None)
            if p:
                sess['persona_id'] = p['persona_id']
                sess['persona_model'] = 'style_persona'
                sess['persona_label'] = f"Персона: {p['name']}"
        await callback.answer()
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], studio_action_text(sid, sess), studio_action_keyboard(sid, sess))
        return

    if action == 'inp' and len(parts) == 4:
        field = parts[3]
        await callback.answer()
        _studio_awaiting[(sess['chat_id'], sess['user_id'])] = {
            'mode': 'action_field', 'sid': sid, 'field': field,
            'msg_id': sess['msg_id'], 'thread_id': sess.get('thread_id'),
        }
        hints = {
            'upload': "Пришли аудиофайл, голосовое, документ (.mp3/.wav/.ogg) или прямую HTTP(S)-ссылку.",
            'style': "Пришли стиль / теги (до 1000 символов).",
            'lyrics': "Пришли текст песни (до 5000 символов).",
            'title': "Пришли название трека (до 80 символов).",
            'neg': "Пришли стили/инструменты, которые нужно исключить.",
            'continue_at': "Пришли секунду старта продления (например <code>95</code> или <code>1:35</code>).",
            'infill_range': "Пришли интервал замены в секундах (минимум 10 сек), например: <code>15 35</code> или <code>0:15-0:35</code>.",
            'prompt': "Пришли новый текст только для заменяемого фрагмента.",
            'full_lyrics': "Пришли полный текст всей песни с учётом замены.",
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Назад", callback_data=f"ssa:back:{sid}")]])
        await _edit_or_send(callback.bot, sess['chat_id'], sess['msg_id'], hints.get(field, 'Пришли значение:'), kb)
        return

    if action == 'run':
        kind = sess['kind']
        # Validate required source
        if kind == 'mashup' and len(sess.get('urls') or []) < 2:
            await callback.answer("Нужно загрузить 2 аудиофайла.", show_alert=True)
            return
        if kind in ('uextend', 'addinst', 'addvoc') and not sess.get('upload_url'):
            await callback.answer("Сначала загрузи аудио или ссылку.", show_alert=True)
            return
        if kind in ('ucover', 'replace') and not (sess.get('upload_url') or (sess.get('task_id') and sess.get('audio_id'))):
            await callback.answer("Нужен исходный трек или загруженный файл.", show_alert=True)
            return
        if kind == 'replace':
            if sess.get('infill_start') is None or sess.get('infill_end') is None:
                await callback.answer("Задай интервал замены (минимум 10 сек).", show_alert=True)
                return
            if not (sess.get('prompt') or '').strip():
                await callback.answer("Задай текст заменяемого фрагмента.", show_alert=True)
                return

        await callback.answer()
        cfg = {
            'instrumental': '1' if sess.get('instrumental') else '0',
            'style': sess.get('style', ''),
            'lyrics': sess.get('lyrics', ''),
            'title': sess.get('title', ''),
            'neg': sess.get('neg', ''),
            'continue_at': sess.get('continue_at'),
            'persona_id': sess.get('persona_id', ''),
            'persona_model': sess.get('persona_model', 'style_persona'),
        }

        def _factory(cb, ev):
            if kind == 'extend':
                return extend_suno(sess['audio_id'], task_id=sess.get('task_id', ''), model_key=sess['model'], cfg=cfg, status_cb=cb, cancel_event=ev)
            if kind == 'ucover':
                return upload_cover_suno(upload_url=sess.get('upload_url', ''), task_id=sess.get('task_id', ''), audio_id=sess.get('audio_id', ''), model_key=sess['model'], cfg=cfg, status_cb=cb, cancel_event=ev)
            if kind == 'uextend':
                return upload_extend_suno(sess['upload_url'], model_key=sess['model'], cfg=cfg, status_cb=cb, cancel_event=ev)
            if kind == 'addinst':
                return add_instrumental_suno(sess['upload_url'], sess.get('title') or 'Instrumental', sess.get('style') or 'pop, clean mix', sess.get('neg') or 'noise', model_key=sess['model'], cfg=cfg, status_cb=cb, cancel_event=ev)
            if kind == 'addvoc':
                return add_vocals_suno(sess['upload_url'], sess.get('title') or 'Vocal Mix', sess.get('style') or 'pop, expressive vocal', sess.get('neg') or 'noise', lyrics=sess.get('lyrics', ''), model_key=sess['model'], cfg=cfg, status_cb=cb, cancel_event=ev)
            if kind == 'mashup':
                return mashup_suno(sess['urls'], model_key=sess['model'], cfg=cfg, status_cb=cb, cancel_event=ev)
            return replace_section_suno(
                infill_start_s=sess['infill_start'], infill_end_s=sess['infill_end'],
                prompt=sess.get('prompt', ''), full_lyrics=sess.get('full_lyrics') or sess.get('prompt', ''),
                tags=sess.get('style') or 'pop', title=sess.get('title') or 'Edited',
                task_id=sess.get('task_id', ''), audio_id=sess.get('audio_id', ''),
                upload_url=sess.get('upload_url', ''), model_key=sess['model'],
                cfg=cfg, status_cb=cb, cancel_event=ev,
            )

        tracks, err, cancelled, mid = await _run_with_progress(
            callback.bot, sess['chat_id'], sess['msg_id'], sess['user_id'],
            f"Suno · {kind}", _factory, sess.get('thread_id'),
        )
        if tracks:
            try:
                await callback.bot.delete_message(sess['chat_id'], mid)
            except Exception:
                pass
            await _deliver_suno_tracks(
                callback.bot, sess['chat_id'], sess['user_id'], tracks,
                label=f"Suno {sess['model']}", model=sess['model'], thread_id=sess.get('thread_id'),
            )


# ── Utilities: Credits, Task Status, Recovery, File Upload ───────────────

@music_studio_router.callback_query(F.data == "ss:utils")
async def ss_utils_open(callback: types.CallbackQuery):
    _studio_awaiting.pop((callback.message.chat.id, callback.from_user.id), None)
    await callback.answer()
    await _edit_or_send(callback.bot, callback.message.chat.id, callback.message.message_id, studio_utils_text(), studio_utils_keyboard())


@music_studio_router.callback_query(F.data == "ssu:credits")
async def ssu_credits(callback: types.CallbackQuery):
    await callback.answer("Проверяю баланс…")
    rows_data, err = await get_remaining_credits()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Обновить", callback_data="ssu:credits", style="primary")],
        [ui_button("← Утилиты", callback_data="ss:utils")],
    ])
    if err or not rows_data:
        await _edit_or_send(callback.bot, callback.message.chat.id, callback.message.message_id, f"<b>Ошибка:</b> {escape(err or 'нет данных')}", kb)
        return
    total = sum(r['credits'] for r in rows_data if r.get('ok') and r.get('credits') is not None)
    lines = [f"{tg_icon('chart', '💰')} <b>Баланс Suno</b> · всего <code>{total}</code> кр.", ""]
    for r in rows_data[:12]:
        if r['ok']:
            lines.append(f"• <code>{escape(r['key'])}</code> — <code>{r['credits']}</code> кр.")
        else:
            lines.append(f"• <code>{escape(r['key'])}</code> — <i>{escape(_cut(str(r.get('error', '')), 40))}</i>")
    await _edit_or_send(callback.bot, callback.message.chat.id, callback.message.message_id, "\n".join(lines), kb)


@music_studio_router.callback_query(F.data.in_({"ssu:status", "ssu:recover", "ssu:upload"}))
async def ssu_prompt_input(callback: types.CallbackQuery):
    mode = callback.data.split(':', 1)[1]
    await callback.answer()
    chat_id = callback.message.chat.id
    _studio_awaiting[(chat_id, callback.from_user.id)] = {
        'mode': f'util_{mode}',
        'msg_id': callback.message.message_id,
        'thread_id': callback.message.message_thread_id if callback.message.chat.is_forum else None,
    }
    prompts = {
        'status': f"{tg_icon('search', '🔍')} <b>Статус задачи</b>\n\nПришли <code>taskId</code> музыкальной задачи Suno.",
        'recover': f"{tg_icon('link', '🔗')} <b>Восстановить ссылки</b>\n\nПришли <code>sunoTaskId</code> старой задачи — сервис перевыпустит рабочие ссылки и скачает аудио.",
        'upload': f"{tg_icon('attachment', '📎')} <b>Временный хостинг (3 дня)</b>\n\nПришли любой файл (аудио/фото/видео/документ) или прямую ссылку HTTP(S).",
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Утилиты", callback_data="ss:utils")]])
    await _edit_or_send(callback.bot, chat_id, callback.message.message_id, prompts[mode], kb)


def _parse_seconds(raw: str) -> Optional[float]:
    s = (raw or '').strip().replace(',', '.')
    if ':' in s:
        parts = s.split(':')
        if len(parts) == 2:
            try:
                return int(parts[0]) * 60 + float(parts[1])
            except ValueError:
                return None
    try:
        return float(s)
    except ValueError:
        return None


# ── Unified Message Input Handler (registered on music_studio_router) ────

@music_studio_router.message(StudioAwaitingFilter())
async def handle_studio_input_message(message: types.Message):
    key = (message.chat.id, message.from_user.id)
    wait = _studio_awaiting.pop(key, None)
    if not wait:
        return
    mode = wait.get('mode')
    msg_id = wait.get('msg_id')
    thread_id = wait.get('thread_id')
    text = (message.text or '').strip()

    # Let secondary module handle track/stem/voice/ref inputs
    from handlers.music_studio_tracks import handle_studio_extra_input
    if await handle_studio_extra_input(message, wait):
        return

    try:
        await message.delete()
    except Exception:
        pass

    if mode == 'lyrics':
        if not text:
            await _edit_or_send(message.bot, message.chat.id, msg_id, "Нужен текстовый промпт.", studio_hub_keyboard(), thread_id)
            return
        variants, err, cancelled, mid = await _run_with_progress(
            message.bot, message.chat.id, msg_id, message.from_user.id,
            'Текст песни', lambda cb, ev: generate_lyrics(text, status_cb=cb, cancel_event=ev), thread_id,
        )
        if not variants:
            return
        first = variants[0]
        sid = _new_sid()
        _studio_sessions[sid] = {'user_id': message.from_user.id, 'title': first['title'], 'lyrics': first['text']}
        body = [f"{tg_icon('pencil', '✎')} <b>{escape(first['title'])}</b>", f"<blockquote expandable>{escape(first['text'][:3200])}</blockquote>"]
        if len(variants) > 1:
            body.append(f"\n<i>Вариант 2 · {escape(variants[1]['title'])}</i>")
            body.append(f"<blockquote expandable>{escape(variants[1]['text'][:600])}</blockquote>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ui_button("Использовать в песне", callback_data=f"ss:uselyr:{sid}", style="success", icon="play")],
            [ui_button("Ещё текст", callback_data="ss:lyrics"), ui_button("← Студия", callback_data="ss:hub")],
        ])
        await _edit_or_send(message.bot, message.chat.id, mid, "\n".join(body), kb, thread_id)
        return

    if mode == 'boost':
        if not text:
            return
        await _edit_or_send(message.bot, message.chat.id, msg_id, _progress_html('Буст стиля', 'Улучшаю описание…'), None, thread_id)
        data, err = await boost_music_style(text)
        if err or not data:
            await _edit_or_send(message.bot, message.chat.id, msg_id, f"<b>Ошибка:</b> {escape(err or 'не удалось')}", studio_hub_keyboard(), thread_id)
            return
        result_style = str(data.get('result') or '').strip()
        rem = data.get('creditsRemaining')
        rem_line = f"\n<i>Остаток кредитов: {rem}</i>" if rem is not None else ""
        kb_rows = []
        if result_style:
            kb_rows.append([ui_button("Скопировать стиль", copy_text=result_style[:256])])
        kb_rows.append([ui_button("Ещё раз", callback_data="ss:boost"), ui_button("← Студия", callback_data="ss:hub")])
        await _edit_or_send(
            message.bot, message.chat.id, msg_id,
            f"{tg_icon('sparkle', '✦')} <b>Усиленный стиль</b>\n<blockquote>{escape(result_style or 'Пустой ответ')}</blockquote>{rem_line}",
            InlineKeyboardMarkup(inline_keyboard=kb_rows), thread_id,
        )
        return

    if mode == 'sounds_field':
        sess = _studio_sessions.get(wait['sid'])
        if not sess:
            return
        field = wait['field']
        if field == 'prompt':
            sess['prompt'] = text[:500]
        elif field == 'sound_tempo':
            try:
                v = int(text)
                sess['sound_tempo'] = v if 1 <= v <= 300 else None
            except ValueError:
                sess['sound_tempo'] = None
        await _edit_or_send(message.bot, sess['chat_id'], sess['msg_id'], studio_sounds_text(wait['sid'], sess), studio_sounds_keyboard(wait['sid'], sess), thread_id)
        return

    if mode == 'speech_field':
        sess = _studio_sessions.get(wait['sid'])
        if not sess:
            return
        field = wait['field']
        limits = {'prompt': 3000, 'script': 5000, 'tone': 1000}
        sess[field] = text[:limits.get(field, 1000)]
        await _edit_or_send(message.bot, sess['chat_id'], sess['msg_id'], studio_speech_text(wait['sid'], sess), studio_speech_keyboard(wait['sid'], sess), thread_id)
        return

    if mode == 'action_field':
        sess = _studio_sessions.get(wait['sid'])
        if not sess:
            return
        field = wait['field']
        if field == 'upload':
            await _edit_or_send(message.bot, sess['chat_id'], sess['msg_id'], _progress_html('Загрузка файла', 'Отправляю на хостинг Suno…'), None, thread_id)
            url, lbl, err = await _extract_and_upload_media(message, accept=('audio', 'voice', 'document', 'url'))
            if err or not url:
                _studio_awaiting[key] = wait
                await _edit_or_send(
                    message.bot, sess['chat_id'], sess['msg_id'],
                    f"<b>Ошибка:</b> {escape(err or 'не удалось загрузить')}\nПопробуй ещё раз.",
                    InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Назад", callback_data=f"ssa:back:{wait['sid']}")]]),
                    thread_id,
                )
                return
            if sess['kind'] == 'mashup':
                urls = sess.setdefault('urls', [])
                if len(urls) >= 2:
                    urls.pop(0)
                urls.append(url)
                sess['source_label'] = ', '.join([lbl or 'audio'] if not sess.get('source_label') else [sess['source_label'], lbl or 'audio'][-2:])
            else:
                sess['upload_url'] = url
                sess['source_label'] = lbl or 'audio'
        elif field == 'continue_at':
            sec = _parse_seconds(text)
            sess['continue_at'] = sec if sec and sec > 0 else None
        elif field == 'infill_range':
            tokens = re.findall(r'\d+(?::\d+)?(?:\.\d+)?', text)
            if len(tokens) >= 2:
                s0, s1 = _parse_seconds(tokens[0]), _parse_seconds(tokens[1])
                if s0 is not None and s1 is not None and s1 - s0 >= 10:
                    sess['infill_start'], sess['infill_end'] = s0, s1
        else:
            sess[field] = text
        await _edit_or_send(message.bot, sess['chat_id'], sess['msg_id'], studio_action_text(wait['sid'], sess), studio_action_keyboard(wait['sid'], sess), thread_id)
        return

    if mode == 'util_status':
        data, err = await get_music_record_info(text)
        kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Утилиты", callback_data="ss:utils")]])
        if err or not data:
            await _edit_or_send(message.bot, message.chat.id, msg_id, f"<b>Ошибка:</b> {escape(err or 'не найдено')}", kb, thread_id)
            return
        status = data.get('status') or 'UNKNOWN'
        tracks = (data.get('response') or {}).get('sunoData') or []
        lines = [
            f"{tg_icon('search', '🔍')} <b>Задача</b> <code>{escape(text[:24])}</code>",
            f"Статус: <code>{escape(str(status))}</code> · Треков: <code>{len(tracks)}</code>",
        ]
        for i, t in enumerate(tracks[:4], 1):
            lines.append(f"{i}. <b>{escape(str(t.get('title') or 'Suno'))}</b> · id <code>{escape(str(t.get('id') or '')[:14])}</code>")
        await _edit_or_send(message.bot, message.chat.id, msg_id, "\n".join(lines), kb, thread_id)
        return

    if mode == 'util_recover':
        res, err, cancelled, mid = await _run_with_progress(
            message.bot, message.chat.id, msg_id, message.from_user.id,
            'Восстановление ссылок', lambda cb, ev: recover_audio(text, status_cb=cb, cancel_event=ev), thread_id,
        )
        if not res:
            return
        ok = [t for t in res if t.get('audio')]
        if ok:
            try:
                await message.bot.delete_message(message.chat.id, mid)
            except Exception:
                pass
            for idx, item in enumerate(ok, 1):
                await safe_send(
                    message.bot.send_audio, chat_id=message.chat.id,
                    audio=BufferedInputFile(item['audio'], filename=f"recovered_{idx}.mp3"),
                    caption=f"Восстановленный трек <code>{escape(item['id'][:14])}</code>",
                    parse_mode='HTML', **_reply_kw(thread_id),
                )
        else:
            errs = "; ".join(f"{t.get('id', '')[:8]}: {t.get('error', 'failed')}" for t in res)
            await _edit_or_send(message.bot, message.chat.id, mid, f"<b>Не удалось восстановить:</b> <code>{escape(_cut(errs, 260))}</code>", studio_utils_keyboard(), thread_id)
        return

    if mode == 'util_upload':
        await _edit_or_send(message.bot, message.chat.id, msg_id, _progress_html('Загрузка файла', 'Загружаю на сервер Suno…'), None, thread_id)
        url, lbl, err = await _extract_and_upload_media(
            message, accept=('audio', 'voice', 'video', 'photo', 'document', 'url'), use_base64_small=True,
        )
        if err or not url:
            await _edit_or_send(message.bot, message.chat.id, msg_id, f"<b>Ошибка:</b> {escape(err or 'не удалось')}", studio_utils_keyboard(), thread_id)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ui_button("Скопировать URL", copy_text=url[:256])],
            [ui_button("← Утилиты", callback_data="ss:utils")],
        ])
        await _edit_or_send(
            message.bot, message.chat.id, msg_id,
            f"{tg_icon('attachment', '📎')} <b>Файл загружен</b> (хранится 3 дня)\n"
            f"Имя: <code>{escape(lbl or 'file')}</code>\n"
            f"<code>{escape(url)}</code>",
            kb, thread_id,
        )


# Register track actions, stems/MIDI, voices/personas handlers onto music_studio_router
import handlers.music_studio_tracks  # noqa: E402, F401
