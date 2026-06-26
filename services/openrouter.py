import asyncio
import base64
import json
import logging
import aiohttp
from typing import Tuple, Optional, List, Dict, Any

from keys import load_openrouter_keys, remove_key
from services.security_utils import is_safe_url

logger = logging.getLogger(__name__)


async def generate_image_with_openrouter(
    prompt: str,
    model: str = 'google/gemini-3.1-flash-image-preview',
    images_bytes: Optional[List[bytes]] = None,
    state_data: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Generate an image via OpenRouter API (multi-provider proxy with unified chat-completions interface)."""
    api_keys = await load_openrouter_keys()
    if not api_keys:
        return (None, 'Нет ключей OpenRouter. Добавьте sk-or-... ключи в r.txt.')
    prompt_text = prompt if prompt else 'A highly detailed beautiful picture'
    url = 'https://openrouter.ai/api/v1/chat/completions'
    modalities = ['image'] if 'flux' in model or 'seedream' in model or 'riverflow' in model else ['image', 'text']
    msg_content = [{'type': 'text', 'text': prompt_text}]
    if images_bytes:
        for img in images_bytes[:4]:
            b64 = base64.b64encode(img).decode()
            msg_content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}})
    payload = {'model': model, 'modalities': modalities, 'messages': [{'role': 'user', 'content': msg_content}]}
    request_timeout = aiohttp.ClientTimeout(total=300)
    last_error = None
    for idx, api_key in enumerate(api_keys):
        if state_data:
            state_data['status'] = f'Пробую ключ {idx+1}/{len(api_keys)} (OpenRouter)'
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers, timeout=request_timeout) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        raw_text = raw.decode('utf-8', errors='replace')
                        try:
                            import re as _re
                            b64_match = _re.search('data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', raw_text)
                            if b64_match:
                                return (base64.b64decode(b64_match.group(1)), None)
                        except Exception:
                            pass
                        try:
                            d = json.loads(raw_text)
                            msg = d.get('choices', [{}])[0].get('message', {})
                            for src in [msg.get('images', []), msg.get('content', []) or []]:
                                for part in src:
                                    if isinstance(part, dict) and part.get('type') == 'image_url':
                                        img_url = part.get('image_url', {}).get('url', '')
                                        if img_url.startswith('data:'):
                                            return (base64.b64decode(img_url.split(',', 1)[1]), None)
                                        if img_url.startswith('http'):
                                            if not is_safe_url(img_url):
                                                logging.warning(f'OpenRouter returned unsafe image URL, blocked: {img_url[:120]}')
                                                continue
                                            async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=60)) as img_resp:
                                                if img_resp.status == 200:
                                                    return (await img_resp.read(), None)
                        except Exception:
                            pass
                        return (None, 'OpenRouter не вернул изображение в ответе.')
                    err_text = await resp.text()
                    last_error = f'Ошибка OpenRouter ({resp.status}): {err_text[:200]}'
                    if resp.status in [401, 403]:
                        logging.warning(f'OpenRouter {resp.status} на ключе {api_key[:12]}..., пробую следующий.')
                        remove_key(api_key, resp.status)
                        continue
            except asyncio.TimeoutError:
                last_error = 'Таймаут OpenRouter'
                continue
            except Exception as e:
                last_error = str(e)
                continue
    return (None, last_error)
