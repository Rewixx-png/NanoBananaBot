"""Extended Suno API endpoints: lyrics, style boost, covers, recovery,
WAV, stem separation, MIDI (+ SMF encoder), MP4 video, and Suno Voice.
"""

import logging
import struct
from typing import Any, Optional, Tuple

import aiohttp

from services.suno_service import (
    SUNO_POLL_INTERVAL,
    SUNO_POLL_TIMEOUT,
    _PLACEHOLDER_CALLBACK,
    _call_with_keys,
    _download_bytes,
    _get,
    _is_cancelled,
    _wait_or_cancel,
)

logger = logging.getLogger(__name__)

SUNO_STEM_NAMES: tuple[str, ...] = (
    'Lead Vocal', 'Drum Kit', 'Kick', 'Snare', 'Risers', 'Bass', 'Backing Vocals',
    'Piano', 'Electric Guitar', 'Percussion', 'String Section', 'Synth',
    'Acoustic Guitar', 'Sound Effects', 'Synth Pad', 'Synth Bass', 'Guitar',
    'Brass Section', 'Organ', 'Electronic Drum Kit', 'Lead Electric Guitar',
    'Synth Keys', 'Rhythm Electric Guitar', 'Electric Piano', 'Upright Bass',
    'Keyboards', 'Distorted Electric Guitar', 'Synth Strings', 'Synth Lead',
    'Woodwinds', 'Rhythm Acoustic Guitar', 'Flute', 'Harp', 'Tambourine',
    'Trumpet', 'Arpeggiator', 'Accordion', 'Fiddle', 'Pedal Steel Guitar',
    'Synth Voice', 'Violin', 'Digital Piano', 'Synth Brass', 'Mandolin', 'Choir',
    'Banjo', 'Bells', 'Clarinet', 'Tenor Saxophone', 'Trombone', 'Shaker',
    'French Horn', 'Glockenspiel', 'Electric Bass', 'Cello', 'Timpani',
    'Harmonica', 'Marimba', 'Vibraphone', 'Lap Steel Guitar', 'Saxophone',
    'Orchestra', 'Horns', 'Cymbals', 'Hand Clap', 'Oboe', 'Celesta', 'Congas',
    'Drone', 'Alto Saxophone', 'Double Bass', 'Ukulele', 'Harpsichord',
    'Baritone Saxophone', 'Xylophone', 'Tuba', 'Bass Guitar', 'Whistle',
    'Lead Guitar', 'Rhodes', '808', 'Bongos', 'Bassoon', 'Cowbell', 'Viola',
    'Sitar', 'Steel Drums', 'Piccolo', 'Theremin', 'Bagpipes', 'Hi-Hat',
    'Music Box', 'Melodica', 'Tabla', 'Koto', 'Djembe', 'Taiko', 'Didgeridoo',
)

SUNO_VOICE_LANGUAGES: tuple[tuple[str, str], ...] = (
    ('ru', 'Русский'),
    ('en', 'English'),
    ('zh', '中文'),
    ('es', 'Español'),
    ('fr', 'Français'),
    ('de', 'Deutsch'),
    ('pt', 'Português'),
    ('ja', '日本語'),
    ('ko', '한국어'),
    ('hi', 'हिन्दी'),
)

SUNO_SINGER_SKILLS: tuple[tuple[str, str], ...] = (
    ('beginner', 'Начальный'),
    ('intermediate', 'Средний'),
    ('advanced', 'Продвинутый'),
    ('professional', 'Профи'),
)

_STEM_URL_FIELDS: tuple[tuple[str, str], ...] = (
    ('vocalUrl', 'Vocals'),
    ('instrumentalUrl', 'Instrumental'),
    ('backingVocalsUrl', 'Backing Vocals'),
    ('drumsUrl', 'Drums'),
    ('bassUrl', 'Bass'),
    ('guitarUrl', 'Guitar'),
    ('keyboardUrl', 'Keyboard'),
    ('percussionUrl', 'Percussion'),
    ('stringsUrl', 'Strings'),
    ('synthUrl', 'Synth'),
    ('fxUrl', 'FX'),
    ('brassUrl', 'Brass'),
    ('woodwindsUrl', 'Woodwinds'),
)


# ── Lyrics & Timestamped Lyrics ──────────────────────────────────────────

async def get_lyrics_record_info(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/lyrics/record-info."""
    body, _, err = await _call_with_keys('GET', '/lyrics/record-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить статус текста.'
    return body.get('data') or {}, None


async def generate_lyrics(
    prompt: str,
    *,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/lyrics + poll GET /api/v1/lyrics/record-info."""
    if _is_cancelled(cancel_event):
        return None, 'Отменено.'
    payload = {
        'prompt': (prompt or '').strip()[:1200],
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    body, key, err = await _call_with_keys('POST', '/lyrics', payload=payload)
    if err or not body or not key:
        return None, err or 'Suno не ответил.'
    task_id = (body.get('data') or {}).get('taskId')
    if not task_id:
        return None, 'Suno не вернул taskId для текста.'

    waited = 0
    async with aiohttp.ClientSession() as session:
        while waited <= 180:
            if _is_cancelled(cancel_event):
                return None, 'Генерация текста отменена.'
            _, poll = await _get(session, '/lyrics/record-info', key, {'taskId': task_id})
            data = poll.get('data') or {}
            status = str(data.get('status') or '')
            if status == 'SUCCESS':
                items = (data.get('response') or {}).get('data') or []
                out = [
                    {
                        'title': str(i.get('title') or f'Вариант {idx}'),
                        'text': str(i.get('text') or '').strip(),
                        'status': str(i.get('status') or 'complete'),
                    }
                    for idx, i in enumerate(items, 1)
                    if (i.get('text') or '').strip()
                ]
                if out:
                    return out, None
                return None, 'Suno вернул пустой текст.'
            if status.endswith('_FAILED') or status in ('SENSITIVE_WORD_ERROR', 'CALLBACK_EXCEPTION'):
                reason = data.get('errorMessage') or status
                return None, f'Ошибка генерации текста: {reason}'
            if status_cb:
                try:
                    await status_cb(f'Пишу текст… ({waited}с)')
                except Exception:
                    pass
            if await _wait_or_cancel(cancel_event, 3):
                return None, 'Генерация текста отменена.'
            waited += 3
    return None, 'Таймаут генерации текста.'


async def get_timestamped_lyrics(task_id: str, audio_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/generate/get-timestamped-lyrics."""
    payload = {'taskId': task_id.strip(), 'audioId': audio_id.strip()}
    body, _, err = await _call_with_keys('POST', '/generate/get-timestamped-lyrics', payload=payload)
    if err or not body:
        return None, err or 'Не удалось получить таймкоды.'
    return body.get('data') or {}, None


def format_lrc(aligned_words: list[dict]) -> str:
    """Convert Suno alignedWords into standard .lrc lines."""
    if not aligned_words:
        return ''
    lines: list[str] = []
    cur_line: list[str] = []
    line_start: Optional[float] = None

    def _ts(sec: float) -> str:
        s = max(0.0, float(sec))
        m = int(s // 60)
        rem = s - m * 60
        return f'[{m:02d}:{rem:05.2f}]'

    for w in aligned_words:
        word = str(w.get('word') or '')
        start = w.get('startS')
        if start is None:
            start = w.get('start_s')
        if start is None:
            start = w.get('start', 0.0)
        if not word:
            continue
        parts = word.split('\n')
        for idx, part in enumerate(parts):
            if idx > 0 and cur_line:
                lines.append(f'{_ts(line_start or 0.0)} {"".join(cur_line).strip()}')
                cur_line = []
                line_start = None
            if part:
                if line_start is None:
                    try:
                        line_start = float(start)
                    except (TypeError, ValueError):
                        line_start = 0.0
                cur_line.append(part)
    if cur_line:
        lines.append(f'{_ts(line_start or 0.0)} {"".join(cur_line).strip()}')
    return '\n'.join(line for line in lines if line.strip())


# ── Style Boost ──────────────────────────────────────────────────────────

async def boost_music_style(content: str) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/style/generate — expand concise tags into rich style prompt."""
    clean = (content or '').strip()
    if not clean:
        return None, 'Укажи базовое описание стиля.'
    body, _, err = await _call_with_keys('POST', '/style/generate', payload={'content': clean[:1000]})
    if err or not body:
        return None, err or 'Не удалось усилить стиль.'
    data = body.get('data') or {}
    if data.get('successFlag') == 2:
        return None, f"Ошибка усиления стиля: {data.get('errorMessage') or 'failed'}"
    return data, None


# ── Music Cover Art ──────────────────────────────────────────────────────

async def get_music_cover_details(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/suno/cover/record-info."""
    body, _, err = await _call_with_keys('GET', '/suno/cover/record-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить статус обложки.'
    return body.get('data') or {}, None


async def generate_music_cover(
    task_id: str,
    *,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[bytes]], Optional[str]]:
    """POST /api/v1/suno/cover/generate + poll GET /api/v1/suno/cover/record-info."""
    if _is_cancelled(cancel_event):
        return None, 'Отменено.'
    payload = {'taskId': task_id.strip(), 'callBackUrl': _PLACEHOLDER_CALLBACK}
    body, key, err = await _call_with_keys('POST', '/suno/cover/generate', payload=payload, accept_codes=(200, 409))
    if err or not body or not key:
        return None, err or 'Не удалось запустить генерацию обложки.'
    cover_task_id = (body.get('data') or {}).get('taskId') or task_id.strip()

    waited = 0
    async with aiohttp.ClientSession() as session:
        while waited <= 180:
            if _is_cancelled(cancel_event):
                return None, 'Генерация обложки отменена.'
            _, poll = await _get(session, '/suno/cover/record-info', key, {'taskId': cover_task_id})
            data = poll.get('data') or {}
            flag = data.get('successFlag')
            resp_obj = data.get('response') or {}
            images = resp_obj.get('images') or data.get('images') or []
            if images or flag in (1, 'SUCCESS', 'COMPLETE'):
                urls = [u for u in images if isinstance(u, str) and u.startswith('http')]
                if not urls and isinstance(resp_obj, dict):
                    for k in ('imageUrl', 'image_url', 'coverUrl'):
                        if resp_obj.get(k):
                            urls.append(resp_obj[k])
                imgs: list[bytes] = []
                for u in urls[:4]:
                    raw = await _download_bytes(session, u, timeout=60)
                    if raw:
                        imgs.append(raw)
                if imgs:
                    return imgs, None
                if flag in (1, 'SUCCESS', 'COMPLETE'):
                    return None, 'Обложки готовы, но не удалось скачать изображения.'
            if flag in (2, 3, 'CREATE_TASK_FAILED', 'GENERATE_FAILED') or data.get('errorMessage'):
                return None, f"Ошибка генерации обложки: {data.get('errorMessage') or flag}"
            if status_cb:
                try:
                    await status_cb(f'Рисую обложки… ({waited}с)')
                except Exception:
                    pass
            if await _wait_or_cancel(cancel_event, 4):
                return None, 'Генерация обложки отменена.'
            waited += 4
    return None, 'Таймаут генерации обложки.'


# ── Audio Recovery ───────────────────────────────────────────────────────

async def get_recovery_audio_details(recovery_task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/suno/recovery/record-info?task_id=..."""
    body, _, err = await _call_with_keys(
        'GET', '/suno/recovery/record-info',
        params={'task_id': recovery_task_id.strip()},
        accept_codes=(200, 201),
    )
    if err or not body:
        return None, err or 'Не удалось проверить статус восстановления.'
    return body, None


async def recover_audio(
    suno_task_id: str,
    *,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/suno/recovery + poll GET /api/v1/suno/recovery/record-info."""
    if _is_cancelled(cancel_event):
        return None, 'Отменено.'
    payload = {'sunoTaskId': suno_task_id.strip(), 'callBackUrl': _PLACEHOLDER_CALLBACK}
    body, key, err = await _call_with_keys('POST', '/suno/recovery', payload=payload)
    if err or not body or not key:
        return None, err or 'Не удалось создать задачу восстановления.'
    rec_id = (body.get('data') or {}).get('task_id') or (body.get('data') or {}).get('taskId')
    if not rec_id:
        return None, 'Suno не вернул task_id восстановления.'

    waited = 0
    async with aiohttp.ClientSession() as session:
        while waited <= 180:
            if _is_cancelled(cancel_event):
                return None, 'Восстановление отменено.'
            _, poll = await _get(session, '/suno/recovery/record-info', key, {'task_id': rec_id})
            code = poll.get('code')
            if code == 200:
                tracks = poll.get('data') or []
                recovered = []
                for item in tracks:
                    url = item.get('audio_url') or ''
                    raw = await _download_bytes(session, url, timeout=120) if url else None
                    recovered.append({
                        'id': item.get('id') or '',
                        'status': item.get('status') or ('success' if raw else 'failed'),
                        'audio_url': url,
                        'audio': raw,
                        'error': item.get('error') or '',
                    })
                return recovered, None
            if code != 201:
                return None, f"Ошибка восстановления: {poll.get('msg') or f'код {code}'}"
            if status_cb:
                try:
                    await status_cb(f'Восстанавливаю ссылки… ({waited}с)')
                except Exception:
                    pass
            if await _wait_or_cancel(cancel_event, 4):
                return None, 'Восстановление отменено.'
            waited += 4
    return None, 'Таймаут восстановления ссылок.'


# ── WAV Conversion ───────────────────────────────────────────────────────

async def get_wav_conversion_details(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/wav/record-info."""
    body, _, err = await _call_with_keys('GET', '/wav/record-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить статус WAV.'
    return body.get('data') or {}, None


async def convert_to_wav(
    task_id: str,
    audio_id: str,
    *,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/wav/generate + poll GET /api/v1/wav/record-info."""
    if _is_cancelled(cancel_event):
        return None, 'Отменено.'
    payload = {
        'taskId': task_id.strip(),
        'audioId': audio_id.strip(),
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    body, key, err = await _call_with_keys('POST', '/wav/generate', payload=payload)
    if err or not body or not key:
        return None, err or 'Не удалось запустить конвертацию WAV.'
    wav_task_id = (body.get('data') or {}).get('taskId')
    if not wav_task_id:
        return None, 'Suno не вернул taskId для WAV.'

    waited = 0
    async with aiohttp.ClientSession() as session:
        while waited <= 300:
            if _is_cancelled(cancel_event):
                return None, 'Конвертация WAV отменена.'
            _, poll = await _get(session, '/wav/record-info', key, {'taskId': wav_task_id})
            data = poll.get('data') or {}
            flag = str(data.get('successFlag') or '')
            wav_url = (data.get('response') or {}).get('audioWavUrl') or ''
            if flag == 'SUCCESS' and wav_url:
                raw = await _download_bytes(session, wav_url, timeout=180)
                return {'task_id': wav_task_id, 'url': wav_url, 'audio': raw}, None
            if flag.endswith('_FAILED') or flag == 'CALLBACK_EXCEPTION':
                return None, f"Ошибка WAV: {data.get('errorMessage') or flag}"
            if status_cb:
                try:
                    await status_cb(f'Конвертирую в WAV… ({waited}с)')
                except Exception:
                    pass
            if await _wait_or_cancel(cancel_event, 4):
                return None, 'Конвертация WAV отменена.'
            waited += 4
    return None, 'Таймаут конвертации WAV.'


# ── Vocal & Instrument Stem Separation ───────────────────────────────────

async def get_vocal_separation_details(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/vocal-removal/record-info."""
    body, _, err = await _call_with_keys('GET', '/vocal-removal/record-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить статус разделения.'
    return body.get('data') or {}, None


async def separate_vocals(
    sep_type: str = 'separate_vocal',
    *,
    task_id: str = '',
    audio_id: str = '',
    audio_url: str = '',
    stem_name: str = '',
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/vocal-removal/generate + poll record-info."""
    if _is_cancelled(cancel_event):
        return None, 'Отменено.'
    payload: dict[str, Any] = {
        'type': sep_type,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    if audio_url:
        payload['audioUrl'] = audio_url.strip()
    elif task_id and audio_id:
        payload['taskId'] = task_id.strip()
        payload['audioId'] = audio_id.strip()
    else:
        return None, 'Нужен либо audioUrl, либо taskId + audioId.'

    if sep_type == 'split_stem_advanced':
        if stem_name not in SUNO_STEM_NAMES:
            return None, f'Неизвестный инструмент: {stem_name}'
        payload['stemName'] = stem_name

    body, key, err = await _call_with_keys('POST', '/vocal-removal/generate', payload=payload)
    if err or not body or not key:
        return None, err or 'Не удалось запустить разделение.'
    sep_task_id = (body.get('data') or {}).get('taskId')
    if not sep_task_id:
        return None, 'Suno не вернул taskId для разделения.'

    waited = 0
    async with aiohttp.ClientSession() as session:
        while waited <= SUNO_POLL_TIMEOUT:
            if _is_cancelled(cancel_event):
                return None, 'Разделение отменено.'
            _, poll = await _get(session, '/vocal-removal/record-info', key, {'taskId': sep_task_id})
            data = poll.get('data') or {}
            flag = str(data.get('successFlag') or '')
            if flag == 'SUCCESS':
                resp = data.get('response') or {}
                origin_data = resp.get('originData') or []
                stems: list[dict] = []
                if origin_data:
                    for item in origin_data:
                        url = item.get('audio_url') or ''
                        if not url:
                            continue
                        raw = await _download_bytes(session, url, timeout=120)
                        stems.append({
                            'id': str(item.get('id') or ''),
                            'name': str(item.get('stem_type_group_name') or 'Stem'),
                            'url': url,
                            'duration': float(item.get('duration') or 0),
                            'audio': raw,
                        })
                else:
                    for field, label in _STEM_URL_FIELDS:
                        url = resp.get(field) or ''
                        if not url:
                            continue
                        raw = await _download_bytes(session, url, timeout=120)
                        stems.append({
                            'id': '',
                            'name': label,
                            'url': url,
                            'duration': 0.0,
                            'audio': raw,
                        })
                if not stems:
                    return None, 'Разделение завершилось, но стемы не найдены.'
                return {'task_id': sep_task_id, 'sep_type': sep_type, 'stems': stems}, None
            if flag.endswith('_FAILED') or flag == 'CALLBACK_EXCEPTION':
                return None, f"Ошибка разделения: {data.get('errorMessage') or flag}"
            if status_cb:
                try:
                    await status_cb(f'Разделяю дорожки… ({waited}с)')
                except Exception:
                    pass
            if await _wait_or_cancel(cancel_event, SUNO_POLL_INTERVAL):
                return None, 'Разделение отменено.'
            waited += SUNO_POLL_INTERVAL
    return None, 'Таймаут разделения стемов.'


# ── MIDI Generation & Pure-Python SMF Encoder ────────────────────────────

async def get_midi_details(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/midi/record-info."""
    body, _, err = await _call_with_keys('GET', '/midi/record-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить статус MIDI.'
    return body.get('data') or {}, None


async def generate_midi(
    sep_task_id: str,
    *,
    audio_id: str = '',
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/midi/generate + poll GET /api/v1/midi/record-info."""
    if _is_cancelled(cancel_event):
        return None, 'Отменено.'
    payload: dict[str, Any] = {
        'taskId': sep_task_id.strip(),
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    if audio_id:
        payload['audioId'] = audio_id.strip()

    body, key, err = await _call_with_keys('POST', '/midi/generate', payload=payload)
    if err or not body or not key:
        return None, err or 'Не удалось запустить генерацию MIDI.'
    midi_task_id = (body.get('data') or {}).get('taskId')
    if not midi_task_id:
        return None, 'Suno не вернул taskId для MIDI.'

    waited = 0
    async with aiohttp.ClientSession() as session:
        while waited <= 300:
            if _is_cancelled(cancel_event):
                return None, 'Генерация MIDI отменена.'
            _, poll = await _get(session, '/midi/record-info', key, {'taskId': midi_task_id})
            data = poll.get('data') or {}
            flag = data.get('successFlag')
            if flag == 1 or str(flag).upper() == 'SUCCESS':
                midi_data = data.get('midiData') or {}
                midi_bytes = build_midi_bytes(midi_data)
                return {
                    'task_id': midi_task_id,
                    'midi_data': midi_data,
                    'midi_bytes': midi_bytes,
                }, None
            if flag in (2, 3) or str(flag).endswith('_FAILED'):
                return None, f"Ошибка MIDI: {data.get('errorMessage') or f'flag={flag}'}"
            if status_cb:
                try:
                    await status_cb(f'Извлекаю MIDI-ноты… ({waited}с)')
                except Exception:
                    pass
            if await _wait_or_cancel(cancel_event, 4):
                return None, 'Генерация MIDI отменена.'
            waited += 4
    return None, 'Таймаут генерации MIDI.'


def _vlq(val: int) -> bytes:
    """Encode non-negative integer as MIDI Variable-Length Quantity."""
    val = max(0, int(val))
    out = [val & 0x7F]
    val >>= 7
    while val:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    return bytes(reversed(out))


def build_midi_bytes(midi_data: dict, ppqn: int = 480, bpm: int = 120) -> bytes:
    """Encode Suno midiData ({instruments: [{name, notes: [{pitch, start, end, velocity}]}]})
    into a playable Standard MIDI File (Format 1).
    """
    instruments = (midi_data or {}).get('instruments') or []
    tempo_us = int(60_000_000 / max(20, bpm))
    ticks_per_sec = ppqn * (bpm / 60.0)

    tracks_raw: list[bytes] = []
    # Track 0: tempo meta track
    t0 = bytearray()
    t0 += b'\x00\xff\x51\x03' + tempo_us.to_bytes(3, 'big')
    t0 += b'\x00\xff\x2f\x00'
    tracks_raw.append(bytes(t0))

    for idx, inst in enumerate(instruments):
        name = str(inst.get('name') or f'Track {idx + 1}')
        is_drums = 'drum' in name.lower() or 'perc' in name.lower()
        channel = 9 if is_drums else (idx % 15 if idx % 15 < 9 else (idx % 15) + 1)
        events: list[tuple[int, int, bytes]] = []
        for note in inst.get('notes') or []:
            try:
                pitch = max(0, min(127, int(note.get('pitch', 60))))
                start_s = max(0.0, float(note.get('start', 0.0)))
                end_s = max(start_s + 0.02, float(note.get('end', start_s + 0.1)))
                vel_f = float(note.get('velocity', 0.8))
                vel = max(1, min(127, int(round(vel_f * 127)) if vel_f <= 1.0 else int(vel_f)))
            except (TypeError, ValueError):
                continue
            on_tick = int(round(start_s * ticks_per_sec))
            off_tick = max(on_tick + 1, int(round(end_s * ticks_per_sec)))
            events.append((on_tick, 1, bytes([0x90 | channel, pitch, vel])))
            events.append((off_tick, 0, bytes([0x80 | channel, pitch, 0])))

        events.sort(key=lambda e: (e[0], e[1]))
        buf = bytearray()
        name_bytes = name.encode('utf-8')[:64]
        buf += b'\x00\xff\x03' + _vlq(len(name_bytes)) + name_bytes
        last_tick = 0
        for tick, _, msg in events:
            buf += _vlq(tick - last_tick) + msg
            last_tick = tick
        buf += b'\x00\xff\x2f\x00'
        tracks_raw.append(bytes(buf))

    header = b'MThd' + struct.pack('>IHHH', 6, 1, len(tracks_raw), ppqn)
    body = b''.join(b'MTrk' + struct.pack('>I', len(t)) + t for t in tracks_raw)
    return header + body


# ── Music Video (MP4) ────────────────────────────────────────────────────

async def get_music_video_details(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/mp4/record-info."""
    body, _, err = await _call_with_keys('GET', '/mp4/record-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить статус видео.'
    return body.get('data') or {}, None


async def create_music_video(
    task_id: str,
    audio_id: str,
    *,
    author: str = '',
    domain_name: str = '',
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/mp4/generate + poll GET /api/v1/mp4/record-info."""
    if _is_cancelled(cancel_event):
        return None, 'Отменено.'
    payload: dict[str, Any] = {
        'taskId': task_id.strip(),
        'audioId': audio_id.strip(),
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    if author:
        payload['author'] = author.strip()[:50]
    if domain_name:
        payload['domainName'] = domain_name.strip()[:50]

    body, key, err = await _call_with_keys('POST', '/mp4/generate', payload=payload)
    if err or not body or not key:
        return None, err or 'Не удалось запустить создание видео.'
    mp4_task_id = (body.get('data') or {}).get('taskId')
    if not mp4_task_id:
        return None, 'Suno не вернул taskId для видео.'

    waited = 0
    async with aiohttp.ClientSession() as session:
        while waited <= SUNO_POLL_TIMEOUT:
            if _is_cancelled(cancel_event):
                return None, 'Создание видео отменено.'
            _, poll = await _get(session, '/mp4/record-info', key, {'taskId': mp4_task_id})
            data = poll.get('data') or {}
            flag = str(data.get('successFlag') or '')
            video_url = (data.get('response') or {}).get('videoUrl') or ''
            if flag == 'SUCCESS' and video_url:
                raw = await _download_bytes(session, video_url, timeout=180)
                return {'task_id': mp4_task_id, 'url': video_url, 'video': raw}, None
            if flag.endswith('_FAILED') or flag == 'CALLBACK_EXCEPTION':
                return None, f"Ошибка создания клипа: {data.get('errorMessage') or flag}"
            if status_cb:
                try:
                    await status_cb(f'Рендерю клип MP4… ({waited}с)')
                except Exception:
                    pass
            if await _wait_or_cancel(cancel_event, SUNO_POLL_INTERVAL):
                return None, 'Создание видео отменено.'
            waited += SUNO_POLL_INTERVAL
    return None, 'Таймаут создания MP4.'


# ── Suno Voice (Custom Singing Voice — 6 Endpoints) ──────────────────────

async def voice_validate_info(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/voice/validate-info."""
    body, _, err = await _call_with_keys('GET', '/voice/validate-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить проверочную фразу.'
    return body.get('data') or {}, None


async def _poll_voice_validate(
    session: aiohttp.ClientSession,
    task_id: str,
    key: str,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[dict], Optional[str]]:
    waited = 0
    while waited <= 180:
        if _is_cancelled(cancel_event):
            return None, 'Отменено.'
        _, poll = await _get(session, '/voice/validate-info', key, {'taskId': task_id})
        data = poll.get('data') or {}
        st = str(data.get('status') or '')
        if st in ('wait_validating', 'success') and data.get('validateInfo'):
            return data, None
        if st in ('processing_validate_fail', 'fail'):
            return None, f"Ошибка валидации: {data.get('errorMessage') or st}"
        if status_cb:
            try:
                await status_cb(f'Готовлю проверочную фразу… ({waited}с)')
            except Exception:
                pass
        if await _wait_or_cancel(cancel_event, 3):
            return None, 'Отменено.'
        waited += 3
    return None, 'Таймаут подготовки проверочной фразы.'


async def voice_validate(
    voice_url: str,
    vocal_start_s: int,
    vocal_end_s: int,
    *,
    language: str = 'ru',
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/voice/validate + poll GET /api/v1/voice/validate-info."""
    if int(vocal_end_s) <= int(vocal_start_s):
        return None, 'Конец фрагмента должен быть больше начала.'
    payload = {
        'voiceUrl': voice_url.strip(),
        'vocalStartS': max(0, int(vocal_start_s)),
        'vocalEndS': int(vocal_end_s),
        'language': language,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    body, key, err = await _call_with_keys('POST', '/voice/validate', payload=payload)
    if err or not body or not key:
        return None, err or 'Не удалось начать создание голоса.'
    task_id = (body.get('data') or {}).get('taskId')
    if not task_id:
        return None, 'Suno не вернул taskId валидации.'
    async with aiohttp.ClientSession() as session:
        return await _poll_voice_validate(session, task_id, key, status_cb, cancel_event)


async def voice_regenerate(
    task_id: str,
    *,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/voice/regenerate (uses calBackUrl per API schema) + poll validate-info."""
    payload = {
        'taskId': task_id.strip(),
        'calBackUrl': _PLACEHOLDER_CALLBACK,
    }
    body, key, err = await _call_with_keys('POST', '/voice/regenerate', payload=payload)
    if err or not body or not key:
        return None, err or 'Не удалось обновить фразу.'
    new_tid = (body.get('data') or {}).get('taskId') or task_id.strip()
    async with aiohttp.ClientSession() as session:
        return await _poll_voice_validate(session, new_tid, key, status_cb, cancel_event)


async def voice_record_info(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/voice/record-info."""
    body, _, err = await _call_with_keys('GET', '/voice/record-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить данные голоса.'
    return body.get('data') or {}, None


async def voice_generate(
    task_id: str,
    verify_url: str,
    *,
    voice_name: str = '',
    description: str = '',
    style: str = '',
    singer_skill_level: str = 'beginner',
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/voice/generate + poll GET /api/v1/voice/record-info."""
    payload: dict[str, Any] = {
        'taskId': task_id.strip(),
        'verifyUrl': verify_url.strip(),
        'singerSkillLevel': singer_skill_level,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    if voice_name:
        payload['voiceName'] = voice_name[:80]
    if description:
        payload['description'] = description[:300]
    if style:
        payload['style'] = style[:200]

    body, key, err = await _call_with_keys('POST', '/voice/generate', payload=payload)
    if err or not body or not key:
        return None, err or 'Не удалось запустить создание голоса.'
    gen_tid = (body.get('data') or {}).get('taskId') or task_id.strip()

    waited = 0
    async with aiohttp.ClientSession() as session:
        while waited <= SUNO_POLL_TIMEOUT:
            if _is_cancelled(cancel_event):
                return None, 'Отменено.'
            _, poll = await _get(session, '/voice/record-info', key, {'taskId': gen_tid})
            data = poll.get('data') or {}
            st = str(data.get('status') or '')
            if st == 'success' and data.get('voiceId'):
                return data, None
            if st in ('processing_validate_fail', 'fail'):
                return None, f"Ошибка создания голоса: {data.get('errorMessage') or st}"
            if status_cb:
                try:
                    await status_cb(f'Обучаю голос… ({waited}с)')
                except Exception:
                    pass
            if await _wait_or_cancel(cancel_event, SUNO_POLL_INTERVAL):
                return None, 'Отменено.'
            waited += SUNO_POLL_INTERVAL
    return None, 'Таймаут создания голоса.'


async def voice_check_availability(task_id: str) -> Tuple[Optional[bool], Optional[str]]:
    """POST /api/v1/voice/check-voice (snake_case task_id per API schema)."""
    body, _, err = await _call_with_keys('POST', '/voice/check-voice', payload={'task_id': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось проверить доступность голоса.'
    return bool((body.get('data') or {}).get('isAvailable')), None
