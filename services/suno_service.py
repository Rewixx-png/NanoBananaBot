"""Suno music generation — text-to-song via the sunoapi.org aggregator.

Flow is asynchronous: POST /generate returns a task id, then
GET /generate/record-info?taskId=… is polled until tracks appear. The API
demands a callBackUrl on submit, but the docs allow polling instead, so a
placeholder is sent and no inbound endpoint is needed.
"""
import asyncio
import logging
from typing import Optional, Tuple

import aiohttp

from keys import load_suno_keys, remove_key

logger = logging.getLogger(__name__)

SUNO_BASE_URL = 'https://api.sunoapi.org/api/v1'

# The API sits behind Cloudflare, which answers 403 to requests without a
# User-Agent — an empty one fails even with a valid key.
_USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
)
# Submitted because the schema requires it; unused, since we poll instead.
_PLACEHOLDER_CALLBACK = 'https://example.com/suno-callback'

SUNO_POLL_INTERVAL = 5
SUNO_POLL_TIMEOUT = 420
SUNO_TIMEOUT = 60

SUNO_MODELS = {
    'suno-v6': {'id': 'V6', 'label': '🤘 Suno V6', 'desc': 'Лучшее качество, живые вокалы'},
    'suno-v6-wild': {'id': 'V6_WILD', 'label': '🔥 Suno V6 Wild', 'desc': 'Смелые эксперименты'},
    'suno-v6-mini': {'id': 'V6_MINI', 'label': '⚡ Suno V6 Mini', 'desc': 'Быстро и дёшево'},
}

# Documented body codes; HTTP status is 200 even for failures.
_CODE_MESSAGES = {
    400: 'Некорректные параметры запроса',
    401: 'Ключ Suno недействителен',
    405: 'Превышен лимит запросов',
    413: 'Промпт или текст слишком длинный',
    429: 'На ключе кончились кредиты',
    430: 'Слишком частые запросы, попробуй позже',
    455: 'Техобслуживание на стороне Suno',
    500: 'Ошибка сервера Suno',
}
# Retrying with another key only helps for key-specific failures.
_KEY_ERRORS = {401, 429}


async def _post(session: aiohttp.ClientSession, path: str, key: str, payload: dict) -> tuple[int, dict]:
    headers = {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'User-Agent': _USER_AGENT,
    }
    async with session.post(SUNO_BASE_URL + path, json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=SUNO_TIMEOUT)) as resp:
        try:
            return resp.status, await resp.json()
        except Exception:
            return resp.status, {'msg': (await resp.text())[:200]}


async def _get_detail(session: aiohttp.ClientSession, task_id: str, key: str) -> dict:
    headers = {'Authorization': f'Bearer {key}', 'User-Agent': _USER_AGENT}
    async with session.get(f'{SUNO_BASE_URL}/generate/record-info',
                           params={'taskId': task_id}, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=SUNO_TIMEOUT)) as resp:
        return await resp.json()


def build_payload(prompt: str, model_id: str, cfg: dict) -> dict:
    """Map the /music settings UI onto the API's custom-mode body."""
    style = (cfg.get('style') or '').strip() or (prompt or '').strip()
    instrumental = str(cfg.get('instrumental', '0')) == '1'
    lyrics = (cfg.get('lyrics') or '').strip()

    payload: dict = {
        'customMode': True,
        'instrumental': instrumental,
        'model': model_id,
        'callBackUrl': _PLACEHOLDER_CALLBACK,
        'duration': int(cfg.get('duration', 60)),
        'styleWeight': float(cfg.get('style_weight', 0.65)),
        'weirdnessConstraint': float(cfg.get('weirdness', 0.25)),
        'variety': int(cfg.get('variety', 1)),
    }
    # In custom mode the API needs at least one of style/lyrics/negativeTags.
    if style:
        payload['style'] = style
    if lyrics and not instrumental:
        payload['lyrics'] = lyrics
    if cfg.get('title'):
        payload['title'] = str(cfg['title'])[:80]
    if cfg.get('neg'):
        payload['negativeTags'] = str(cfg['neg'])[:1000]
    if cfg.get('vocal') and not instrumental:
        payload['vocalGender'] = str(cfg['vocal'])
    # audioWeight needs vocals — the API rejects it for instrumentals.
    if not instrumental and 'audio_weight' in cfg:
        payload['audioWeight'] = float(cfg['audio_weight'])
    if not payload.get('style') and not payload.get('lyrics') and not payload.get('negativeTags'):
        # Nothing to sing about and no style — the request would be rejected.
        payload['style'] = 'instrumental music'
    return payload


async def generate_suno(
    prompt: str,
    model_key: str = 'suno-v6',
    cfg: Optional[dict] = None,
    status_cb=None,
) -> Tuple[Optional[list[dict]], Optional[str]]:
    """Generate a song. Returns (tracks, error).

    Each track is ``{'audio': bytes, 'cover': bytes | None, 'title': str,
    'tags': str, 'duration': float}``. Suno always returns two variations.
    """
    if model_key not in SUNO_MODELS:
        return None, f'Неизвестная модель Suno: {model_key}'
    cfg = cfg or {}
    model_id = SUNO_MODELS[model_key]['id']

    keys = await load_suno_keys()
    if not keys:
        return None, ('Нет живых ключей Suno в KeyHunter. Проверь, что keyhunter '
                      'валидирует сервис Suno и помечает ключи как live.')

    payload = build_payload(prompt, model_id, cfg)
    key_errors: list[str] = []
    last_error = 'Suno не ответил.'

    async with aiohttp.ClientSession() as session:
        for key in keys:
            try:
                http_status, body = await _post(session, '/generate', key, payload)
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
                return await _await_tracks(session, task_id, key, status_cb)

            message = body.get('msg') or _CODE_MESSAGES.get(code, f'код {code}')
            described = f'{_CODE_MESSAGES.get(code, "Ошибка Suno")}: {message}'
            key_errors.append(f'{key[:8]}…: {described}')
            logger.warning(f'Suno {code} on key {key[:8]}…: {message}')
            if code == 401:
                await remove_key(key)          # invalid key — retire it
            elif code == 430:
                await remove_key(key, 429)     # rate limited — cool down, rotate
            last_error = described
            if code not in _KEY_ERRORS:
                break                          # request is bad; another key won't help

    detail = f'\n\nПопытки:\n' + '\n'.join(key_errors[:5]) if key_errors else ''
    return None, f'Suno: {last_error}{detail}'


async def _await_tracks(session, task_id: str, key: str, status_cb) -> Tuple[Optional[list], Optional[str]]:
    """Poll the task until tracks are ready, then download audio and covers."""
    waited = 0
    while waited <= SUNO_POLL_TIMEOUT:
        try:
            body = await _get_detail(session, task_id, key)
        except Exception as e:
            return None, f'Suno: не смог опросить задачу ({type(e).__name__}: {e})'

        data = body.get('data') or {}
        response = data.get('response') or {}
        tracks = response.get('sunoData') or []
        ready = [t for t in tracks if t.get('audio_url') or t.get('audioUrl')]

        if ready:
            downloaded = await _download_tracks(session, ready)
            if downloaded:
                return downloaded, None
            return None, 'Suno: не удалось скачать готовые треки.'

        # Documented failure signals; the API omits `status` on success.
        error = response.get('errorMessage') or data.get('errorMessage')
        status = response.get('status') or ''
        if error or status.endswith('_FAILED') or status in ('SENSITIVE_WORD_ERROR', 'CALLBACK_EXCEPTION'):
            reason = error or _CODE_MESSAGES.get(0, status) or status
            if status == 'SENSITIVE_WORD_ERROR':
                reason = 'текст или стиль содержат запрещённые слова'
            return None, f'Suno не смог сгенерировать: {reason}'

        if status_cb:
            try:
                await status_cb(f'🎼 Suno генерирует… ({waited}с)')
            except Exception:
                pass
        await asyncio.sleep(SUNO_POLL_INTERVAL)
        waited += SUNO_POLL_INTERVAL

    return None, f'Suno: таймаут {SUNO_POLL_TIMEOUT}с — задача {task_id} не завершилась.'


async def _download_tracks(session, tracks: list[dict]) -> list[dict]:
    result = []
    for track in tracks:
        audio_url = track.get('audio_url') or track.get('audioUrl')
        if not audio_url:
            continue
        try:
            async with session.get(audio_url, headers={'User-Agent': _USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    logger.warning(f'Suno audio {resp.status} for {audio_url[:80]}')
                    continue
                audio = await resp.read()
        except Exception as e:
            logger.warning(f'Suno audio download failed: {type(e).__name__}: {e}')
            continue

        cover = None
        cover_url = track.get('image_url') or track.get('imageUrl')
        if cover_url:
            try:
                async with session.get(cover_url, headers={'User-Agent': _USER_AGENT},
                                       timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        cover = await resp.read()
            except Exception:
                pass

        result.append({
            'audio': audio,
            'cover': cover,
            'title': track.get('title') or 'Suno',
            'tags': track.get('tags') or '',
            'duration': track.get('duration') or 0,
            'prompt': track.get('prompt') or '',
        })
    return result
