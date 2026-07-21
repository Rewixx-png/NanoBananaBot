import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import aiohttp
from typing import Optional

from config import SYSTEM_PROMPT, GEMINI_VIDEO_TIMEOUT, MAX_VIDEO_FRAMES, VIDEO_FPS, VIDEO_FRAME_SIZE
from keys import load_keys
from services.security_utils import is_safe_url
from shared_types import _models_cache, _MODELS_CACHE_TTL, _pretty_model_name, _gemini_url, _gemini_headers, gemini_post, gemini_text_of

logger = logging.getLogger(__name__)
async def generate_video_with_gemini(prompt: str, video_path: str) -> str:
    temp_dir = tempfile.mkdtemp()
    cmd = ['ffmpeg', '-i', video_path, '-vf', f'fps={VIDEO_FPS},scale={VIDEO_FRAME_SIZE}:-1', '-q:v', '10', os.path.join(temp_dir, 'frame_%04d.jpg')]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 'ffmpeg не установлен, блять. Установи ffmpeg и попробуй снова.'
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logging.error(f'Ошибка ffmpeg при извлечении кадров: {e}')
        return 'Не смог обработать видео, что-то пошло не так с ffmpeg.'
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
    try:
        subprocess.run(['ffmpeg', '-i', video_path, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        logging.warning('ffmpeg не найден, пропускаем извлечение аудио.')
    except Exception as e:
        logging.warning(f'Ошибка ffmpeg при извлечении аудио: {e}')
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
        with open(audio_path, 'rb') as f:
            audio_b64 = base64.b64encode(f.read()).decode('utf-8')
            parts.append({'inlineData': {'mimeType': 'audio/wav', 'data': audio_b64}})
    shutil.rmtree(temp_dir, ignore_errors=True)
    parts.append({'text': prompt if prompt else 'Что происходит на этом видео? (учитывай и визуальный ряд, и звук, если он есть)'})
    payload = {'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]}, 'contents': [{'parts': parts}], 'generationConfig': {'temperature': 1.0, 'thinkingConfig': {'thinkingBudget': -1}}}
    data, _key, err = await gemini_post("models/gemini-3.5-flash:generateContent", payload, timeout=GEMINI_VIDEO_TIMEOUT)
    if data is not None:
        text = gemini_text_of(data)
        if text:
            return text
        return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
    if err == 'нет живых Gemini ключей':
        return 'Блять, ключи закончились, иди нахуй.'
    if err and err.startswith('HTTP 400:') and any(w in err.lower() for w in ('safety', 'prohibited', 'harm', 'block', 'policy', 'recitation')):
        return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
    return 'Все ключи проебаны или сдохли, отъебись.'

async def start_veo_generation(prompt: str, model: str='veo-2.0-generate-001', image_bytes: bytes=None, state_data: dict = None) -> tuple:
    prompt_text = prompt if prompt else 'A beautiful cinematic scene'
    instance = {'prompt': prompt_text}
    if image_bytes:
        instance['image'] = {'bytesBase64Encoded': base64.b64encode(image_bytes).decode('utf-8'), 'mimeType': 'image/jpeg'}
    payload = {'instances': [instance], 'parameters': {'aspectRatio': '16:9', 'durationSeconds': 8, 'personGeneration': 'allow_adult' if image_bytes else 'allow_all'}}
    data, key, err = await gemini_post(f"models/{model}:predictLongRunning", payload, timeout=30)
    if data is not None:
        op_name = data.get('name', '')
        if op_name:
            return (op_name, key, None)
        return (None, None, f'Veo не вернул имя операции: {data}')
    if err == 'нет живых Gemini ключей':
        return (None, None, 'Нет Gemini ключей для генерации видео.')
    if err and err.startswith('HTTP 400:'):
        return (None, None, err)
    return (None, None, 'Все Gemini ключи исчерпаны.')

async def poll_veo_operation(operation_name: str, api_key: str, state_data: dict = None) -> tuple:
    poll_url = _gemini_url(operation_name)
    async with aiohttp.ClientSession() as session:
        for i in range(60):
            await asyncio.sleep(5)
            if state_data:
                state_data['status'] = f'Рендеринг видео... ({i*5 + 5} сек / 300)'
            try:
                async with session.get(poll_url, headers=_gemini_headers(api_key), timeout=aiohttp.ClientTimeout(total=15)) as resp:
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

async def fetch_veo_models() -> list:
    cache_key = 'veo'
    now = __import__('time').time()
    if cache_key in _models_cache and now - _models_cache[cache_key]['ts'] < _MODELS_CACHE_TTL:
        return _models_cache[cache_key]['data']
    keys = await load_keys()
    if not keys:
        return []
    url = _gemini_url("models") + "?pageSize=200"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=_gemini_headers(keys[0]), timeout=aiohttp.ClientTimeout(total=10)) as resp:
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


_omni_good_proxies: list = []  # прокси, через которые Google реально ответил — пробуем первыми


async def _omni_proxy_list() -> list:
    """Прокси для обхода EEA-блока редактирования: OMNI_PROXY env или общий пул keyhunter в Redis."""
    env_px = os.environ.get('OMNI_PROXY')
    if env_px:
        return [env_px]
    try:
        import random
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'), socket_timeout=3, socket_connect_timeout=3)
        try:
            raw = await r.smembers('kh:proxies:valid')
        finally:
            await r.aclose()
        pool = [p.decode() if isinstance(p, bytes) else p for p in raw]
        random.shuffle(pool)
        # проверенные прокси — в начало, остальные случайно
        good = [p for p in _omni_good_proxies if p in pool]
        rest = [p for p in pool if p not in good]
        return good + rest
    except Exception as e:
        logging.warning(f'Omni: не смог прочитать пул прокси из Redis: {type(e).__name__}: {e}')
        return []


async def _sanitize_video_prompt(prompt: str) -> Optional[str]:
    """Переписывает промпт так, чтобы он прошёл фильтр безопасности Google, сохраняя смысл."""
    keys = await load_keys()
    if not keys:
        return None
    instruction = (
        "Rewrite the following video generation prompt so it fully complies with Google's Generative AI Prohibited Use Policy. "
        "Keep the original intent, subject and scene, but remove or soften any wording that could read as sexual, violent, "
        "hateful, self-harm or person-harm content, including slang, typos and ambiguous words. "
        "Output ONLY the rewritten prompt in English, no explanations.\n\nPrompt: " + prompt
    )
    payload = {'contents': [{'parts': [{'text': instruction}]}]}
    for key in keys[:10]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(_gemini_url('models/gemini-3.5-flash:generateContent'), json=payload, headers=_gemini_headers(key), timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data['candidates'][0]['content']['parts'][0]['text'].strip()
        except Exception as e:
            logging.warning(f'Omni sanitize prompt: {type(e).__name__}: {e}')
            continue
    return None


async def generate_video_with_omni(
    prompt: str,
    image_bytes: bytes = None,
    video_bytes: bytes = None,
    aspect_ratio: str = '16:9',
    state_data: dict = None,
    _sanitized: bool = False,
) -> tuple:
    """Generate or edit a video via Gemini Omni Flash (Interactions API).

    Supports: text-to-video, image-to-video, video-to-video (edit up to 10s).
    """
    import aiosqlite
    from keys.manager import REWTEST_DB
    # Load keys with their referrer info from Keyhunter
    keys_with_ref = []
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            async with db.execute(
                "SELECT key, info FROM keys WHERE service='Gemini' AND is_live=1 AND info LIKE '%PAID%'"
            ) as cur:
                rows = await cur.fetchall()
                import re as _re
                for key, info in rows:
                    ref = ''
                    m = _re.search(r'referrer:\s*(https?://[^\s|]+)', info or '')
                    if m:
                        ref = m.group(1).strip()
                    keys_with_ref.append((key, ref))
    except Exception as e:
        logger.warning(f'Omni Flash: не смог прочитать ключи из keyhunter DB: {type(e).__name__}: {e}')
    if not keys_with_ref:
        return (None, 'Нет Gemini ключей с доступом к Omni Flash: в keyhunter нет живых ключей с пометкой PAID.')
    url = 'https://generativelanguage.googleapis.com/v1beta/interactions'
    # Build input array with type field per Omni API spec
    omni_input = []
    if image_bytes:
        omni_input.append({
            'type': 'image', 'data': base64.b64encode(image_bytes).decode(), 'mime_type': 'image/jpeg'
        })
    if video_bytes:
        omni_input.append({
            'type': 'video', 'data': base64.b64encode(video_bytes).decode(), 'mime_type': 'video/mp4'
        })
    # Translate Russian prompts to English (Google safety filter is stricter on Russian)
    final_prompt = prompt or 'A beautiful cinematic scene'
    if prompt and any('Ѐ' <= c <= 'ӿ' for c in prompt):
        try:
            from services.gemini_text import translate_to_english
            translated = await translate_to_english(prompt)
            if translated and translated != prompt:
                final_prompt = translated
                logging.info(f'Omni: translated prompt to English: {final_prompt[:80]}...')
        except Exception:
            pass
    if omni_input:
        omni_input.append({'type': 'text', 'text': final_prompt})
    else:
        # Text-only: input can be a plain string
        omni_input = final_prompt
    task = 'edit' if video_bytes else ('image_to_video' if image_bytes else 'text_to_video')
    payload = {
        'model': 'gemini-omni-flash-preview',
        'input': omni_input,
        'generation_config': {'video_config': {'task': task}},
    }
    if not isinstance(omni_input, str) and task != 'edit':
        payload['response_format'] = {'type': 'video', 'aspect_ratio': aspect_ratio}
    # Редактура загруженных видео заблокирована из EEA (сервер в DE) — идём через прокси вне EEA
    omni_proxies = await _omni_proxy_list() if video_bytes else []
    if omni_proxies:
        # сначала прокси, внутри ключи: живой не-EEA выход важнее перебора ключей
        attempts = [(key, ref, px) for px in omni_proxies[:5] for (key, ref) in keys_with_ref]
    else:
        attempts = [(key, ref, None) for (key, ref) in keys_with_ref]
    key_errors = []
    for idx, (key, referrer, proxy) in enumerate(attempts):
        if state_data:
            state_data['status'] = f'Omni Flash {idx+1}/{len(attempts)}'
        headers = {'Content-Type': 'application/json', 'x-goog-api-key': key}
        if referrer:
            headers['Referer'] = referrer
        connector = None
        if proxy:
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy)
            except Exception as e:
                key_errors.append(f'proxy {proxy}: {type(e).__name__}')
                continue
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                    if resp.status == 200:
                        if proxy and proxy not in _omni_good_proxies:
                            _omni_good_proxies.insert(0, proxy)
                            del _omni_good_proxies[5:]
                        data = await resp.json()
                        steps = data if isinstance(data, list) else data.get('steps', [])
                        for step in steps:
                            if not isinstance(step, dict):
                                continue
                            # Content can be list or dict
                            contents = step.get('content', [])
                            if isinstance(contents, dict):
                                contents = [contents]
                            for ct in contents:
                                if isinstance(ct, dict) and ct.get('type') == 'video':
                                    b64 = ct.get('data', '')
                                    if b64:
                                        return (base64.b64decode(b64), None)
                                    uri = ct.get('uri', '')
                                    if uri:
                                        if not is_safe_url(uri):
                                            continue
                                        async with session.get(uri, timeout=aiohttp.ClientTimeout(total=60)) as dl:
                                            if dl.status == 200:
                                                return (await dl.read(), None)
                        # Check for output_video field
                        output_video = data.get('output_video') if isinstance(data, dict) else None
                        if output_video and isinstance(output_video, dict):
                            b64 = output_video.get('data', '')
                            if b64:
                                return (base64.b64decode(b64), None)
                        # Log what we got for debugging
                        step_types = [s.get('type','?') for s in steps if isinstance(s, dict)]
                        for step in steps:
                            if step.get('type') == 'model_output':
                                ct = step.get('content', {})
                                logging.error(f'Omni model_output content keys: {list(ct.keys()) if isinstance(ct, dict) else type(ct).__name__}')
                                if isinstance(ct, dict) and ct.get('type') == 'video':
                                    d = ct.get('data', '')
                                    u = ct.get('uri', '')
                                    logging.error(f'Omni video content: has_data={bool(d)}, has_uri={bool(u)}, data_len={len(d) if d else 0}')
                        logging.error(f'Omni: {len(steps)} steps, types={step_types}, has_output_video={bool(output_video)}')
                        key_errors.append(f'200: видео не найдено (steps={len(steps)}, types={step_types})')
                        continue
                    err = await resp.text()
                    logging.warning(f'Omni Flash key {idx} HTTP {resp.status}: {err[:200]}')
                    if resp.status == 404:
                        # Ключ без доступа к Omni — пробуем следующий
                        key_errors.append(f'ключ {idx+1}: 404 нет доступа к модели')
                        continue
                    # 429/403 = ключ мёртв или в лимите → следующий
                    if resp.status in (429, 403):
                        from keys import remove_key
                        remove_key(key, resp.status)
                        key_errors.append(f'ключ {idx+1}: HTTP {resp.status}: {err[:150]}')
                        if resp.status == 403 and 'leaked' in err.lower():
                            # Слитый ключ мёртв навсегда — выключаем в keyhunter, чтобы не возвращался
                            try:
                                async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
                                    await db.execute("UPDATE keys SET is_live=0, info=info||' ❌ LEAKED' WHERE key=? AND is_live=1", (key,))
                                    await db.commit()
                            except Exception as e:
                                logging.warning(f'Omni Flash: не смог пометить слитый ключ в keyhunter: {type(e).__name__}: {e}')
                        continue
                    # Блок по региону/контенту: через прокси — пробуем следующий (выход может быть EEA или в регионе без Interactions API)
                    err_low = err.lower()
                    if resp.status == 400 and ('input blocked' in err_low or 'not available in your current location' in err_low):
                        if video_bytes:
                            if omni_proxies:
                                key_errors.append(f'{proxy}: региональный блок (400)')
                                continue
                            return (None, 'Omni Flash: Google блокирует редактирование загруженных видео для региона EEA/UK/CH (сервер в Германии), а пул прокси пуст. Задай прокси вне EEA в OMNI_PROXY (ecosystem.config.js) или пополни пул keyhunter (/addproxies).')
                        if 'input blocked' not in err_low:
                            return (None, f'Omni Flash ошибка API ({resp.status}):\n{err[:500]}')
                        if not _sanitized:
                            safe = await _sanitize_video_prompt(final_prompt)
                            if safe and safe != final_prompt:
                                logging.info(f'Omni: промпт заблокирован фильтром, retry со смягчённым: {safe[:80]}')
                                return await generate_video_with_omni(safe, image_bytes=image_bytes, video_bytes=video_bytes, aspect_ratio=aspect_ratio, state_data=state_data, _sanitized=True)
                        return (None, f'Omni Flash: промпт заблокирован фильтром безопасности Google (даже после смягчения — возможно, дело в самом фото):\n{err[:400]}')
                    # Выходной фильтр: видео сгенерировалось, но Google его убил — обычно из-за реального человека в кадре
                    if resp.status == 400 and ('request blocked' in err_low or 'filtered out' in err_low or 'harmful content' in err_low):
                        return (None, 'Omni Flash: видео сгенерировалось, но Google отфильтровал результат — это не твой промпт. Причина почти всегда: на видео реальный человек, а редактура людей ограничена политикой Google (особенно если лицо выглядит молодо). Попробуй: другое видео, сцену без людей, или нейтральную правку (стиль, свет, фон).')
                    # Прочие 4xx — зависят от запроса, а не от ключа
                    return (None, f'Omni Flash ошибка API ({resp.status}):\n{err[:500]}')
            except asyncio.TimeoutError:
                key_errors.append('TimeoutError')
                continue
            except Exception as e:
                key_errors.append(f'{type(e).__name__}: {e}')
                continue
    details = '\n'.join(key_errors[:5]) if key_errors else 'нет деталей'
    region_hint = '\n\nРедактура загруженных видео доступна только через прокси вне EEA — если выше сплошные Input blocked, в пуле не нашлось не-EEA выхода.' if video_bytes else ''
    return (None, f'Omni Flash не ответил.\n\nОшибки по попыткам:\n{details}{region_hint}')
