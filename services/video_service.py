import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import aiohttp
from typing import Tuple, Optional

from config import SYSTEM_PROMPT, GEMINI_VIDEO_TIMEOUT, MAX_VIDEO_FRAMES, VIDEO_FPS, VIDEO_FRAME_SIZE
from keys import load_keys, remove_key

logger = logging.getLogger(__name__)


def _ensure_ai_imports():
    """Late-import shared cache and helpers from ai_services (avoids circular imports)."""
    global _models_cache, _MODELS_CACHE_TTL, _pretty_model_name
    if '_models_cache' not in globals() or globals()['_models_cache'] is None:
        from ai_services import _models_cache as _mc, _MODELS_CACHE_TTL as _mttl, _pretty_model_name as _pmn
        globals()['_models_cache'] = _mc
        globals()['_MODELS_CACHE_TTL'] = _mttl
        globals()['_pretty_model_name'] = _pmn
async def generate_video_with_gemini(prompt: str, video_path: str) -> str:
    keys = await load_keys()
    if not keys:
        return 'Блять, ключи закончились, иди нахуй.'
    temp_dir = tempfile.mkdtemp()
    cmd = ['ffmpeg', '-i', video_path, '-vf', f'fps={VIDEO_FPS},scale={VIDEO_FRAME_SIZE}:-1', '-q:v', '10', os.path.join(temp_dir, 'frame_%04d.jpg')]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parts = []
    frames = sorted(os.listdir(temp_dir))
    if not frames:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 'Не смог извлечь кадры из твоего уебищного видео. Может формат дерьмо?'
    for frame in frames[:MAX_VIDEO_FRAMES]:
        with open(os.path.join(temp_dir, frame), 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
            parts.append({'inlineData': {'mimeType': 'image/jpeg', 'data': b64}})
    for frame in frames:
        os.remove(os.path.join(temp_dir, frame))
    audio_path = os.path.join(temp_dir, 'audio.wav')
    subprocess.run(['ffmpeg', '-i', video_path, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
        with open(audio_path, 'rb') as f:
            audio_b64 = base64.b64encode(f.read()).decode('utf-8')
            parts.append({'inlineData': {'mimeType': 'audio/wav', 'data': audio_b64}})
    shutil.rmtree(temp_dir, ignore_errors=True)
    parts.append({'text': prompt if prompt else 'Что происходит на этом видео? (учитывай и визуальный ряд, и звук, если он есть)'})
    for key in keys.copy():
        url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}'
        payload = {'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]}, 'contents': [{'parts': parts}], 'generationConfig': {'temperature': 1.0, 'thinkingConfig': {'thinkingBudget': 0}}}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=GEMINI_VIDEO_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            return data['candidates'][0]['content']['parts'][0]['text']
                        except KeyError:
                            return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
                    elif resp.status in [429, 403, 400]:
                        resp_text = await resp.text()
                        logging.warning(f'Ошибка ключа (видео) {key[:10]}... Код: {resp.status}. Текст: {resp_text}')
                        if resp.status == 400 and any(w in resp_text.lower() for w in ('safety', 'prohibited', 'harm', 'block', 'policy', 'recitation')):
                            return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
                        if resp.status != 400:
                            remove_key(key, resp.status)
                        continue
                    else:
                        resp_text = await resp.text()
                        logging.error(f'API Error {resp.status}: {resp_text}')
                        continue
            except Exception as e:
                logging.error(f'Сетевая ошибка (видео): {e}')
                continue
    return 'Все ключи проебаны или сдохли, отъебись.'
async def analyze_image_for_veo(image_bytes: bytes, user_prompt: str='') -> str:
    keys = await load_keys()
    if not keys:
        return user_prompt or ''
    key = keys[0]
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}'
    lang_hint = 'на русском' if user_prompt and any(('Ѐ' <= c <= 'ӿ' for c in user_prompt)) else 'in English'
    ask = f'Опиши это изображение подробно {lang_hint} для генерации видео через Veo: что изображено, кто/что главный объект, их внешность, поза, выражение, фон, освещение, стиль. Дай описание в 2-3 предложения — только описание, без пояснений.'
    payload = {'contents': [{'parts': [{'inlineData': {'mimeType': 'image/jpeg', 'data': base64.b64encode(image_bytes).decode()}}, {'text': ask}]}], 'generationConfig': {'temperature': 0.2}}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    description = data['candidates'][0]['content']['parts'][0]['text'].strip()
                    logging.info(f'Gemini описал фото для Veo: {description[:80]}...')
                    return description
        except Exception as e:
            logging.warning(f'Ошибка анализа фото для Veo: {e}')
    return user_prompt or ''

async def start_veo_generation(prompt: str, model: str='veo-2.0-generate-001', image_bytes: bytes=None, state_data: dict = None) -> tuple:
    keys = await load_keys()
    if not keys:
        return (None, None, 'Нет Gemini ключей для генерации видео.')
    prompt_text = prompt if prompt else 'A beautiful cinematic scene'
    for idx, key in enumerate(keys):
        if state_data:
            state_data['status'] = f'Запуск видео {idx+1}/{len(keys)} (Veo)'
        headers = {'Content-Type': 'application/json', 'x-goog-api-key': key}
        instance = {'prompt': prompt_text}
        if image_bytes:
            instance['image'] = {'bytesBase64Encoded': base64.b64encode(image_bytes).decode('utf-8'), 'mimeType': 'image/jpeg'}
        payload = {'instances': [instance], 'parameters': {'aspectRatio': '16:9', 'durationSeconds': 8, 'personGeneration': 'allow_adult' if image_bytes else 'allow_all'}}
        base_url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}'
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(f'{base_url}:predictLongRunning', json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logging.warning(f'Veo start {resp.status} ключ {key[:12]}: {err[:100]}')
                        if resp.status in [429, 403]:
                            continue
                        return (None, None, f'Ошибка Veo ({resp.status}): {err[:200]}')
                    op_data = await resp.json()
                    op_name = op_data.get('name', '')
                    if op_name:
                        return (op_name, key, None)
                    return (None, None, f'Veo не вернул имя операции: {op_data}')
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                return (None, None, f'Ошибка Veo start: {type(e).__name__}: {e}')
    return (None, None, 'Все Gemini ключи исчерпаны.')

async def poll_veo_operation(operation_name: str, api_key: str, state_data: dict = None) -> tuple:
    poll_url = f'https://generativelanguage.googleapis.com/v1beta/{operation_name}?key={api_key}'
    async with aiohttp.ClientSession() as session:
        for i in range(60):
            await asyncio.sleep(5)
            if state_data:
                state_data['status'] = f'Рендеринг видео... ({i*5 + 5} сек / 300)'
            try:
                async with session.get(poll_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    poll_data = await resp.json()
                    if not poll_data.get('done'):
                        continue
                    if 'error' in poll_data:
                        return (None, f"Ошибка Veo: {poll_data['error'].get('message', str(poll_data['error']))}")
                    response_obj = poll_data.get('response', {})
                    video_response = response_obj.get('generateVideoResponse', response_obj)
                    samples = video_response.get('generatedSamples', [])
                    if not samples:
                        reasons = video_response.get('raiMediaFilteredReasons', [])
                        if reasons:
                            return (None, f'Veo заблокировал видео фильтром безопасности:\n{reasons[0][:300]}')
                        return (None, f'Veo не вернул видео: {json.dumps(response_obj, ensure_ascii=False)[:400]}')
                    video = samples[0].get('video', {})
                    encoded = video.get('encodedVideo', '')
                    if encoded:
                        return (base64.b64decode(encoded), None)
                    uri = video.get('uri', '')
                    if uri:
                        dl_headers = {'x-goog-api-key': api_key}
                        async with session.get(uri, headers=dl_headers, timeout=aiohttp.ClientTimeout(total=120)) as dl:
                            if dl.status == 200:
                                return (await dl.read(), None)
                            return (None, f'Ошибка скачивания ({dl.status}): {(await dl.text())[:200]}')
                    return (None, f'Veo: нет видеоданных в ответе.')
            except Exception:
                continue
    return (None, 'Veo: операция не завершилась за 5 минут.')

async def generate_video_with_veo(prompt: str, model: str='veo-2.0-generate-001', image_bytes: bytes=None) -> tuple:
    (op_name, api_key, error) = await start_veo_generation(prompt, model, image_bytes)
    if error:
        return (None, error)
    return await poll_veo_operation(op_name, api_key)
async def fetch_veo_models() -> list:
    _ensure_ai_imports()
    cache_key = 'veo'
    now = __import__('time').time()
    if cache_key in _models_cache and now - _models_cache[cache_key]['ts'] < _MODELS_CACHE_TTL:
        return _models_cache[cache_key]['data']
    keys = await load_keys()
    if not keys:
        return []
    url = f'https://generativelanguage.googleapis.com/v1beta/models?key={keys[0]}&pageSize=200'
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = []
                    for m in data.get('models', []):
                        model_id = m['name'].replace('models/', '')
                        if 'veo' in model_id.lower():
                            result.append((_pretty_model_name(model_id), model_id))
                    _models_cache[cache_key] = {'ts': now, 'data': result}
                    return result
        except Exception as e:
            logging.warning(f'fetch_veo_models: {e}')
    return []


async def generate_video_with_omni(
    prompt: str,
    image_bytes: bytes = None,
    video_bytes: bytes = None,
    aspect_ratio: str = '16:9',
    state_data: dict = None,
) -> tuple:
    """Generate or edit a video via Gemini Omni Flash (Interactions API).

    Supports: text-to-video, image-to-video, video-to-video (edit up to 10s).
    """
    from keys import load_keys
    keys = await load_keys()
    if not keys:
        return (None, 'Нет Gemini ключей.')
    url = 'https://generativelanguage.googleapis.com/v1beta/interactions'
    input_parts = []
    if image_bytes:
        input_parts.append({
            'inlineData': {'mimeType': 'image/jpeg', 'data': base64.b64encode(image_bytes).decode()}
        })
    if video_bytes:
        input_parts.append({
            'inlineData': {'mimeType': 'video/mp4', 'data': base64.b64encode(video_bytes).decode()}
        })
    input_parts.append({'text': prompt or 'A beautiful cinematic scene'})
    task = 'video_to_video' if video_bytes else ('image_to_video' if image_bytes else 'text_to_video')
    payload = {
        'model': 'gemini-omni-flash-preview',
        'input': input_parts,
        'response_format': {'type': 'video', 'aspect_ratio': aspect_ratio},
        'generation_config': {'video_config': {'task': task}},
    }
    key_errors = []
    for idx, key in enumerate(keys):
        if state_data:
            state_data['status'] = f'Omni Flash {idx+1}/{len(keys)}'
        headers = {'Content-Type': 'application/json', 'x-goog-api-key': key}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for step in data.get('steps', []):
                            if step.get('type') == 'model_output':
                                content = step.get('content', {})
                                if content.get('type') == 'video':
                                    b64 = content.get('data', '')
                                    if b64:
                                        return (base64.b64decode(b64), None)
                                    uri = content.get('uri', '')
                                    if uri:
                                        async with session.get(uri, timeout=aiohttp.ClientTimeout(total=60)) as dl:
                                            if dl.status == 200:
                                                return (await dl.read(), None)
                        logging.error(f'Omni returned no video. Full response: {json.dumps(data, ensure_ascii=False)[:500]}')
                        key_errors.append(f'200 но без видео')
                        continue
                    err = await resp.text()
                    logging.warning(f'Omni Flash key {idx} HTTP {resp.status}: {err[:200]}')
                    key_errors.append(f'{resp.status}: {err[:150]}')
                    if resp.status == 404:
                        break  # модель не найдена — бесполезно пробовать другие ключи
                    if resp.status in (429, 403):
                        from keys import remove_key
                        remove_key(key, resp.status)
                        continue
            except asyncio.TimeoutError:
                key_errors.append('TimeoutError')
                continue
            except Exception as e:
                key_errors.append(f'{type(e).__name__}: {e}')
                continue
    details = '\n'.join(key_errors[:5]) if key_errors else 'нет деталей'
    return (None, f'Omni Flash не ответил.\n\nОшибки по ключам:\n{details}')
