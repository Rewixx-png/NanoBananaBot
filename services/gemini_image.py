import asyncio
import base64
import json
import logging
import aiohttp
from typing import Tuple, Optional, Dict, Any

from config import SYSTEM_PROMPT
from keys import load_keys, remove_key, strip_code_fences

logger = logging.getLogger(__name__)


def _http_explain(code: int) -> str:
    return {
        429: '(слишком много запросов)',
        403: '(доступ запрещён)',
        500: '(ошибка сервера)',
        502: '(шлюз недоступен)',
        503: '(сервер перегружен)',
        504: '(таймаут шлюза)',
    }.get(code, '')


def _ensure_ai_imports():
    """Late-import shared cache and helpers from ai_services."""
    g = globals()
    if '_models_cache' not in g or g['_models_cache'] is None:
        from ai_services import (_models_cache as _mc, _MODELS_CACHE_TTL as _mttl,
                                 _pretty_model_name as _pmn, _guess_image_mime as _gim,
                                 _TEXT_MODEL_FALLBACKS as _tfm, _thinking_config as _tc)
        g['_models_cache'] = _mc
        g['_MODELS_CACHE_TTL'] = _mttl
        g['_pretty_model_name'] = _pmn
        g['_guess_image_mime'] = _gim
        g['_TEXT_MODEL_FALLBACKS'] = _tfm
        g['_thinking_config'] = _tc


# ── Gemini image service ──────────────────────────────────────────────────

async def classify_draw_intent_with_gemini(prompt: str, has_replied_image: bool=False) -> dict[str, Any]:
    keys = await load_keys()
    if not keys:
        return {'draw_request': False, 'edit_request': False, 'prompt': ''}
    system = '''Classify whether the user wants the bot to create or edit a visual image. Return ONLY JSON:
{"draw_request": true/false, "edit_request": true/false, "prompt": "clean visual prompt or edit instruction"}

draw_request=true means the user wants a concrete visual result: draw, generate, create, render, make an art/logo/sticker/photo/picture/illustration/design/character/object/scene.
edit_request=true means the user is replying to an existing image and wants that image changed, corrected, restyled, recolored, expanded, or modified.
Return false for: normal chat, asking how to draw, talking about art tools, code generation, web search, compliments about an image, or vague messages with no visual deliverable.
Do not require exact trigger words. Infer natural intent from slang, Russian, English, or mixed text.'''
    user_text = f'has_replied_image={has_replied_image}\nuser_message={prompt[:1800]}'
    for model_name in _TEXT_MODEL_FALLBACKS:
        payload = {
            'systemInstruction': {'parts': [{'text': system}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_text}]}],
            'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json', 'thinkingConfig': _thinking_config(model_name, 'minimal')},
        }
        for key in keys:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}'
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                        if resp.status == 404:
                            break
                        if resp.status == 200:
                            data = await resp.json()
                            raw = data['candidates'][0]['content']['parts'][0]['text']
                            parsed = json.loads(strip_code_fences(raw))
                            clean_prompt = str(parsed.get('prompt') or '').strip()
                            return {
                                'draw_request': bool(parsed.get('draw_request')),
                                'edit_request': bool(parsed.get('edit_request')) and has_replied_image,
                                'prompt': clean_prompt,
                            }
                        if resp.status in [429, 403]:
                            remove_key(key, resp.status)
            except Exception as e:
                logging.warning(f'Draw intent classifier failed: {type(e).__name__}: {e}')
                continue
    return {'draw_request': False, 'edit_request': False, 'prompt': ''}



async def review_image_with_gemini(image_bytes: bytes, prompt: str) -> Tuple[bool, str]:
    keys = await load_keys()
    if not keys or not image_bytes:
        return (False, 'No Gemini key or image bytes available for visual review.')
    from ai_services import _guess_image_mime, _TEXT_MODEL_FALLBACKS, _thinking_config
    system = '''You are a practical QA critic for AI-generated images. Return ONLY JSON:
{"ok": true/false, "fix": "short concrete fix instruction"}

ok=true only if the main subject is recognizable, the key prompt elements are present, and the result looks like an intentional finished picture.
ok=false for blank/near-blank images, primitive placeholder sketches, major mismatch, missing main subject, obvious broken object structure, severe artifacts, or unreadable required text.
The fix must be a concrete instruction for the next generation attempt.'''
    parts = [
        {'inlineData': {'mimeType': _guess_image_mime(image_bytes), 'data': base64.b64encode(image_bytes).decode('utf-8')}},
        {'text': f'Original user prompt: {prompt[:1500]}\nCheck this generated image.'},
    ]
    for model_name in _TEXT_MODEL_FALLBACKS:
        payload = {
            'systemInstruction': {'parts': [{'text': system}]},
            'contents': [{'role': 'user', 'parts': parts}],
            'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json', 'thinkingConfig': _thinking_config(model_name, 'minimal')},
        }
        for key in keys:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}'
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status == 404:
                            break
                        if resp.status == 200:
                            data = await resp.json()
                            raw = data['candidates'][0]['content']['parts'][0]['text']
                            parsed = json.loads(strip_code_fences(raw))
                            return (bool(parsed.get('ok')), str(parsed.get('fix') or '').strip())
                        if resp.status in [429, 403]:
                            remove_key(key, resp.status)
            except Exception as e:
                logging.warning(f'Image review failed: {type(e).__name__}: {e}')
                continue
    return (False, 'Review failed. Make the next image clearer, more detailed, and more literal to the prompt.')



async def generate_reviewed_image_with_gemini(prompt: str, image_bytes: Optional[bytes]=None, model: str='gemini-3.1-flash-image-preview', max_attempts: int=3, temperature: float=1.0, state_data: Optional[dict[str, Any]]=None) -> Tuple[Optional[bytes], Optional[str], str]:
    attempts = max(1, min(max_attempts, 3))
    last_img = None
    last_error = None
    critique = ''
    for attempt in range(attempts):
        if state_data:
            state_data['status'] = f'Генерация {attempt + 1}/{attempts}'
        source_image = image_bytes if attempt == 0 else last_img
        effective_prompt = prompt
        if attempt > 0 and critique:
            effective_prompt = f'{prompt}\n\nFix the previous image using this critique: {critique}. Keep the intended subject and improve the result.'
        (result_img, error_msg) = await generate_image_with_gemini(effective_prompt, image_bytes=source_image, model=model, temperature=temperature, state_data=state_data or {})
        if error_msg:
            last_error = error_msg
            if not last_img:
                continue
            break
        if not result_img:
            last_error = 'Gemini не вернул изображение.'
            continue
        last_img = result_img
        if state_data:
            state_data['status'] = f'Самопроверка {attempt + 1}/{attempts}'
        (ok, critique) = await review_image_with_gemini(result_img, prompt)
        if ok:
            return (result_img, None, critique)
    if last_img:
        return (last_img, None, critique)
    return (None, last_error or 'Не удалось получить изображение.', critique)


async def analyze_photo_with_gemini(image_bytes: bytes, prompt: str) -> str:
    keys = await load_keys()
    if not keys:
        return 'Блять, ключи закончились, иди нахуй.'
    if not prompt:
        prompt = 'Что на этом фото?'
    mime = _guess_image_mime(image_bytes)
    system = _build_text_system_prompt(allow_web_directive=False)
    parts = [
        {'inlineData': {'mimeType': mime, 'data': base64.b64encode(image_bytes).decode()}},
        {'text': prompt},
    ]
    payload = {
        'systemInstruction': {'parts': [{'text': system}]},
        'contents': [{'role': 'user', 'parts': parts}],
        'generationConfig': {'temperature': 1.0, 'thinkingConfig': {'thinkingLevel': 'minimal'}},
    }
    for key in keys:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            return data['candidates'][0]['content']['parts'][0]['text']
                        except KeyError:
                            return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
                    elif resp.status in [429, 403, 400]:
                        resp_text = await resp.text()
                        logging.warning(f'Ошибка ключа (фото) {key[:10]}... Код: {resp.status}. Текст: {resp_text}')
                        if resp.status == 400 and any(w in resp_text.lower() for w in ('safety', 'prohibited', 'harm', 'block', 'policy', 'recitation')):
                            return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
                        remove_key(key, resp.status)
                        continue
                    else:
                        continue
        except Exception as e:
            logging.error(f'Сетевая ошибка (фото): {e}')
            continue
    return 'Все ключи проебаны или сдохли, отъебись.'


async def generate_image_with_gemini(prompt: str, image_bytes: Optional[bytes]=None, model: str='gemini-3.1-flash-image-preview', images_bytes: list=None, temperature: float=1.0, state_data: dict = None) -> Tuple[Optional[bytes], Optional[str]]:
    keys = await load_keys()
    if not keys:
        return (None, 'Нет доступных API ключей.')
    all_images = images_bytes if images_bytes else [image_bytes] if image_bytes else []
    key_errors = []
    for idx, key in enumerate(keys.copy()):
        if state_data:
            state_data['status'] = f'Пробую ключ {idx+1}/{len(keys)} (Gemini)'
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
        parts = []
        for img in all_images:
            if img:
                parts.append({'inlineData': {'mimeType': _guess_image_mime(img), 'data': base64.b64encode(img).decode('utf-8')}})
        if len(all_images) > 1:
            multi_ref = f'The {len(all_images)} photos above are ALL different photos of the SAME subject/person. Treat every photo as a reference of the exact same individual — same face, same identity. Do NOT blend different people. '
            effective_prompt = multi_ref + (prompt if prompt else 'Generate a high-quality image of this person.')
        else:
            effective_prompt = prompt if prompt else 'A highly detailed beautiful picture'
        parts.append({'text': effective_prompt})
        payload = {'contents': [{'parts': parts}], 'generationConfig': {'temperature': temperature, 'responseModalities': ['TEXT', 'IMAGE']}}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=GEMINI_IMAGE_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            for part in data['candidates'][0]['content']['parts']:
                                inline = part.get('inlineData') or part.get('inline_data')
                                if inline and inline.get('data'):
                                    img_bytes = base64.b64decode(inline['data'])
                                    return (img_bytes, None)
                        except (KeyError, IndexError, TypeError):
                            pass
                        try:
                            error_details = json.dumps(data, ensure_ascii=False)
                            return (None, f'Нейросеть не вернула изображение (возможно, запрос заблокирован цензурой).\n\n> Ответ API:\n> {error_details}')
                        except Exception as e:
                            return (None, f'Не удалось разобрать ответ Gemini: {type(e).__name__}: {e}')
                    elif resp.status in [429, 403]:
                        resp_text = await resp.text()
                        logging.warning(f'Ошибка ключа (фото) {key[:10]}... Код: {resp.status}. Текст: {resp_text}')
                        key_errors.append(f'{resp.status} {_http_explain(resp.status)}')
                        remove_key(key, resp.status)
                        continue
                    elif resp.status == 400:
                        resp_text = await resp.text()
                        logging.warning(f'Bad request (фото) {key[:10]}... Текст: {resp_text}')
                        return (None, f'Запрос отклонён API (400): {resp_text}')
                    elif resp.status in [500, 502, 503, 504]:
                        resp_text = await resp.text()
                        logging.warning(f'Gemini временно недоступен (фото) {key[:10]}... Код: {resp.status}. Пробую следующий ключ.')
                        key_errors.append(f'{resp.status} {_http_explain(resp.status)}')
                        continue
                    else:
                        resp_text = await resp.text()
                        return (None, f'Неизвестная ошибка API: {resp.status} - {resp_text}')
            except Exception as e:
                logging.error(f'Сетевая ошибка: {e}')
                key_errors.append(f'{type(e).__name__}: {e}')
                continue
    details = '\n'.join(key_errors[:5]) if key_errors else 'нет деталей'
    return (None, f'Все {len(keys)} ключей исчерпаны.\n\nОшибки по ключам:\n{details}')


async def fetch_gemini_image_models() -> list:
    from ai_services import _models_cache, _MODELS_CACHE_TTL, _pretty_model_name
    cache_key = 'gemini_image'
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
                        methods = m.get('supportedGenerationMethods', [])
                        if 'image' in model_id.lower() and 'generateContent' in methods:
                            result.append((_pretty_model_name(model_id), model_id))
                    _models_cache[cache_key] = {'ts': now, 'data': result}
                    return result
        except Exception as e:
            logging.warning(f'fetch_gemini_image_models: {e}')
    return []


async def generate_image_prompt(prompt: str, images_bytes: list, prev_prompts: list=None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    keys = await load_keys()
    if not keys:
        return (None, None, 'Нет ключей')
    key = keys[0]
    prev_prompts = prev_prompts or []
    sys_text = "You are a world-class AI image generation prompt engineer specializing in Midjourney, DALL-E, Flux, and Stable Diffusion. You analyze reference photo(s) and the user's rough idea, then craft the perfect detailed generation prompt. Include: subject description, art style, lighting, composition, mood, quality tags (masterpiece, 8k, highly detailed, etc.). Respond ONLY in this exact format — nothing else:\nENGLISH: <your detailed English prompt>\nRUSSIAN: <Russian translation>"
    photo_count = len([b for b in images_bytes if b])
    prev_note = ''
    if prev_prompts:
        prev_note = ' Previously generated (make a DIFFERENT one): ' + ' | '.join((f'"{p}"' for p in prev_prompts[-3:]))
    user_text = f'''{('Two' if photo_count > 1 else 'One')} reference photo{('s are' if photo_count > 1 else ' is')} attached. User's idea: "{prompt}".{prev_note} Generate the optimal image generation prompt.'''
    parts = []
    for img in images_bytes:
        if img:
            parts.append({'inlineData': {'mimeType': 'image/jpeg', 'data': base64.b64encode(img).decode()}})
    parts.append({'text': user_text})
    for model_name in ['gemini-3.5-flash', 'gemini-3.1-pro-preview', 'gemini-3.1-flash-preview', 'gemini-3.1-flash-lite-preview']:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}'
        payload = {'systemInstruction': {'parts': [{'text': sys_text}]}, 'contents': [{'parts': parts}], 'generationConfig': {'temperature': 0.95}}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                    if resp.status == 404:
                        continue
                    if resp.status == 200:
                        data = await resp.json()
                        raw = data['candidates'][0]['content']['parts'][0]['text'].strip()
                        (english, russian) = ('', '')
                        for line in raw.split('\n'):
                            if line.upper().startswith('ENGLISH:'):
                                english = line[8:].strip()
                            elif line.upper().startswith('RUSSIAN:'):
                                russian = line[8:].strip()
                        if not english:
                            english = raw.split('\n')[0][:300]
                        return (english, russian, None)
                    err = await resp.text()
                    return (None, None, f'API {resp.status}: {err[:150]}')
            except Exception as e:
                return (None, None, str(e))
    return (None, None, 'Все модели недоступны')
