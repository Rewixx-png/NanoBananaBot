"""Suno Studio handlers for Track Actions, Stems/MIDI/WAV/MP4/Cover/LRC, Voices/Personas, and Refs."""

import json
import logging
import re
from html import escape
from typing import Any

from aiogram import F, types
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from database.suno import (
    delete_suno_persona,
    delete_suno_voice,
    get_suno_personas,
    get_suno_stems_by_id,
    get_suno_stems_list,
    get_suno_track_by_id,
    get_suno_tracks,
    get_suno_voice_by_id,
    get_suno_voices,
    save_suno_persona,
    save_suno_stems,
    save_suno_track,
    save_suno_voice,
)
from handlers.common import safe_send
from handlers.music_studio import (
    _edit_or_send,
    _extract_and_upload_media,
    _new_sid,
    _parse_seconds,
    _progress_html,
    _reply_kw,
    _run_with_progress,
    _studio_awaiting,
    _studio_sessions,
    create_action_session,
    music_studio_router,
)
from handlers.music_studio_ui import (
    my_tracks_screen,
    persona_detail_keyboard,
    persona_detail_text,
    studio_action_keyboard,
    studio_action_text,
    studio_hub_keyboard,
    studio_stem_mode_keyboard,
    studio_stem_mode_text,
    studio_stem_picker_keyboard,
    studio_stems_hub_keyboard,
    studio_stems_hub_text,
    studio_stems_result_keyboard,
    suno_refs_keyboard,
    suno_refs_text,
    track_actions_keyboard,
    track_actions_text,
    voice_detail_keyboard,
    voice_detail_text,
    voices_hub_keyboard,
    voices_hub_text,
)
from handlers.music_ui import _cut, tg_icon, ui_button
from services.suno_service import (
    SUNO_SINGER_SKILLS,
    SUNO_STEM_NAMES,
    SUNO_VOICE_LANGUAGES,
    convert_to_wav,
    create_music_video,
    format_lrc,
    generate_midi,
    generate_music_cover,
    generate_persona,
    get_timestamped_lyrics,
    recover_audio,
    separate_vocals,
    voice_check_availability,
    voice_generate,
    voice_regenerate,
    voice_validate,
)
from state import pending_suno_configs

logger = logging.getLogger(__name__)


# ── My Tracks & Per-Track Actions ────────────────────────────────────────

@music_studio_router.callback_query(F.data.startswith("ss:tracks:"))
async def ss_tracks_list(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    mode = parts[3] if len(parts) > 3 else 'open'
    await callback.answer()
    tracks = await get_suno_tracks(callback.from_user.id, limit=60)
    text, kb = my_tracks_screen(tracks, page, mode)
    await _edit_or_send(callback.bot, callback.message.chat.id, callback.message.message_id, text, kb)


@music_studio_router.callback_query(F.data == "sst:manual")
async def sst_manual_start(callback: types.CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    _studio_awaiting[(chat_id, callback.from_user.id)] = {
        'mode': 'track_manual',
        'msg_id': callback.message.message_id,
        'thread_id': callback.message.message_thread_id if callback.message.chat.is_forum else None,
    }
    text = (
        f"{tg_icon('search', '🔍')} <b>Открыть трек по ID</b>\n\n"
        "Пришли <code>taskId audioId</code> через пробел (или через перенос строки).\n"
        "Трек сохранится в «Мои треки» и откроется карточка действий."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Мои треки", callback_data="ss:tracks:0")]])
    await _edit_or_send(callback.bot, chat_id, callback.message.message_id, text, kb)


@music_studio_router.callback_query(F.data.startswith("sst:"))
async def sst_track_action(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) < 3 or not parts[2].isdigit():
        await callback.answer()
        return
    action = parts[1]
    track_db_id = int(parts[2])
    track = await get_suno_track_by_id(track_db_id)
    if not track:
        await callback.answer("Трек не найден.", show_alert=True)
        return
    if track['user_id'] != callback.from_user.id:
        await callback.answer("Этот трек принадлежит другому пользователю.", show_alert=True)
        return

    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id if callback.message.chat.is_forum else None

    if action == 'open':
        await callback.answer()
        # If invoked from an audio message, send a fresh control card instead of editing audio caption
        if callback.message.audio or callback.message.document:
            await safe_send(
                callback.bot.send_message, chat_id=chat_id,
                text=track_actions_text(track),
                reply_markup=track_actions_keyboard(track_db_id),
                parse_mode='HTML', **_reply_kw(thread_id),
            )
        else:
            await _edit_or_send(
                callback.bot, chat_id, callback.message.message_id,
                track_actions_text(track), track_actions_keyboard(track_db_id), thread_id,
            )
        return

    msg_id = callback.message.message_id

    if action == 'extend':
        await callback.answer()
        sid, sess = create_action_session(
            'extend', callback.from_user.id, chat_id, msg_id, thread_id,
            task_id=track['task_id'], audio_id=track['audio_id'],
            track_db_id=track_db_id, source_label=track['title'],
            style=track.get('tags', ''), title=track.get('title', ''),
        )
        await _edit_or_send(callback.bot, chat_id, msg_id, studio_action_text(sid, sess), studio_action_keyboard(sid, sess), thread_id)
        return

    if action == 'cover':
        await callback.answer()
        sid, sess = create_action_session(
            'ucover', callback.from_user.id, chat_id, msg_id, thread_id,
            task_id=track['task_id'], audio_id=track['audio_id'],
            track_db_id=track_db_id, source_label=track['title'],
            title=track.get('title', ''),
        )
        await _edit_or_send(callback.bot, chat_id, msg_id, studio_action_text(sid, sess), studio_action_keyboard(sid, sess), thread_id)
        return

    if action == 'replace':
        await callback.answer()
        sid, sess = create_action_session(
            'replace', callback.from_user.id, chat_id, msg_id, thread_id,
            task_id=track['task_id'], audio_id=track['audio_id'],
            track_db_id=track_db_id, source_label=track['title'],
            style=track.get('tags', ''), title=track.get('title', ''),
            full_lyrics=track.get('prompt', ''),
        )
        await _edit_or_send(callback.bot, chat_id, msg_id, studio_action_text(sid, sess), studio_action_keyboard(sid, sess), thread_id)
        return

    if action == 'stems':
        await callback.answer()
        sid = _new_sid()
        _studio_sessions[sid] = {
            'kind': 'stems', 'user_id': callback.from_user.id, 'chat_id': chat_id,
            'msg_id': msg_id, 'thread_id': thread_id,
            'task_id': track['task_id'], 'audio_id': track['audio_id'],
            'audio_url': '', 'source_label': track['title'], 'track_db_id': track_db_id,
        }
        await _edit_or_send(
            callback.bot, chat_id, msg_id,
            studio_stem_mode_text(track['title']),
            studio_stem_mode_keyboard(sid, f"sst:open:{track_db_id}"), thread_id,
        )
        return

    if action == 'wav':
        await callback.answer()
        res, err, cancelled, mid = await _run_with_progress(
            callback.bot, chat_id, msg_id, callback.from_user.id,
            'Конвертация в WAV',
            lambda cb, ev: convert_to_wav(track['task_id'], track['audio_id'], status_cb=cb, cancel_event=ev),
            thread_id,
        )
        if not res:
            return
        if res.get('audio'):
            await safe_send(
                callback.bot.send_document, chat_id=chat_id,
                document=BufferedInputFile(res['audio'], filename=f"{_cut(track['title'] or 'track', 32)}.wav"),
                caption=f"<b>WAV · {escape(track['title'] or 'Suno')}</b>",
                parse_mode='HTML', **_reply_kw(thread_id),
            )
            await _edit_or_send(callback.bot, chat_id, mid, track_actions_text(track), track_actions_keyboard(track_db_id), thread_id)
        else:
            await _edit_or_send(callback.bot, chat_id, mid, f"WAV готов: <code>{escape(res.get('url', ''))}</code>", track_actions_keyboard(track_db_id), thread_id)
        return

    if action == 'mp4':
        await callback.answer()
        _studio_awaiting[(chat_id, callback.from_user.id)] = {
            'mode': 'mp4_branding', 'track_db_id': track_db_id,
            'msg_id': msg_id, 'thread_id': thread_id,
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ui_button("Создать без подписей", callback_data=f"sstmp4go:{track_db_id}", style="success", icon="play")],
            [ui_button("← К треку", callback_data=f"sst:open:{track_db_id}")],
        ])
        await _edit_or_send(
            callback.bot, chat_id, msg_id,
            f"{tg_icon('play', '▶')} <b>Клип MP4 · {escape(track['title'])}</b>\n\n"
            "Нажми кнопку для быстрого старта или пришли <code>Автор | Сайт</code> (до 50 симв. каждый).",
            kb, thread_id,
        )
        return

    if action == 'art':
        await callback.answer()
        imgs, err, cancelled, mid = await _run_with_progress(
            callback.bot, chat_id, msg_id, callback.from_user.id,
            'Генерация обложек',
            lambda cb, ev: generate_music_cover(track['task_id'], status_cb=cb, cancel_event=ev),
            thread_id,
        )
        if not imgs:
            return
        for i, raw in enumerate(imgs, 1):
            await safe_send(
                callback.bot.send_photo, chat_id=chat_id,
                photo=BufferedInputFile(raw, filename=f"cover_{i}.jpg"),
                caption=f"<b>Обложка {i} · {escape(track['title'])}</b>",
                parse_mode='HTML', **_reply_kw(thread_id),
            )
        await _edit_or_send(callback.bot, chat_id, mid, track_actions_text(track), track_actions_keyboard(track_db_id), thread_id)
        return

    if action == 'lrc':
        await callback.answer("Загружаю таймкоды…")
        data, err = await get_timestamped_lyrics(track['task_id'], track['audio_id'])
        if err or not data:
            await _edit_or_send(callback.bot, chat_id, msg_id, f"<b>Таймкоды недоступны:</b> {escape(err or 'нет данных')}", track_actions_keyboard(track_db_id), thread_id)
            return
        words = data.get('alignedWords') or []
        lrc_text = format_lrc(words)
        if not lrc_text:
            await _edit_or_send(callback.bot, chat_id, msg_id, "У этого трека нет вокальной лирики (инструментал).", track_actions_keyboard(track_db_id), thread_id)
            return
        await safe_send(
            callback.bot.send_document, chat_id=chat_id,
            document=BufferedInputFile(lrc_text.encode('utf-8'), filename=f"{_cut(track['title'] or 'lyrics', 30)}.lrc"),
            caption=f"<b>Таймкоды (.lrc) · {escape(track['title'])}</b>\nСлов: <code>{len(words)}</code>",
            parse_mode='HTML', **_reply_kw(thread_id),
        )
        preview = lrc_text[:1200]
        await _edit_or_send(
            callback.bot, chat_id, msg_id,
            f"{tg_icon('pencil', '✎')} <b>Таймкоды · {escape(track['title'])}</b>\n<blockquote expandable>{escape(preview)}</blockquote>",
            track_actions_keyboard(track_db_id), thread_id,
        )
        return

    if action == 'persona':
        await callback.answer()
        _studio_awaiting[(chat_id, callback.from_user.id)] = {
            'mode': 'persona_create', 'track_db_id': track_db_id,
            'msg_id': msg_id, 'thread_id': thread_id,
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← К треку", callback_data=f"sst:open:{track_db_id}")]])
        await _edit_or_send(
            callback.bot, chat_id, msg_id,
            f"{tg_icon('sparkle', '✦')} <b>Создать персону из трека</b>\n\n"
            "Пришли данные через <code>|</code>:\n"
            "<code>Имя | Описание характера и стиля | 0-30</code>\n"
            "<i>Последний параметр (секунды анализа) необязателен, по умолчанию 0-30.</i>",
            kb, thread_id,
        )
        return

    if action == 'recover':
        await callback.answer()
        res, err, cancelled, mid = await _run_with_progress(
            callback.bot, chat_id, msg_id, callback.from_user.id,
            'Восстановление аудио',
            lambda cb, ev: recover_audio(track['task_id'], status_cb=cb, cancel_event=ev),
            thread_id,
        )
        if not res:
            return
        ok = [t for t in res if t.get('audio')]
        for idx, item in enumerate(ok, 1):
            await safe_send(
                callback.bot.send_audio, chat_id=chat_id,
                audio=BufferedInputFile(item['audio'], filename=f"recovered_{idx}.mp3"),
                caption=f"Восстановлено · <code>{escape(item['id'][:14])}</code>",
                parse_mode='HTML', **_reply_kw(thread_id),
            )
        await _edit_or_send(callback.bot, chat_id, mid, track_actions_text(track), track_actions_keyboard(track_db_id), thread_id)


@music_studio_router.callback_query(F.data.startswith("sstmp4go:"))
async def sst_mp4_quick(callback: types.CallbackQuery):
    track_db_id = int(callback.data.split(':', 1)[1])
    _studio_awaiting.pop((callback.message.chat.id, callback.from_user.id), None)
    track = await get_suno_track_by_id(track_db_id, callback.from_user.id)
    if not track:
        await callback.answer("Трек не найден.", show_alert=True)
        return
    await callback.answer()
    await _run_mp4(callback.bot, callback.message.chat.id, callback.message.message_id, callback.from_user.id, track, '', '',
                   callback.message.message_thread_id if callback.message.chat.is_forum else None)


async def _run_mp4(bot, chat_id: int, msg_id: int, uid: int, track: dict, author: str, domain: str, thread_id=None):
    res, err, cancelled, mid = await _run_with_progress(
        bot, chat_id, msg_id, uid, 'Клип MP4',
        lambda cb, ev: create_music_video(track['task_id'], track['audio_id'], author=author, domain_name=domain, status_cb=cb, cancel_event=ev),
        thread_id,
    )
    if not res:
        return
    if res.get('video'):
        await safe_send(
            bot.send_video, chat_id=chat_id,
            video=BufferedInputFile(res['video'], filename=f"{_cut(track['title'] or 'clip', 28)}.mp4"),
            caption=f"<b>Клип MP4 · {escape(track['title'] or 'Suno')}</b>",
            parse_mode='HTML', **_reply_kw(thread_id),
        )
        await _edit_or_send(bot, chat_id, mid, track_actions_text(track), track_actions_keyboard(track['id']), thread_id)
    else:
        await _edit_or_send(bot, chat_id, mid, f"Клип готов: <code>{escape(res.get('url', ''))}</code>", track_actions_keyboard(track['id']), thread_id)


# ── Stems & MIDI ─────────────────────────────────────────────────────────

@music_studio_router.callback_query(F.data == "ss:stems")
async def ss_stems_hub(callback: types.CallbackQuery):
    _studio_awaiting.pop((callback.message.chat.id, callback.from_user.id), None)
    await callback.answer()
    recent = await get_suno_stems_list(callback.from_user.id, limit=8)
    await _edit_or_send(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        studio_stems_hub_text(recent), studio_stems_hub_keyboard(recent),
    )


@music_studio_router.callback_query(F.data == "sssep:upload")
async def sssep_upload_start(callback: types.CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    _studio_awaiting[(chat_id, callback.from_user.id)] = {
        'mode': 'stems_upload',
        'msg_id': callback.message.message_id,
        'thread_id': callback.message.message_thread_id if callback.message.chat.is_forum else None,
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Стемы", callback_data="ss:stems")]])
    await _edit_or_send(
        callback.bot, chat_id, callback.message.message_id,
        f"{tg_icon('attachment', '📎')} <b>Загрузка аудио для стемов</b>\n\n"
        "Пришли аудиофайл/голосовое/документ (до 20 МБ) или прямую ссылку HTTP(S).",
        kb,
    )


@music_studio_router.callback_query(F.data.startswith("sssep:view:"))
async def sssep_view(callback: types.CallbackQuery):
    stems_db_id = int(callback.data.split(':', 2)[2])
    rec = await get_suno_stems_by_id(stems_db_id, callback.from_user.id)
    if not rec:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    await callback.answer()
    names = ', '.join(s.get('name', 'Stem') for s in rec['stems'][:12])
    text = (
        f"{tg_icon('chart', '🎛')} <b>Стемы · {escape(rec['source_title'] or rec['task_id'][:12])}</b>\n"
        f"Режим: <code>{escape(rec['sep_type'])}</code> · Дорожек: <code>{len(rec['stems'])}</code>\n"
        f"<i>{escape(_cut(names, 200))}</i>\n\n"
        "Можно сгенерировать MIDI или конвертировать любой стем в WAV:"
    )
    await _edit_or_send(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        text, studio_stems_result_keyboard(stems_db_id, rec['stems']),
    )


@music_studio_router.callback_query(F.data.startswith("sssep:"))
async def sssep_cb(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    action = parts[1]
    sid = parts[2] if len(parts) > 2 else ''
    sess = _studio_sessions.get(sid)
    if not sess or sess.get('user_id') != callback.from_user.id:
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    chat_id, msg_id = sess['chat_id'], sess['msg_id']
    thread_id = sess.get('thread_id')

    if action == 'modes':
        _studio_awaiting.pop((chat_id, sess['user_id']), None)
        await callback.answer()
        back_cb = f"sst:open:{sess['track_db_id']}" if sess.get('track_db_id') else "ss:stems"
        await _edit_or_send(callback.bot, chat_id, msg_id, studio_stem_mode_text(sess['source_label']), studio_stem_mode_keyboard(sid, back_cb), thread_id)
        return

    if action == 'pick':
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await callback.answer()
        _studio_awaiting[(chat_id, sess['user_id'])] = {
            'mode': 'stem_search', 'sid': sid, 'msg_id': msg_id, 'thread_id': thread_id,
        }
        text, kb = studio_stem_picker_keyboard(sid, page, sess.get('stem_query', ''))
        await _edit_or_send(callback.bot, chat_id, msg_id, text, kb, thread_id)
        return

    if action == 'go':
        sep_type = parts[3]
        await callback.answer()
        await _execute_separation(callback.bot, sess, sep_type, '')
        return

    if action == 'adv':
        idx = int(parts[3])
        stem_name = SUNO_STEM_NAMES[idx] if 0 <= idx < len(SUNO_STEM_NAMES) else 'Lead Vocal'
        _studio_awaiting.pop((chat_id, sess['user_id']), None)
        await callback.answer(f"Выбран: {stem_name}")
        await _execute_separation(callback.bot, sess, 'split_stem_advanced', stem_name)


async def _execute_separation(bot, sess: dict, sep_type: str, stem_name: str):
    chat_id, msg_id, uid = sess['chat_id'], sess['msg_id'], sess['user_id']
    thread_id = sess.get('thread_id')
    label = f"Стемы · {stem_name}" if stem_name else f"Стемы ({sep_type})"
    res, err, cancelled, mid = await _run_with_progress(
        bot, chat_id, msg_id, uid, label,
        lambda cb, ev: separate_vocals(
            sep_type,
            task_id=sess.get('task_id', ''), audio_id=sess.get('audio_id', ''),
            audio_url=sess.get('audio_url', ''), stem_name=stem_name,
            status_cb=cb, cancel_event=ev,
        ),
        thread_id,
    )
    if not res:
        return
    stems_meta = [{'id': s.get('id', ''), 'name': s.get('name', 'Stem'), 'url': s.get('url', '')} for s in res['stems']]
    stems_db_id = await save_suno_stems(uid, res['task_id'], sess.get('source_label', 'Audio'), sep_type, stems_meta)

    for idx, st in enumerate(res['stems'], 1):
        if st.get('audio'):
            safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', st['name'])[:30] or f'stem_{idx}'
            await safe_send(
                bot.send_audio, chat_id=chat_id,
                audio=BufferedInputFile(st['audio'], filename=f"{safe_name}.mp3"),
                title=st['name'][:64], performer='Suno Stems',
                caption=f"<b>{escape(st['name'])}</b> · <i>{escape(_cut(sess.get('source_label', ''), 60))}</i>",
                parse_mode='HTML', **_reply_kw(thread_id),
            )
    if stems_db_id:
        await _edit_or_send(
            bot, chat_id, mid,
            f"{tg_icon('chart', '🎛')} <b>Стемы готовы</b> ({len(res['stems'])} дорожек)\nМожно извлечь MIDI или WAV:",
            studio_stems_result_keyboard(stems_db_id, stems_meta), thread_id,
        )


@music_studio_router.callback_query(F.data.startswith("ssmidi:"))
async def ssmidi_run(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    scope = parts[1]  # all | one
    stems_db_id = int(parts[2])
    rec = await get_suno_stems_by_id(stems_db_id, callback.from_user.id)
    if not rec:
        await callback.answer("Запись стемов не найдена.", show_alert=True)
        return
    audio_id = ''
    stem_label = 'all'
    if scope == 'one' and len(parts) > 3:
        idx = int(parts[3])
        if 0 <= idx < len(rec['stems']):
            audio_id = rec['stems'][idx].get('id', '')
            stem_label = rec['stems'][idx].get('name', 'stem')

    await callback.answer()
    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id if callback.message.chat.is_forum else None
    res, err, cancelled, mid = await _run_with_progress(
        callback.bot, chat_id, callback.message.message_id, callback.from_user.id,
        f'MIDI · {stem_label}',
        lambda cb, ev: generate_midi(rec['task_id'], audio_id=audio_id, status_cb=cb, cancel_event=ev),
        thread_id,
    )
    if not res:
        return
    midi_data = res.get('midi_data') or {}
    instruments = midi_data.get('instruments') or []
    summary = ', '.join(f"{i.get('name', '?')} ({len(i.get('notes') or [])})" for i in instruments[:8])
    safe_base = re.sub(r'[^A-Za-z0-9_-]+', '_', f"{rec['source_title']}_{stem_label}")[:36] or 'suno'
    if res.get('midi_bytes'):
        await safe_send(
            callback.bot.send_document, chat_id=chat_id,
            document=BufferedInputFile(res['midi_bytes'], filename=f"{safe_base}.mid"),
            caption=f"<b>MIDI · {escape(rec['source_title'] or 'Suno')}</b>\n<i>{escape(_cut(summary or 'Готово', 250))}</i>",
            parse_mode='HTML', **_reply_kw(thread_id),
        )
    json_bytes = json.dumps(midi_data, ensure_ascii=False, indent=2).encode('utf-8')
    await safe_send(
        callback.bot.send_document, chat_id=chat_id,
        document=BufferedInputFile(json_bytes, filename=f"{safe_base}_notes.json"),
        caption="Полные MIDI-ноты (JSON)", **_reply_kw(thread_id),
    )
    await _edit_or_send(
        callback.bot, chat_id, mid,
        f"{tg_icon('music', '♪')} <b>MIDI готов</b> · инструментов: <code>{len(instruments)}</code>",
        studio_stems_result_keyboard(stems_db_id, rec['stems']), thread_id,
    )


@music_studio_router.callback_query(F.data.startswith("ssswav:"))
async def ssswav_run(callback: types.CallbackQuery):
    _, db_id_s, idx_s = callback.data.split(':', 2)
    rec = await get_suno_stems_by_id(int(db_id_s), callback.from_user.id)
    if not rec:
        await callback.answer("Стемы не найдены.", show_alert=True)
        return
    idx = int(idx_s)
    if not (0 <= idx < len(rec['stems'])) or not rec['stems'][idx].get('id'):
        await callback.answer("У этого стема нет audioId.", show_alert=True)
        return
    stem = rec['stems'][idx]
    await callback.answer()
    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id if callback.message.chat.is_forum else None
    res, err, cancelled, mid = await _run_with_progress(
        callback.bot, chat_id, callback.message.message_id, callback.from_user.id,
        f"WAV · {stem['name']}",
        lambda cb, ev: convert_to_wav(rec['task_id'], stem['id'], status_cb=cb, cancel_event=ev),
        thread_id,
    )
    if not res:
        return
    if res.get('audio'):
        await safe_send(
            callback.bot.send_document, chat_id=chat_id,
            document=BufferedInputFile(res['audio'], filename=f"{_cut(stem['name'], 28)}.wav"),
            caption=f"<b>WAV стем · {escape(stem['name'])}</b>",
            parse_mode='HTML', **_reply_kw(thread_id),
        )
    await _edit_or_send(callback.bot, chat_id, mid, "Стем сконвертирован в WAV.", studio_stems_result_keyboard(rec['id'], rec['stems']), thread_id)


# ── Voices & Personas (6 Suno Voice Endpoints + Personas) ────────────────

@music_studio_router.callback_query(F.data == "ss:voices")
async def ss_voices_hub(callback: types.CallbackQuery):
    _studio_awaiting.pop((callback.message.chat.id, callback.from_user.id), None)
    await callback.answer()
    personas = await get_suno_personas(callback.from_user.id)
    voices = await get_suno_voices(callback.from_user.id)
    await _edit_or_send(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        voices_hub_text(personas, voices), voices_hub_keyboard(personas, voices),
    )


@music_studio_router.callback_query(F.data == "ssv:new")
async def ssv_new_start(callback: types.CallbackQuery):
    await callback.answer()
    sid = _new_sid()
    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id if callback.message.chat.is_forum else None
    _studio_sessions[sid] = {
        'kind': 'voice_clone', 'user_id': callback.from_user.id,
        'chat_id': chat_id, 'msg_id': callback.message.message_id, 'thread_id': thread_id,
        'language': 'ru', 'skill': 'beginner', 'start_s': 0, 'end_s': 20,
        'voice_url': '', 'task_id': '', 'validate_info': '',
    }
    rows = []
    for i in range(0, len(SUNO_VOICE_LANGUAGES), 2):
        rows.append([
            ui_button(lbl, callback_data=f"ssv:lang:{sid}:{code}")
            for code, lbl in SUNO_VOICE_LANGUAGES[i:i + 2]
        ])
    rows.append([ui_button("← Назад", callback_data="ss:voices")])
    await _edit_or_send(
        callback.bot, chat_id, callback.message.message_id,
        f"{tg_icon('microphone', '🎙')} <b>Новый голос Suno</b> · <i>шаг 1/3</i>\n\n"
        "Выбери язык проверочной фразы:",
        InlineKeyboardMarkup(inline_keyboard=rows), thread_id,
    )


@music_studio_router.callback_query(F.data.startswith("ssv:"))
async def ssv_cb(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    action = parts[1]
    chat_id = callback.message.chat.id
    msg_id = callback.message.message_id
    uid = callback.from_user.id
    thread_id = callback.message.message_thread_id if callback.message.chat.is_forum else None

    if action == 'lang':
        sid, lang = parts[2], parts[3]
        sess = _studio_sessions.get(sid)
        if not sess or sess['user_id'] != uid:
            await callback.answer("Сессия устарела.", show_alert=True)
            return
        sess['language'] = lang
        await callback.answer()
        _studio_awaiting[(chat_id, uid)] = {
            'mode': 'voice_src', 'sid': sid, 'msg_id': msg_id, 'thread_id': thread_id,
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Назад", callback_data="ss:voices")]])
        await _edit_or_send(
            callback.bot, chat_id, msg_id,
            f"{tg_icon('microphone', '🎙')} <b>Новый голос Suno</b> · <i>шаг 2/3</i>\n\n"
            "Пришли исходное аудио/голосовое с вокалом (или URL).\n"
            "В подписи можно указать отрезок в секундах, например <code>0-25</code> (по умолчанию <code>0-20</code>).",
            kb, thread_id,
        )
        return

    if action == 'regen':
        sid = parts[2]
        sess = _studio_sessions.get(sid)
        if not sess or sess['user_id'] != uid or not sess.get('task_id'):
            await callback.answer("Сессия устарела.", show_alert=True)
            return
        await callback.answer()
        data, err, cancelled, mid = await _run_with_progress(
            callback.bot, chat_id, msg_id, uid, 'Новая фраза',
            lambda cb, ev: voice_regenerate(sess['task_id'], status_cb=cb, cancel_event=ev), thread_id,
        )
        if not data:
            return
        sess['task_id'] = data.get('taskId') or sess['task_id']
        sess['validate_info'] = data.get('validateInfo') or ''
        await _show_voice_phrase_step(callback.bot, sid, sess, mid)
        return

    if action == 'skill':
        sid, skill = parts[2], parts[3]
        sess = _studio_sessions.get(sid)
        if not sess or sess['user_id'] != uid:
            await callback.answer("Сессия устарела.", show_alert=True)
            return
        sess['skill'] = skill
        await callback.answer()
        await _show_voice_phrase_step(callback.bot, sid, sess, msg_id)
        return

    if action == 'view':
        vid = int(parts[2])
        v = await get_suno_voice_by_id(vid, uid)
        if not v:
            await callback.answer("Голос не найден.", show_alert=True)
            return
        await callback.answer()
        await _edit_or_send(callback.bot, chat_id, msg_id, voice_detail_text(v), voice_detail_keyboard(v), thread_id)
        return

    if action == 'check':
        vid = int(parts[2])
        v = await get_suno_voice_by_id(vid, uid)
        if not v:
            await callback.answer("Голос не найден.", show_alert=True)
            return
        avail, err = await voice_check_availability(v['task_id'])
        if err is not None:
            await callback.answer(_cut(err, 180), show_alert=True)
            return
        await callback.answer("Доступен!" if avail else "Пока недоступен")
        await _edit_or_send(callback.bot, chat_id, msg_id, voice_detail_text(v, avail=avail), voice_detail_keyboard(v), thread_id)
        return

    if action == 'del':
        vid = int(parts[2])
        await delete_suno_voice(vid, uid)
        await callback.answer("Голос удалён")
        personas = await get_suno_personas(uid)
        voices = await get_suno_voices(uid)
        await _edit_or_send(callback.bot, chat_id, msg_id, voices_hub_text(personas, voices), voices_hub_keyboard(personas, voices), thread_id)
        return

    if action == 'pview':
        pid = int(parts[2])
        personas = await get_suno_personas(uid)
        p = next((x for x in personas if x['id'] == pid), None)
        if not p:
            await callback.answer("Персона не найдена.", show_alert=True)
            return
        await callback.answer()
        await _edit_or_send(callback.bot, chat_id, msg_id, persona_detail_text(p), persona_detail_keyboard(p), thread_id)
        return

    if action == 'pdel':
        pid = int(parts[2])
        await delete_suno_persona(pid, uid)
        await callback.answer("Персона удалена")
        personas = await get_suno_personas(uid)
        voices = await get_suno_voices(uid)
        await _edit_or_send(callback.bot, chat_id, msg_id, voices_hub_text(personas, voices), voices_hub_keyboard(personas, voices), thread_id)


async def _show_voice_phrase_step(bot, sid: str, sess: dict, msg_id: int):
    _studio_awaiting[(sess['chat_id'], sess['user_id'])] = {
        'mode': 'voice_verify', 'sid': sid, 'msg_id': msg_id, 'thread_id': sess.get('thread_id'),
    }
    skill = sess.get('skill', 'beginner')
    next_idx = ([k for k, _ in SUNO_SINGER_SKILLS].index(skill) + 1) % len(SUNO_SINGER_SKILLS)
    next_skill, _ = SUNO_SINGER_SKILLS[next_idx]
    cur_skill_lbl = dict(SUNO_SINGER_SKILLS).get(skill, skill)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            ui_button("Другая фраза", callback_data=f"ssv:regen:{sid}", icon="sparkle"),
            ui_button(f"Уровень: {cur_skill_lbl}", callback_data=f"ssv:skill:{sid}:{next_skill}"),
        ],
        [ui_button("← Голоса", callback_data="ss:voices")],
    ])
    text = (
        f"{tg_icon('microphone', '🎙')} <b>Проверочная фраза</b> · <i>шаг 3/3</i>\n\n"
        f"<blockquote>{escape(sess.get('validate_info') or '—')}</blockquote>\n\n"
        "Пропой (или запиши) эту фразу и пришли голосовым/аудио.\n"
        "<i>В подписи можно указать имя голоса, например:</i> <code>Мой рок-вокал</code>"
    )
    await _edit_or_send(bot, sess['chat_id'], msg_id, text, kb, sess.get('thread_id'))


# ── Extra Input Dispatcher (called from handlers/music_studio.py) ────────

async def handle_studio_extra_input(message: types.Message, wait: dict) -> bool:
    mode = wait.get('mode')
    if mode not in (
        'track_manual', 'mp4_branding', 'persona_create',
        'stems_upload', 'stem_search', 'voice_src', 'voice_verify', 'suno_ref_upload',
    ):
        return False

    chat_id = message.chat.id
    uid = message.from_user.id
    msg_id = wait.get('msg_id')
    thread_id = wait.get('thread_id')
    text = (message.text or message.caption or '').strip()

    try:
        await message.delete()
    except Exception:
        pass

    if mode == 'track_manual':
        parts = text.split()
        if len(parts) < 2:
            _studio_awaiting[(chat_id, uid)] = wait
            await _edit_or_send(
                message.bot, chat_id, msg_id,
                "Нужны оба значения: <code>taskId audioId</code> через пробел.",
                InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Мои треки", callback_data="ss:tracks:0")]]),
                thread_id,
            )
            return True
        db_id = await save_suno_track(uid, chat_id, parts[0], parts[1], title=f"Track {parts[1][:8]}")
        track = await get_suno_track_by_id(db_id, uid) if db_id else None
        if track:
            await _edit_or_send(message.bot, chat_id, msg_id, track_actions_text(track), track_actions_keyboard(track['id']), thread_id)
        return True

    if mode == 'mp4_branding':
        track = await get_suno_track_by_id(wait['track_db_id'], uid)
        if not track:
            return True
        pieces = [p.strip() for p in text.split('|')]
        author = pieces[0][:50] if pieces else ''
        domain = pieces[1][:50] if len(pieces) > 1 else ''
        await _run_mp4(message.bot, chat_id, msg_id, uid, track, author, domain, thread_id)
        return True

    if mode == 'persona_create':
        track = await get_suno_track_by_id(wait['track_db_id'], uid)
        if not track:
            return True
        pieces = [p.strip() for p in text.split('|')]
        name = (pieces[0] if pieces and pieces[0] else track['title'] or 'My Persona')[:80]
        desc = (pieces[1] if len(pieces) > 1 and pieces[1] else f"Style of {track['title']}")[:500]
        v_start, v_end = 0.0, 30.0
        if len(pieces) > 2:
            nums = re.findall(r'\d+(?:\.\d+)?', pieces[2])
            if len(nums) >= 2:
                v_start, v_end = float(nums[0]), float(nums[1])
        await _edit_or_send(message.bot, chat_id, msg_id, _progress_html('Создание персоны', 'Анализирую трек…'), None, thread_id)
        data, err = await generate_persona(
            track['task_id'], track['audio_id'], name, desc,
            vocal_start=v_start, vocal_end=v_end, style=track.get('tags', ''),
        )
        if err or not data or not data.get('personaId'):
            await _edit_or_send(message.bot, chat_id, msg_id, f"<b>Ошибка:</b> {escape(err or 'не удалось создать')}", track_actions_keyboard(track['id']), thread_id)
            return True
        await save_suno_persona(
            uid, data['personaId'], data.get('name') or name,
            data.get('description') or desc, track.get('tags', ''),
            track['task_id'], track['audio_id'],
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ui_button("Голоса и персоны", callback_data="ss:voices", style="primary")],
            [ui_button("← К треку", callback_data=f"sst:open:{track['id']}")],
        ])
        await _edit_or_send(
            message.bot, chat_id, msg_id,
            f"{tg_icon('sparkle', '✦')} <b>Персона создана!</b>\n"
            f"Имя: <b>{escape(name)}</b>\n"
            f"ID: <code>{escape(data['personaId'])}</code>\n\n"
            "Теперь её можно выбрать в настройках генерации /music.",
            kb, thread_id,
        )
        return True

    if mode == 'stems_upload':
        await _edit_or_send(message.bot, chat_id, msg_id, _progress_html('Загрузка аудио', 'Отправляю файл…'), None, thread_id)
        url, lbl, err = await _extract_and_upload_media(message, accept=('audio', 'voice', 'document', 'url'))
        if err or not url:
            _studio_awaiting[(chat_id, uid)] = wait
            await _edit_or_send(
                message.bot, chat_id, msg_id,
                f"<b>Ошибка:</b> {escape(err or 'не удалось загрузить')}\nПопробуй ещё раз.",
                InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Стемы", callback_data="ss:stems")]]),
                thread_id,
            )
            return True
        sid = _new_sid()
        _studio_sessions[sid] = {
            'kind': 'stems', 'user_id': uid, 'chat_id': chat_id,
            'msg_id': msg_id, 'thread_id': thread_id,
            'task_id': '', 'audio_id': '', 'audio_url': url,
            'source_label': lbl or 'Audio', 'track_db_id': None,
        }
        await _edit_or_send(message.bot, chat_id, msg_id, studio_stem_mode_text(lbl or 'Audio'), studio_stem_mode_keyboard(sid, 'ss:stems'), thread_id)
        return True

    if mode == 'stem_search':
        sid = wait['sid']
        sess = _studio_sessions.get(sid)
        if not sess:
            return True
        sess['stem_query'] = '' if text in ('*', 'все', 'сброс') else text
        _studio_awaiting[(chat_id, uid)] = wait
        t_out, kb = studio_stem_picker_keyboard(sid, 0, sess['stem_query'])
        await _edit_or_send(message.bot, chat_id, msg_id, t_out, kb, thread_id)
        return True

    if mode == 'voice_src':
        sid = wait['sid']
        sess = _studio_sessions.get(sid)
        if not sess:
            return True
        await _edit_or_send(message.bot, chat_id, msg_id, _progress_html('Загрузка вокала', 'Загружаю аудио…'), None, thread_id)
        url, lbl, err = await _extract_and_upload_media(message, accept=('audio', 'voice', 'document', 'url'))
        if err or not url:
            _studio_awaiting[(chat_id, uid)] = wait
            await _edit_or_send(
                message.bot, chat_id, msg_id,
                f"<b>Ошибка:</b> {escape(err or 'не удалось загрузить')}\nПришли аудио/голосовое или URL.",
                InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Голоса", callback_data="ss:voices")]]),
                thread_id,
            )
            return True
        nums = re.findall(r'\d+', text)
        start_s = int(nums[0]) if len(nums) >= 2 else 0
        end_s = int(nums[1]) if len(nums) >= 2 and int(nums[1]) > start_s else max(start_s + 20, 20)
        sess['voice_url'] = url
        sess['start_s'], sess['end_s'] = start_s, end_s
        data, err, cancelled, mid = await _run_with_progress(
            message.bot, chat_id, msg_id, uid, 'Подготовка голоса',
            lambda cb, ev: voice_validate(url, start_s, end_s, language=sess['language'], status_cb=cb, cancel_event=ev),
            thread_id,
        )
        if not data:
            return True
        sess['task_id'] = data.get('taskId') or ''
        sess['validate_info'] = data.get('validateInfo') or ''
        await _show_voice_phrase_step(message.bot, sid, sess, mid)
        return True

    if mode == 'voice_verify':
        sid = wait['sid']
        sess = _studio_sessions.get(sid)
        if not sess:
            return True
        await _edit_or_send(message.bot, chat_id, msg_id, _progress_html('Загрузка записи', 'Отправляю верификацию…'), None, thread_id)
        url, lbl, err = await _extract_and_upload_media(message, accept=('audio', 'voice', 'document', 'url'))
        if err or not url:
            _studio_awaiting[(chat_id, uid)] = wait
            await _edit_or_send(
                message.bot, chat_id, msg_id,
                f"<b>Ошибка:</b> {escape(err or 'пришли голосовое/аудио')}",
                InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Голоса", callback_data="ss:voices")]]),
                thread_id,
            )
            return True
        voice_name = text[:80] if text and not text.startswith('http') else f"Голос {sid[:4]}"
        data, err, cancelled, mid = await _run_with_progress(
            message.bot, chat_id, msg_id, uid, 'Обучение голоса Suno',
            lambda cb, ev: voice_generate(
                sess['task_id'], url, voice_name=voice_name,
                singer_skill_level=sess.get('skill', 'beginner'),
                status_cb=cb, cancel_event=ev,
            ),
            thread_id,
        )
        if not data or not data.get('voiceId'):
            return True
        vid = await save_suno_voice(
            uid, data.get('taskId') or sess['task_id'], data['voiceId'],
            voice_name, skill_level=sess.get('skill', 'beginner'), language=sess.get('language', 'ru'),
        )
        v_obj = await get_suno_voice_by_id(vid, uid) if vid else None
        if v_obj:
            await _edit_or_send(message.bot, chat_id, mid, voice_detail_text(v_obj), voice_detail_keyboard(v_obj), thread_id)
        else:
            await _edit_or_send(message.bot, chat_id, mid, f"Голос создан: <code>{escape(data['voiceId'])}</code>", studio_hub_keyboard(), thread_id)
        return True

    if mode == 'suno_ref_upload':
        req_id = wait['request_id']
        ref_kind = wait['ref_kind']
        d = pending_suno_configs.get(req_id)
        if not d:
            return True
        accept_map = {
            'image': ('photo', 'document', 'url'),
            'video': ('video', 'document', 'url'),
            'audio': ('audio', 'voice', 'document', 'url'),
            'midi': ('document', 'url'),
        }
        await _edit_or_send(message.bot, chat_id, msg_id, _progress_html('Загрузка референса', 'Отправляю файл…'), None, thread_id)
        url, lbl, err = await _extract_and_upload_media(message, accept=accept_map.get(ref_kind, ('document', 'url')))
        if err or not url:
            _studio_awaiting[(chat_id, uid)] = wait
            await _edit_or_send(
                message.bot, chat_id, msg_id,
                f"<b>Ошибка:</b> {escape(err or 'не удалось загрузить')}\nПопробуй ещё раз.",
                InlineKeyboardMarkup(inline_keyboard=[[ui_button("← Референсы", callback_data=f"sunorefs:{req_id}")]]),
                thread_id,
            )
            return True
        cfg = d['cfg']
        cfg['custom_mode'] = '0'
        key_map = {'image': ('image_urls', 5), 'video': ('video_urls', 1), 'audio': ('audio_urls', 5), 'midi': ('midi_urls', 1)}
        cfg_key, limit = key_map[ref_kind]
        arr = list(cfg.get(cfg_key) or [])
        if len(arr) >= limit:
            arr.pop(0)
        arr.append(url)
        cfg[cfg_key] = arr
        await _edit_or_send(message.bot, chat_id, msg_id, suno_refs_text(req_id, d), suno_refs_keyboard(req_id, d), thread_id)
        return True

    return False
