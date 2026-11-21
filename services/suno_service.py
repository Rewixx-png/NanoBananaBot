"""Suno music & audio generation client for sunoapi.org (all 38 endpoints).

Core generation, editing, sounds, speech, file upload, and key-rotation helpers
live here; post-processing, lyrics, covers, recovery, stems, MIDI, MP4, and
Suno Voice live in ``services.suno_extra`` and are re-exported at the bottom.
"""

import asyncio
import base64
import logging
from typing import Any, Optional, Tuple

import aiohttp

from config import SUNO_BASE_URL, SUNO_PLACEHOLDER_CALLBACK, SUNO_UPLOAD_BASE_URL
from keys import load_suno_keys, remove_key

logger = logging.getLogger(__name__)

# Cloudflare rejects requests with empty/default aiohttp User-Agent.
_USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
)
# Schema requires callBackUrl even when polling record-info endpoints.
_PLACEHOLDER_CALLBACK = SUNO_PLACEHOLDER_CALLBACK

SUNO_POLL_INTERVAL = 5
SUNO_POLL_TIMEOUT = 420
SUNO_TIMEOUT = 60

SUNO_MODELS = {
    'suno-v6': {'id': 'V6', 'label': 'Suno V6', 'desc': 'Лучшее качество, живые вокалы'},
    'suno-v6-wild': {'id': 'V6_WILD', 'label': 'Suno V6 Wild', 'desc': 'Смелые эксперименты'},
    'suno-v6-mini': {'id': 'V6_MINI', 'label': 'Suno V6 Mini', 'desc': 'Быстро и дёшево'},
}

SUNO_ALL_MODELS = {
    **SUNO_MODELS,
    'suno-v5-5': {'id': 'V5_5', 'label': 'Suno V5.5', 'desc': 'Legacy · кастомный голос'},
    'suno-v5': {'id': 'V5', 'label': 'Suno V5', 'desc': 'Legacy · быстрая'},
    'suno-v4-5plus': {'id': 'V4_5PLUS', 'label': 'Suno V4.5+', 'desc': 'Legacy · богатый тембр'},
    'suno-v4-5all': {'id': 'V4_5ALL', 'label': 'Suno V4.5 All', 'desc': 'Legacy · структура'},
    'suno-v4-5': {'id': 'V4_5', 'label': 'Suno V4.5', 'desc': 'Legacy · до 8 мин'},
    'suno-v4': {'id': 'V4', 'label': 'Suno V4', 'desc': 'Legacy · до 4 мин'},
}

_MODEL_ID_SET = {v['id'] for v in SUNO_ALL_MODELS.values()}

_CODE_MESSAGES = {
    400: 'Некорректные параметры запроса',
    401: 'Ключ Suno недействителен',
    402: 'На ключе недостаточно кредитов',
    404: 'Ресурс или метод не найден',
    405: 'Превышен лимит запросов',
    409: 'Конфликт (задача уже существует)',
    413: 'Промпт или текст слишком длинный',
    422: 'Ошибка валидации параметров',
    429: 'На ключе кончились кредиты',
    430: 'Слишком частые запросы, попробуй позже',
    451: 'Не удалось скачать исходный файл',
    455: 'Техобслуживание на стороне Suno',
    500: 'Ошибка сервера Suno',
}

def _code_msg(code: Any, default: str) -> str:
    return _CODE_MESSAGES.get(code, default) if isinstance(code, int) else default
_KEY_ERRORS = {401, 402, 429, 430}


def resolve_model_id(model_key_or_id: str) -> Optional[str]:
    if model_key_or_id in SUNO_ALL_MODELS:
        return SUNO_ALL_MODELS[model_key_or_id]['id']
    if model_key_or_id in _MODEL_ID_SET:
        return model_key_or_id
    return None


def _is_cancelled(cancel_event) -> bool:
    return cancel_event is not None and cancel_event.is_set()


async def _wait_or_cancel(cancel_event, seconds: float) -> bool:
    """Sleep for seconds, returning True as soon as cancellation is requested."""
    if cancel_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    return True


async def _post(
    session: aiohttp.ClientSession,
    path: str,
    key: str,
    payload: dict,
    *,
    base_url: str = SUNO_BASE_URL,
    timeout: int = SUNO_TIMEOUT,
) -> tuple[int, dict]:
    headers = {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'User-Agent': _USER_AGENT,
    }
    async with session.post(
        base_url + path,
        json=payload,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        try:
            return resp.status, await resp.json()
        except Exception:
            return resp.status, {'msg': (await resp.text())[:300]}


async def _get(
    session: aiohttp.ClientSession,
    path: str,
    key: str,
    params: Optional[dict] = None,
    *,
    base_url: str = SUNO_BASE_URL,
    timeout: int = SUNO_TIMEOUT,
) -> tuple[int, dict]:
    headers = {
        'Authorization': f'Bearer {key}',
        'User-Agent': _USER_AGENT,
    }
    async with session.get(
        base_url + path,
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        try:
            return resp.status, await resp.json()
        except Exception:
            return resp.status, {'msg': (await resp.text())[:300]}


async def _get_detail(session: aiohttp.ClientSession, task_id: str, key: str) -> dict:
    _, body = await _get(session, '/generate/record-info', key, {'taskId': task_id})
    return body


async def _call_with_keys(
    method: str,
    path: str,
    *,
    payload: Optional[dict] = None,
    params: Optional[dict] = None,
    base_url: str = SUNO_BASE_URL,
    timeout: int = SUNO_TIMEOUT,
    accept_codes: tuple[int, ...] = (200,),
) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    """Call a Suno API endpoint rotating across live KeyHunter keys.

    Returns ``(body, used_key, error_message)``.
    """
    keys = await load_suno_keys()
    if not keys:
        return None, None, (
            'Нет живых ключей Suno в KeyHunter. Проверь, что keyhunter '
            'валидирует сервис Suno и помечает ключи как live.'
        )

    key_errors: list[str] = []
    last_error = 'Suno не ответил.'

    async with aiohttp.ClientSession() as session:
        for key in keys:
            try:
                if method.upper() == 'GET':
                    _, body = await _get(session, path, key, params, base_url=base_url, timeout=timeout)
                else:
                    _, body = await _post(session, path, key, payload or {}, base_url=base_url, timeout=timeout)
            except Exception as e:
                last_error = f'Suno: {type(e).__name__}: {e}'
                logger.warning(last_error)
                continue

            code = body.get('code')
            if code in accept_codes or (code is None and body.get('success') is True):
                return body, key, None

            message = body.get('msg') or _code_msg(code, f'код {code}')
            described = f'{_code_msg(code, "Ошибка Suno")}: {message}'
            key_errors.append(f'{key[:8]}…: {described}')
            logger.warning(f'Suno {path} -> {code} on key {key[:8]}…: {message}')
            if code == 401:
                await remove_key(key)
            elif code in (430, 405):
                await remove_key(key, 429)
            last_error = described
            if code not in _KEY_ERRORS:
                break

    detail = '\n\nПопытки:\n' + '\n'.join(key_errors[:5]) if key_errors else ''
    return None, None, f'Suno: {last_error}{detail}'


async def _download_bytes(session: aiohttp.ClientSession, url: str, timeout: int = 120) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with session.get(
            url,
            headers={'User-Agent': _USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 200:
                return await resp.read()
            logger.warning(f'Suno download HTTP {resp.status}: {url[:90]}')
    except Exception as e:
        logger.warning(f'Suno download failed ({url[:90]}): {type(e).__name__}: {e}')
    return None


# ── File Upload API (https://sunoapiorg.redpandaai.co) ───────────────────

async def upload_file_stream(
    data: bytes,
    filename: str,
    upload_path: str = 'suno/uploads',
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/file-stream-upload — multipart upload (TTL 3 days)."""
    keys = await load_suno_keys()
    if not keys:
        return None, 'Нет живых ключей Suno для загрузки файла.'

    last_err = 'Не удалось загрузить файл.'
    async with aiohttp.ClientSession() as session:
        for key in keys:
            form = aiohttp.FormData()
            form.add_field('file', data, filename=filename)
            form.add_field('uploadPath', upload_path.strip('/'))
            form.add_field('fileName', filename)
            headers = {'Authorization': f'Bearer {key}', 'User-Agent': _USER_AGENT}
            try:
                async with session.post(
                    f'{SUNO_UPLOAD_BASE_URL}/api/file-stream-upload',
                    data=form,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    body: dict[str, Any]
                    try:
                        body = await resp.json()
                    except Exception:
                        body = {'msg': (await resp.text())[:200]}
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
                continue

            if body.get('code') == 200 or body.get('success') is True:
                info = body.get('data')
                if isinstance(info, dict) and info.get('downloadUrl'):
                    return info, None
            code = body.get('code')
            last_err = str(body.get('msg') or _code_msg(code, f'код {code}'))
            if code == 401:
                await remove_key(key)
            elif code not in _KEY_ERRORS:
                break
    return None, f'Ошибка загрузки файла: {last_err}'


async def upload_file_base64(
    data: bytes | str,
    filename: str,
    upload_path: str = 'suno/uploads',
    mime_type: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/file-base64-upload — base64/data-URL upload (TTL 3 days)."""
    if isinstance(data, bytes):
        b64 = base64.b64encode(data).decode('ascii')
        if mime_type:
            b64 = f'data:{mime_type};base64,{b64}'
    else:
        b64 = data
    payload: dict[str, Any] = {
        'base64Data': b64,
        'uploadPath': upload_path.strip('/'),
    }
    if filename:
        payload['fileName'] = filename
    body, _, err = await _call_with_keys(
        'POST', '/api/file-base64-upload',
        payload=payload, base_url=SUNO_UPLOAD_BASE_URL, timeout=120,
    )
    if err or not body:
        return None, err or 'Не удалось загрузить base64.'
    info = body.get('data') or {}
    if not info.get('downloadUrl'):
        return None, 'Сервер загрузки не вернул downloadUrl.'
    return info, None


async def upload_file_url(
    file_url: str,
    upload_path: str = 'suno/uploads',
    file_name: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/file-url-upload — import remote HTTP(S) file (TTL 3 days)."""
    payload: dict[str, Any] = {
        'fileUrl': file_url.strip(),
        'uploadPath': upload_path.strip('/'),
    }
    if file_name:
        payload['fileName'] = file_name
    body, _, err = await _call_with_keys(
        'POST', '/api/file-url-upload',
        payload=payload, base_url=SUNO_UPLOAD_BASE_URL, timeout=120,
    )
    if err or not body:
        return None, err or 'Не удалось загрузить файл по URL.'
    info = body.get('data') or {}
    if not info.get('downloadUrl'):
        return None, 'Сервер загрузки не вернул downloadUrl.'
    return info, None


# ── Shared track polling & download ──────────────────────────────────────

async def _download_tracks(
    session: aiohttp.ClientSession,
    tracks: list[dict],
    task_id: str = '',
    cancel_event=None,
) -> list[dict]:
    result = []
    for track in tracks:
        if _is_cancelled(cancel_event):
            break
        audio_url = track.get('audio_url') or track.get('audioUrl') or track.get('source_audio_url')
        if not audio_url:
            continue
        audio = await _download_bytes(session, audio_url, timeout=120)
        if not audio:
            continue
        if _is_cancelled(cancel_event):
            break

        cover_url = track.get('image_url') or track.get('imageUrl') or track.get('source_image_url')
        cover = await _download_bytes(session, cover_url, timeout=60) if cover_url else None

        result.append({
            'id': track.get('id') or '',
            'task_id': task_id,
            'audio': audio,
            'audio_url': audio_url,
            'cover': cover,
            'cover_url': cover_url or '',
            'title': track.get('title') or 'Suno',
            'tags': track.get('tags') or '',
            'duration': track.get('duration') or 0,
            'prompt': track.get('prompt') or '',
            'model_name': track.get('model_name') or '',
        })
    return result


async def _await_tracks(
    session: aiohttp.ClientSession,
    task_id: str,
    key: str,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list], Optional[str]]:
    """Poll /api/v1/generate/record-info until tracks are ready and download them."""
    waited = 0
    while waited <= SUNO_POLL_TIMEOUT:
        if _is_cancelled(cancel_event):
            logger.info(f'Suno task cancelled by user: {task_id} (waited={waited}s)')
            return None, 'Suno: генерация отменена.'
        try:
            body = await _get_detail(session, task_id, key)
        except Exception as e:
            return None, f'Suno: не смог опросить задачу ({type(e).__name__}: {e})'

        data = body.get('data') or {}
        response = data.get('response') or {}
        tracks = response.get('sunoData') or response.get('data') or []
        status = str(data.get('status') or response.get('status') or '')
        ready = [t for t in tracks if t.get('audio_url') or t.get('audioUrl')]

        # Wait for all tracks when status is FIRST_SUCCESS if total tracks > ready,
        # unless status is already SUCCESS or empty (legacy behavior).
        all_ready = bool(ready) and (
            status in ('SUCCESS', '') or (len(tracks) > 0 and len(ready) >= len(tracks))
        )
        if all_ready:
            downloaded = await _download_tracks(session, ready, task_id=task_id, cancel_event=cancel_event)
            if downloaded:
                return downloaded, None
            if _is_cancelled(cancel_event):
                logger.info(f'Suno task cancelled during download: {task_id}')
                return None, 'Suno: генерация отменена.'
            return None, 'Suno: не удалось скачать готовые треки.'

        error = response.get('errorMessage') or data.get('errorMessage')
        if error or status.endswith('_FAILED') or status in ('SENSITIVE_WORD_ERROR', 'CALLBACK_EXCEPTION'):
            reason = error or _CODE_MESSAGES.get(0, status) or status
            if status == 'SENSITIVE_WORD_ERROR':
                reason = 'текст или стиль содержат запрещённые слова'
            return None, f'Suno не смог сгенерировать: {reason}'

        if waited % 30 == 0:
            logger.info(
                f'Suno task {task_id}: status={status or "PENDING"}, '
                f'ready={len(ready)}/{len(tracks)}, waited={waited}s'
            )
        if status_cb:
            try:
                await status_cb(f'Suno генерирует… ({waited}с)')
            except Exception:
                pass
        if await _wait_or_cancel(cancel_event, SUNO_POLL_INTERVAL):
            logger.info(f'Suno task cancelled by user: {task_id} (waited={waited}s)')
            return None, 'Suno: генерация отменена.'
        waited += SUNO_POLL_INTERVAL

    logger.warning(f'Suno task timed out: {task_id} after {SUNO_POLL_TIMEOUT}s')
    return None, f'Suno: таймаут {SUNO_POLL_TIMEOUT}с — задача {task_id} не завершилась.'


async def _submit_and_await_music(
    path: str,
    payload: dict,
    *,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    if _is_cancelled(cancel_event):
        return None, 'Suno: генерация отменена.'
    body, key, err = await _call_with_keys('POST', path, payload=payload)
    if err or not body or not key:
        return None, err or 'Suno не ответил.'
    task_id = (body.get('data') or {}).get('taskId')
    if not task_id:
        return None, 'Suno не вернул taskId.'
    logger.info(f'Suno {path} task created: {task_id}')
    async with aiohttp.ClientSession() as session:
        return await _await_tracks(session, task_id, key, status_cb, cancel_event)


# ── Payload builder & Music Generation (POST /api/v1/generate) ───────────

def _apply_common_controls(payload: dict, cfg: dict, *, allow_audio_weight: bool = True, allow_duration: bool = True, model_id: str = 'V6') -> None:
    if cfg.get('vocal') and not payload.get('instrumental'):
        payload['vocalGender'] = str(cfg['vocal'])
    if 'style_weight' in cfg and cfg['style_weight'] is not None:
        payload['styleWeight'] = round(float(cfg['style_weight']), 2)
    if 'weirdness' in cfg and cfg['weirdness'] is not None:
        payload['weirdnessConstraint'] = round(float(cfg['weirdness']), 2)
    if allow_audio_weight and not payload.get('instrumental') and 'audio_weight' in cfg and cfg['audio_weight'] is not None:
        payload['audioWeight'] = round(float(cfg['audio_weight']), 2)
    if 'variety' in cfg and cfg['variety'] is not None:
        payload['variety'] = max(0, min(4, int(cfg['variety'])))
    if cfg.get('persona_id'):
        payload['personaId'] = str(cfg['persona_id'])
        payload['personaModel'] = str(cfg.get('persona_model') or 'style_persona')
    if allow_duration and cfg.get('duration') is not None and model_id in ('V5_5', 'V6', 'V6_MINI', 'V6_WILD'):
        payload['duration'] = max(10, min(360, int(cfg['duration'])))


def build_payload(prompt: str, model_id: str, cfg: dict) -> dict:
    """Map /music settings onto POST /api/v1/generate body (custom & non-custom)."""
    custom_mode = str(cfg.get('custom_mode', '1')) != '0'
    instrumental = str(cfg.get('instrumental', '0')) == '1'
    idea = (prompt or '').strip()
    style = (cfg.get('style') or '').strip()
    lyrics = (cfg.get('lyrics') or '').strip()

    if not custom_mode:
        # Non-custom mode: supports multimodal references, forbids custom-only fields.
        payload: dict[str, Any] = {
            'customMode': False,
            'instrumental': instrumental,
            'model': model_id,
            'callBackUrl': _PLACEHOLDER_CALLBACK,
        }
        if idea:
            payload['prompt'] = idea[:3000]
        if style:
            payload['style'] = style[:1000]
        if lyrics:
            payload['lyrics'] = lyrics[:5000]
        if cfg.get('image_urls'):
            payload['imageUrls'] = list(cfg['image_urls'])[:5]
        if cfg.get('video_urls'):
            payload['videoUrls'] = list(cfg['video_urls'])[:1]
        if cfg.get('audio_urls'):
            payload['audioUrls'] = list(cfg['audio_urls'])
        if cfg.get('midi_urls'):
            payload['midiUrls'] = list(cfg['midi_urls'])[:1]
        if cfg.get('persona_id'):
            payload['personaId'] = str(cfg['persona_id'])
            payload['personaModel'] = str(cfg.get('persona_model') or 'style_persona')
        # Non-custom mode requires at least one of style/lyrics/imageUrls/videoUrls/audioUrls/midiUrls
        if not any(payload.get(k) for k in ('style', 'lyrics', 'imageUrls', 'videoUrls', 'audioUrls', 'midiUrls')):
            payload['style'] = (idea or 'modern music')[:1000]
        return payload

    if idea and style and idea.casefold() != style.casefold():
        style_text = f'{idea[:700]}, {style[:300]}'[:1000]
    else:
        style_text = (idea or style)[:1000]

    payload = {
        'customMode': True,
        'instrumental': instrumental,
        'model': model_id,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
        'duration': int(cfg.get('duration', 60)),
        'styleWeight': float(cfg.get('style_weight', 0.65)),
        'weirdnessConstraint': float(cfg.get('weirdness', 0.25)),
        'variety': int(cfg.get('variety', 1)),
    }
    if style_text:
        payload['style'] = style_text
    if lyrics and not instrumental:
        payload['lyrics'] = lyrics[:5000]
    if cfg.get('title'):
        payload['title'] = str(cfg['title'])[:80]
    if cfg.get('neg'):
        payload['negativeTags'] = str(cfg['neg'])[:1000]
    if cfg.get('vocal') and not instrumental:
        payload['vocalGender'] = str(cfg['vocal'])
    if not instrumental and 'audio_weight' in cfg:
        payload['audioWeight'] = float(cfg['audio_weight'])
    if cfg.get('persona_id'):
        payload['personaId'] = str(cfg['persona_id'])
        payload['personaModel'] = str(cfg.get('persona_model') or 'style_persona')
    if not payload.get('style') and not payload.get('lyrics') and not payload.get('negativeTags'):
        payload['style'] = 'instrumental music'
    return payload


async def generate_suno(
    prompt: str,
    model_key: str = 'suno-v6',
    cfg: Optional[dict] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate — generate music tracks."""
    model_id = resolve_model_id(model_key)
    if not model_id:
        return None, f'Неизвестная модель Suno: {model_key}'
    if _is_cancelled(cancel_event):
        return None, 'Suno: генерация отменена.'
    cfg = cfg or {}

    keys = await load_suno_keys()
    if not keys:
        return None, (
            'Нет живых ключей Suno в KeyHunter. Проверь, что keyhunter '
            'валидирует сервис Suno и помечает ключи как live.'
        )

    payload = build_payload(prompt, model_id, cfg)
    key_errors: list[str] = []
    last_error = 'Suno не ответил.'

    async with aiohttp.ClientSession() as session:
        for key in keys:
            try:
                _, body = await _post(session, '/generate', key, payload)
            except Exception as e:
                last_error = f'Suno: {type(e).__name__}: {e}'
                logger.warning(last_error)
                continue

            code = body.get('code')
            if code == 200:
                task_id = (body.get('data') or {}).get('taskId')
                if not task_id:
                    last_error = 'Suno не вернул taskId.'
                    continue
                logger.info(f'Suno task created: {task_id} (model={model_id})')
                return await _await_tracks(session, task_id, key, status_cb, cancel_event)

            message = body.get('msg') or _code_msg(code, f'код {code}')
            described = f'{_code_msg(code, "Ошибка Suno")}: {message}'
            key_errors.append(f'{key[:8]}…: {described}')
            logger.warning(f'Suno {code} on key {key[:8]}…: {message}')
            if code == 401:
                await remove_key(key)
            elif code == 430:
                await remove_key(key, 429)
            last_error = described
            if code not in _KEY_ERRORS:
                break

    detail = '\n\nПопытки:\n' + '\n'.join(key_errors[:5]) if key_errors else ''
    return None, f'Suno: {last_error}{detail}'


# ── Music Editing & Transformation Endpoints ─────────────────────────────

async def extend_suno(
    audio_id: str,
    *,
    task_id: str = '',
    model_key: str = 'V6',
    cfg: Optional[dict] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/extend — extend an existing track."""
    model_id = resolve_model_id(model_key) or 'V6'
    cfg = cfg or {}
    instrumental = str(cfg.get('instrumental', '0')) == '1'
    payload: dict[str, Any] = {
        'audioId': audio_id,
        'model': model_id,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
        'instrumental': instrumental,
    }
    if task_id:
        payload['taskId'] = task_id
    if cfg.get('continue_at') is not None and float(cfg['continue_at']) > 0:
        payload['continueAt'] = round(float(cfg['continue_at']), 2)
    if cfg.get('lyrics') and not instrumental:
        payload['lyrics'] = str(cfg['lyrics'])[:5000]
    if cfg.get('style'):
        payload['style'] = str(cfg['style'])[:1000]
    if cfg.get('title'):
        payload['title'] = str(cfg['title'])[:100]
    if cfg.get('neg'):
        payload['negativeTags'] = str(cfg['neg'])[:1000]
    _apply_common_controls(payload, cfg, allow_duration=False, model_id=model_id)
    return await _submit_and_await_music('/generate/extend', payload, status_cb=status_cb, cancel_event=cancel_event)


async def upload_cover_suno(
    *,
    upload_url: str = '',
    task_id: str = '',
    audio_id: str = '',
    model_key: str = 'V6',
    cfg: Optional[dict] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/upload-cover — restyle uploaded audio or existing track."""
    model_id = resolve_model_id(model_key) or 'V6'
    cfg = cfg or {}
    instrumental = str(cfg.get('instrumental', '0')) == '1'
    payload: dict[str, Any] = {
        'model': model_id,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
        'instrumental': instrumental,
    }
    if upload_url:
        payload['uploadUrl'] = upload_url
    elif task_id and audio_id:
        payload['taskId'] = task_id
        payload['audioId'] = audio_id
    else:
        return None, 'Нужен либо uploadUrl, либо taskId + audioId.'

    if cfg.get('lyrics') and not instrumental:
        payload['lyrics'] = str(cfg['lyrics'])[:5000]
    if cfg.get('style'):
        payload['style'] = str(cfg['style'])[:1000]
    if cfg.get('title'):
        payload['title'] = str(cfg['title'])[:80]
    if cfg.get('neg'):
        payload['negativeTags'] = str(cfg['neg'])[:1000]
    _apply_common_controls(payload, cfg, allow_duration=True, model_id=model_id)
    return await _submit_and_await_music('/generate/upload-cover', payload, status_cb=status_cb, cancel_event=cancel_event)


async def upload_extend_suno(
    upload_url: str,
    *,
    model_key: str = 'V6',
    cfg: Optional[dict] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/upload-extend — upload audio and extend in same style."""
    model_id = resolve_model_id(model_key) or 'V6'
    cfg = cfg or {}
    instrumental = str(cfg.get('instrumental', '0')) == '1'
    payload: dict[str, Any] = {
        'uploadUrl': upload_url,
        'model': model_id,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
        'instrumental': instrumental,
    }
    if cfg.get('continue_at') is not None and float(cfg['continue_at']) > 0:
        payload['continueAt'] = round(float(cfg['continue_at']), 2)
    if cfg.get('lyrics') and not instrumental:
        payload['lyrics'] = str(cfg['lyrics'])[:5000]
    if cfg.get('style'):
        payload['style'] = str(cfg['style'])[:1000]
    if cfg.get('title'):
        payload['title'] = str(cfg['title'])[:100]
    if cfg.get('neg'):
        payload['negativeTags'] = str(cfg['neg'])[:1000]
    _apply_common_controls(payload, cfg, allow_duration=False, model_id=model_id)
    return await _submit_and_await_music('/generate/upload-extend', payload, status_cb=status_cb, cancel_event=cancel_event)


async def add_instrumental_suno(
    upload_url: str,
    title: str,
    tags: str,
    negative_tags: str,
    *,
    model_key: str = 'V6',
    cfg: Optional[dict] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/add-instrumental — add backing music to vocal/melody."""
    model_id = resolve_model_id(model_key) or 'V6'
    cfg = cfg or {}
    payload: dict[str, Any] = {
        'uploadUrl': upload_url,
        'title': (title or 'Instrumental')[:80],
        'tags': (tags or 'modern pop, clean mix')[:1000],
        'negativeTags': (negative_tags or 'noise, distortion')[:1000],
        'model': model_id,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    if cfg.get('lyrics'):
        payload['lyrics'] = str(cfg['lyrics'])[:5000]
    _apply_common_controls(payload, cfg, allow_duration=False, model_id=model_id)
    return await _submit_and_await_music('/generate/add-instrumental', payload, status_cb=status_cb, cancel_event=cancel_event)


async def add_vocals_suno(
    upload_url: str,
    title: str,
    style: str,
    negative_tags: str,
    *,
    lyrics: str = '',
    model_key: str = 'V6',
    cfg: Optional[dict] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/add-vocals — layer AI vocals onto instrumental."""
    model_id = resolve_model_id(model_key) or 'V6'
    cfg = cfg or {}
    payload: dict[str, Any] = {
        'uploadUrl': upload_url,
        'title': (title or 'Vocals')[:80],
        'style': (style or 'pop, expressive vocals')[:1000],
        'negativeTags': (negative_tags or 'harsh noise')[:1000],
        'model': model_id,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    lyr = (lyrics or cfg.get('lyrics') or '').strip()
    if lyr:
        payload['lyrics'] = lyr[:5000]
        payload['prompt'] = lyr[:5000]
    _apply_common_controls(payload, cfg, allow_duration=False, model_id=model_id)
    return await _submit_and_await_music('/generate/add-vocals', payload, status_cb=status_cb, cancel_event=cancel_event)


async def replace_section_suno(
    *,
    infill_start_s: float,
    infill_end_s: float,
    prompt: str,
    full_lyrics: str,
    tags: str,
    title: str,
    task_id: str = '',
    audio_id: str = '',
    upload_url: str = '',
    model_key: str = 'V6',
    cfg: Optional[dict] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/replace-section — replace >=10s segment (infill)."""
    start_s = round(float(infill_start_s), 2)
    end_s = round(float(infill_end_s), 2)
    if end_s - start_s < 10.0:
        return None, 'Интервал замены должен быть не меньше 10 секунд.'

    cfg = cfg or {}
    payload: dict[str, Any] = {
        'prompt': (prompt or '')[:5000],
        'fullLyrics': (full_lyrics or prompt or '')[:5000],
        'tags': (tags or 'pop')[:1000],
        'title': (title or 'Edited Track')[:80],
        'infillStartS': start_s,
        'infillEndS': end_s,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    if cfg.get('lyrics'):
        payload['lyrics'] = str(cfg['lyrics'])[:5000]
    if cfg.get('neg'):
        payload['negativeTags'] = str(cfg['neg'])[:1000]

    if upload_url:
        model_id = resolve_model_id(model_key) or 'V6'
        payload['uploadUrl'] = upload_url
        payload['model'] = model_id
    elif task_id and audio_id:
        payload['taskId'] = task_id
        payload['audioId'] = audio_id
        model_id = 'V6'
    else:
        return None, 'Нужен либо uploadUrl, либо taskId + audioId.'

    _apply_common_controls(payload, cfg, allow_duration=False, model_id=model_id)
    return await _submit_and_await_music('/generate/replace-section', payload, status_cb=status_cb, cancel_event=cancel_event)


async def mashup_suno(
    upload_urls: list[str],
    *,
    model_key: str = 'V6',
    cfg: Optional[dict] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/mashup — mix 2 uploaded audio files."""
    if len(upload_urls) < 2:
        return None, 'Для мэшапа нужны 2 аудиофайла.'
    model_id = resolve_model_id(model_key) or 'V6'
    cfg = cfg or {}
    payload: dict[str, Any] = {
        'uploadUrlList': upload_urls[:2],
        'model': model_id,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    if cfg.get('lyrics'):
        payload['lyrics'] = str(cfg['lyrics'])[:5000]
    if cfg.get('style'):
        payload['style'] = str(cfg['style'])[:1000]
    if cfg.get('title'):
        payload['title'] = str(cfg['title'])[:80]
    _apply_common_controls(payload, cfg, allow_duration=True, model_id=model_id)
    return await _submit_and_await_music('/generate/mashup', payload, status_cb=status_cb, cancel_event=cancel_event)


async def generate_persona(
    task_id: str,
    audio_id: str,
    name: str,
    description: str,
    *,
    vocal_start: float = 0.0,
    vocal_end: float = 30.0,
    style: str = '',
) -> Tuple[Optional[dict], Optional[str]]:
    """POST /api/v1/generate/generate-persona — create reusable style persona."""
    payload: dict[str, Any] = {
        'taskId': task_id,
        'audioId': audio_id,
        'name': name[:80],
        'description': description[:500],
        'vocalStart': max(0.0, float(vocal_start)),
        'vocalEnd': max(float(vocal_start) + 1.0, float(vocal_end)),
    }
    if style:
        payload['style'] = style[:200]
    body, _, err = await _call_with_keys('POST', '/generate/generate-persona', payload=payload)
    if err or not body:
        return None, err or 'Не удалось создать персону.'
    return body.get('data') or {}, None


# ── Sounds & Speech Endpoints ────────────────────────────────────────────

SUNO_SOUND_KEYS = (
    'Any',
    'Cm', 'C#m', 'Dm', 'D#m', 'Em', 'Fm', 'F#m', 'Gm', 'G#m', 'Am', 'A#m', 'Bm',
    'C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B',
)


async def generate_sounds(
    prompt: str,
    *,
    model_key: str = 'V6',
    sound_loop: bool = False,
    sound_tempo: Optional[int] = None,
    sound_key: str = 'Any',
    grab_lyrics: bool = False,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/sounds — SFX, loops, ambient sounds."""
    model_id = resolve_model_id(model_key) or 'V6'
    payload: dict[str, Any] = {
        'prompt': (prompt or '').strip()[:500],
        'model': model_id,
        'soundLoop': bool(sound_loop),
        'soundKey': sound_key if sound_key in SUNO_SOUND_KEYS else 'Any',
        'grabLyrics': bool(grab_lyrics),
        'callBackUrl': _PLACEHOLDER_CALLBACK,
    }
    if sound_tempo is not None and 1 <= int(sound_tempo) <= 300:
        payload['soundTempo'] = int(sound_tempo)
    return await _submit_and_await_music('/generate/sounds', payload, status_cb=status_cb, cancel_event=cancel_event)


async def generate_speech(
    *,
    custom_mode: bool = False,
    prompt: str = '',
    script: str = '',
    tone: str = '',
    variety: Optional[int] = None,
    vocal_gender: str = '',
    background_music: Optional[bool] = None,
    status_cb=None,
    cancel_event=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """POST /api/v1/generate/speech — expressive spoken audio (V6 Mini)."""
    if not custom_mode:
        clean_prompt = (prompt or '').strip()[:3000]
        if not clean_prompt:
            return None, 'Укажи описание сцены (prompt).'
        payload: dict[str, Any] = {
            'customMode': False,
            'prompt': clean_prompt,
            'callBackUrl': _PLACEHOLDER_CALLBACK,
        }
    else:
        clean_script = (script or '').strip()[:5000]
        if not clean_script:
            return None, 'Укажи текст речи (script).'
        payload = {
            'customMode': True,
            'script': clean_script,
            'callBackUrl': _PLACEHOLDER_CALLBACK,
        }
        if tone and tone.strip():
            payload['tone'] = tone.strip()[:1000]
        if variety is not None:
            payload['variety'] = max(0, min(4, int(variety)))
        if vocal_gender in ('m', 'f'):
            payload['vocalGender'] = vocal_gender
        if background_music is not None:
            payload['backgroundMusic'] = bool(background_music)
    return await _submit_and_await_music('/generate/speech', payload, status_cb=status_cb, cancel_event=cancel_event)


# ── Record Info & Credits ────────────────────────────────────────────────

async def get_music_record_info(task_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """GET /api/v1/generate/record-info — inspect any music/sound/speech task."""
    body, _, err = await _call_with_keys('GET', '/generate/record-info', params={'taskId': task_id.strip()})
    if err or not body:
        return None, err or 'Не удалось получить статус задачи.'
    return body.get('data') or {}, None


async def get_remaining_credits() -> Tuple[Optional[list[dict]], Optional[str]]:
    """GET /api/v1/generate/credit — check credits across all live Suno keys."""
    keys = await load_suno_keys()
    if not keys:
        return None, 'Нет живых ключей Suno в KeyHunter.'
    results: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for key in keys:
            try:
                _, body = await _get(session, '/generate/credit', key)
                if body.get('code') == 200:
                    results.append({'key': f'{key[:8]}…{key[-4:]}', 'credits': int(body.get('data') or 0), 'ok': True})
                else:
                    results.append({'key': f'{key[:8]}…{key[-4:]}', 'credits': None, 'ok': False,
                                    'error': body.get('msg') or str(body.get('code'))})
            except Exception as e:
                results.append({'key': f'{key[:8]}…{key[-4:]}', 'credits': None, 'ok': False,
                                'error': f'{type(e).__name__}: {e}'})
    return results, None


# Re-export post-processing, lyrics, covers, recovery, stems, MIDI, MP4 & Voice
from services.suno_extra import (  # noqa: E402, F401
    SUNO_STEM_NAMES,
    SUNO_VOICE_LANGUAGES,
    SUNO_SINGER_SKILLS,
    generate_lyrics,
    get_lyrics_record_info,
    get_timestamped_lyrics,
    format_lrc,
    boost_music_style,
    generate_music_cover,
    get_music_cover_details,
    recover_audio,
    get_recovery_audio_details,
    convert_to_wav,
    get_wav_conversion_details,
    separate_vocals,
    get_vocal_separation_details,
    generate_midi,
    get_midi_details,
    build_midi_bytes,
    create_music_video,
    get_music_video_details,
    voice_validate,
    voice_validate_info,
    voice_regenerate,
    voice_generate,
    voice_record_info,
    voice_check_availability,
)
