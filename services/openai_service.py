import asyncio
import base64
import json
import logging
import aiohttp
from typing import Tuple, Optional, List, Dict, Any

from config import OPENAI_TIMEOUT
from keys import load_openai_keys, remove_key
from services.openrouter import generate_image_with_openrouter



async def parse_openai_image_response(resp) -> Tuple[Optional[bytes], Optional[str]]:
    """Parse OpenAI image API response — supports b64_json and URL formats."""
    resp_text = await resp.text()
    if resp.status != 200:
        try:
            err_data = json.loads(resp_text)
            err_msg = err_data.get('error', {}).get('message', resp_text)
        except Exception:
            err_msg = resp_text
        return (None, f'Ошибка OpenAI API ({resp.status}): {err_msg}')
    try:
        data = json.loads(resp_text)
        image_item = data['data'][0]
        if image_item.get('b64_json'):
            return (base64.b64decode(image_item['b64_json']), None)
        image_url = image_item.get('url')
        if image_url:
            async with aiohttp.ClientSession() as dl_session:
                async with dl_session.get(image_url, timeout=120) as img_resp:
                    if img_resp.status == 200:
                        return (await img_resp.read(), None)
                    return (None, f'OpenAI вернул URL, но скачивание не удалось ({img_resp.status}).')
        return (None, f'OpenAI не вернул изображение в ответе.\n\n> Ответ API:\n> {json.dumps(data, ensure_ascii=False)}')
    except Exception as e:
        return (None, f'Не удалось разобрать ответ OpenAI: {e}\n\n> Сырой ответ API:\n> {resp_text}')


async def generate_image_with_gpt(
    prompt: str,
    image_bytes: Optional[bytes] = None,
    model: str = 'gpt-image-2',
    images_bytes: Optional[List[bytes]] = None,
    state_data: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Generate an image via OpenAI with dynamic model discovery from API key — no OpenRouter fallback."""
    if model.startswith('openai/'):
        logging.info(f'Модель {model} — OpenRouter, пропускаю OpenAI ключи.')
        if state_data:
            state_data['status'] = 'Генерирую через OpenRouter...'
        return await generate_image_with_openrouter(prompt, model=model, images_bytes=images_bytes, state_data=state_data)
    all_ref_images = images_bytes if images_bytes else [image_bytes] if image_bytes else []
    logging.info(f"generate_image_with_gpt: model={model}, ref_images={len(all_ref_images)}, prompt_len={len(prompt)}")
    api_keys = await load_openai_keys()
    if all_ref_images:
        api_keys = sorted(api_keys, key=lambda k: (k.startswith('sk-proj-'), k))
    prompt_text = prompt if prompt else 'A highly detailed beautiful picture'
    request_timeout = aiohttp.ClientTimeout(total=360 if all_ref_images else OPENAI_TIMEOUT)
    last_error = None
    for idx, api_key in enumerate(api_keys):
        if state_data:
            state_data['status'] = f'Пробую ключ {idx+1}/{len(api_keys)} (OpenAI)'
        headers = {'Authorization': f'Bearer {api_key}'}
        async with aiohttp.ClientSession() as session:
            try:
                if all_ref_images and (not model.startswith('dall-e')):
                    _edit_model = model.split('-202')[0] if '-202' in model else model
                    form = aiohttp.FormData()
                    form.add_field('model', _edit_model)
                    form.add_field('prompt', prompt_text)
                    form.add_field('quality', 'high')
                    if len(all_ref_images) == 1:
                        form.add_field('image', all_ref_images[0], filename='image.jpg', content_type='image/jpeg')
                    else:
                        for img in all_ref_images:
                            form.add_field('image[]', img, filename='input.jpg', content_type='image/jpeg')
                    async with session.post('https://api.openai.com/v1/images/edits', data=form, headers=headers, timeout=request_timeout) as resp:
                        logging.info(f"edits endpoint status={resp.status}, model={model}, ref_images={len(all_ref_images)}, key_prefix={api_key[:16]}")
                        if resp.status != 200:
                            err_body = await resp.text()
                            logging.warning(f"edits error: {err_body[:300]}")
                        (result, error) = await parse_openai_image_response(resp)
                else:
                    payload = {'model': model, 'prompt': prompt_text}
                    async with session.post('https://api.openai.com/v1/images/generations', json=payload, headers={**headers, 'Content-Type': 'application/json'}, timeout=request_timeout) as resp:
                        (result, error) = await parse_openai_image_response(resp)
                if result:
                    return (result, None)
                last_error = error
                if error:
                    lowered_err = error.lower()
                    if 'safety' in lowered_err or 'rejected by' in lowered_err or 'censorship' in lowered_err or 'moderation' in lowered_err:
                        logging.warning(f'Запрос заблокирован цензурой OpenAI на ключе {api_key[:12]}. Прерываю цикл.')
                        return (None, error)
                    elif 'access to model' in lowered_err and model != 'gpt-image-1.5':
                        logging.info(f'Ключ {api_key[:12]} нет доступа к {model}')
                        if all_ref_images:
                            try:
                                fb_form = aiohttp.FormData()
                                fb_form.add_field('model', 'gpt-image-2')
                                fb_form.add_field('prompt', prompt_text)
                                fb_form.add_field('quality', 'high')
                                fb_form.add_field('image', all_ref_images[0], filename='image.jpg', content_type='image/jpeg')
                                async with session.post('https://api.openai.com/v1/images/edits', data=fb_form, headers=headers, timeout=request_timeout) as fb_resp:
                                    logging.info(f'edits fallback gpt-image-2 status={fb_resp.status}')
                                    (fb_result, fb_error) = await parse_openai_image_response(fb_resp)
                                    if fb_result:
                                        logging.info(f'edits gpt-image-2 сработал на ключе {api_key[:12]}')
                                        return (fb_result, None)
                                    last_error = fb_error
                            except Exception as _fe:
                                logging.warning(f'edits fallback failed: {_fe}')
                        else:
                            try:
                                fallback_payload = {'model': 'gpt-image-1.5', 'prompt': prompt_text}
                                async with session.post('https://api.openai.com/v1/images/generations', json=fallback_payload, headers={**headers, 'Content-Type': 'application/json'}, timeout=request_timeout) as fb_resp:
                                    (fb_result, fb_error) = await parse_openai_image_response(fb_resp)
                                    if fb_result:
                                        return (fb_result, None)
                                    last_error = fb_error
                            except Exception:
                                pass
                    elif '(429)' in error:
                        logging.warning(f'Rate limit (429) на ключе {api_key[:12]}..., кулдаун 65с.')
                        remove_key(api_key, 429)
                    elif 'billing' in lowered_err or 'quota' in lowered_err or 'hard limit' in lowered_err or '(401)' in error or 'unauthorized' in lowered_err or 'not active' in lowered_err:
                        logging.warning(f'Удаляю нерабочий OpenAI ключ {api_key[:12]}... Ошибка: {error}')
                        remove_key(api_key)
                    else:
                        logging.warning(f'Временная ошибка OpenAI на ключе {api_key[:12]}...: {error}')
                continue
            except asyncio.TimeoutError:
                logging.warning(f'Таймаут OpenAI на ключе {api_key[:12]}..., пробую следующий.')
                continue
            except aiohttp.ClientError as e:
                logging.warning(f'Сетевая ошибка OpenAI на ключе {api_key[:12]}...: {type(e).__name__}: {e}, пробую следующий.')
                continue
            except Exception as e:
                logging.warning(f'Неожиданная ошибка OpenAI на ключе {api_key[:12]}...: {type(e).__name__}: {e}, пробую следующий.')
                continue
    return (None, f'GPT недоступен: все OpenAI ключи не сработали. {last_error or "нет рабочих ключей"}')






async def fetch_openai_image_models() -> list:
    """Fetch available OpenAI image-generation models from API."""
    from shared_types import _models_cache, _MODELS_CACHE_TTL, _pretty_model_name
    cache_key = 'openai_image'
    now = __import__('time').time()
    if cache_key in _models_cache and now - _models_cache[cache_key]['ts'] < _MODELS_CACHE_TTL:
        return _models_cache[cache_key]['data']
    skip = {'dall-e-2', 'dall-e-3', 'gpt-image-1-mini'}
    result = []
    seen = set()
    api_keys = await load_openai_keys()
    for key in api_keys:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.openai.com/v1/models', headers={'Authorization': f'Bearer {key}'}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for m in sorted(data.get('data', []), key=lambda x: x['created'], reverse=True):
                        mid = m['id']
                        if any(p in mid for p in ('gpt-image', 'dall-e', 'chatgpt-image')) and mid not in skip and mid not in seen:
                            result.append((_pretty_model_name(mid), mid))
                            seen.add(mid)
                    if result:
                        break
        except Exception as e:
            logging.warning(f'fetch_openai_image_models key {key[:12]}: {e}')
            continue
    # Fallback: if no key returned models via API, use known defaults so the GPT button still appears
    if not result:
        defaults = [
            ('GPT-Image-2', 'gpt-image-2'),
            ('GPT-Image-2 (Apr)', 'gpt-image-2-2026-04-21'),
            ('GPT-Image-1.5', 'gpt-image-1.5'),
            ('GPT-Image-1', 'gpt-image-1'),
            ('ChatGPT Image', 'chatgpt-image-latest'),
        ]
        for label, mid in defaults:
            result.append((label, mid))
    _models_cache[cache_key] = {'ts': now, 'data': result}
    return result
