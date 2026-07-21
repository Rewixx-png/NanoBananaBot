import base64
import json
import logging
import aiohttp
from typing import Tuple, Optional

from config import GEMINI_IMAGE_TIMEOUT
from keys import load_keys
from shared_types import (
    _guess_image_mime, _build_text_system_prompt,
    _gemini_url, _gemini_headers, gemini_post, gemini_text_of,
)




# ── Gemini image service ──────────────────────────────────────────────────









async def analyze_photo_with_gemini(image_bytes: bytes, prompt: str) -> str:
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
    data, _key, err = await gemini_post("models/gemini-3.5-flash:generateContent", payload, timeout=30.0)
    if data is not None:
        try:
            return data['candidates'][0]['content']['parts'][0]['text']
        except KeyError:
            return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
    if err:
        _el = err.lower()
        if '400' in _el and any(w in _el for w in ('safety', 'prohibited', 'harm', 'block', 'policy', 'recitation')):
            return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
    return 'Все ключи проебаны или сдохли, отъебись.'


async def generate_image_with_gemini(prompt: str, image_bytes: Optional[bytes]=None, model: str='gemini-3.1-flash-image-preview', images_bytes: list=None, temperature: float=1.0, state_data: dict = None) -> Tuple[Optional[bytes], Optional[str]]:
    all_images = images_bytes if images_bytes else [image_bytes] if image_bytes else []
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
    if state_data:
        state_data['status'] = 'Генерирую изображение (Gemini)...'
    data, _key, err = await gemini_post(f"models/{model}:generateContent", payload, timeout=GEMINI_IMAGE_TIMEOUT)
    if data is not None:
        try:
            for part in data['candidates'][0]['content']['parts']:
                inline = part.get('inlineData') or part.get('inline_data')
                if inline and inline.get('data'):
                    return (base64.b64decode(inline['data']), None)
        except (KeyError, IndexError, TypeError):
            pass
        try:
            error_details = json.dumps(data, ensure_ascii=False)
            return (None, f'Нейросеть не вернула изображение (возможно, запрос заблокирован цензурой).\n\n> Ответ API:\n> {error_details}')
        except Exception as e:
            return (None, f'Не удалось разобрать ответ Gemini: {type(e).__name__}: {e}')
    if err:
        _es = str(err)
        if _es.startswith('HTTP 400'):
            return (None, f'Запрос отклонён API (400): {_es[9:]}')
        return (None, f'Все ключи исчерпаны.\n\nОшибка: {_es[:300]}')
    return (None, 'Неизвестная ошибка генерации изображения.')


async def fetch_gemini_image_models() -> list:
    from shared_types import _models_cache, _MODELS_CACHE_TTL, _pretty_model_name
    cache_key = 'gemini_image'
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
                        methods = m.get('supportedGenerationMethods', [])
                        if 'image' in model_id.lower() and 'generateContent' in methods:
                            result.append((_pretty_model_name(model_id), model_id))
                    _models_cache[cache_key] = {'ts': now, 'data': result}
                    return result
        except Exception as e:
            logging.warning(f'fetch_gemini_image_models: {e}')
    return []


async def generate_image_prompt(prompt: str, images_bytes: list, prev_prompts: list=None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
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
        payload = {'systemInstruction': {'parts': [{'text': sys_text}]}, 'contents': [{'parts': parts}], 'generationConfig': {'temperature': 0.95}}
        data, _key, err = await gemini_post(f"models/{model_name}:generateContent", payload, timeout=35.0)
        if data is not None:
            raw = gemini_text_of(data).strip()
            if raw:
                english, russian = '', ''
                for line in raw.split('\n'):
                    if line.upper().startswith('ENGLISH:'):
                        english = line[8:].strip()
                    elif line.upper().startswith('RUSSIAN:'):
                        russian = line[8:].strip()
                if not english:
                    english = raw.split('\n')[0][:300]
                return (english, russian, None)
        if err and '404' in str(err):
            continue
        if err:
            return (None, None, str(err))
    return (None, None, 'Все модели недоступны')
