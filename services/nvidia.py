import asyncio
import base64
import json
import logging
import aiohttp
from typing import Tuple, Optional

from config import NVIDIA_TIMEOUT
from keys import load_nvidia_keys, remove_key
from services.gemini_text import translate_to_english

logger = logging.getLogger(__name__)




async def generate_image_with_nvidia(
    prompt: str,
    model: str = 'black-forest-labs/flux.1-schnell',
    state_data: dict = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Generate an image via NVIDIA NIM (FLUX models)."""
    api_keys = load_nvidia_keys()
    if not api_keys:
        return (None, 'Нет ключей NVIDIA NIM. Добавьте nvapi-... ключи в r.txt.')
    prompt_text = await translate_to_english(prompt) if prompt else 'A highly detailed beautiful picture'
    url = f'https://ai.api.nvidia.com/v1/genai/{model}'
    if 'schnell' in model:
        (steps, cfg_scale) = (4, 0)
    elif 'klein' in model:
        (steps, cfg_scale) = (8, 2.0)
    else:
        (steps, cfg_scale) = (30, 3.5)
    payload = {'prompt': prompt_text, 'width': 1024, 'height': 1024, 'steps': steps, 'seed': 0, 'cfg_scale': cfg_scale}
    request_timeout = aiohttp.ClientTimeout(total=NVIDIA_TIMEOUT)
    last_error = None
    for idx, api_key in enumerate(api_keys):
        if state_data:
            state_data['status'] = f'Пробую ключ {idx+1}/{len(api_keys)} (NVIDIA)'
        headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json', 'Content-Type': 'application/json'}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers, timeout=request_timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        artifacts = data.get('artifacts', [])
                        if artifacts and artifacts[0].get('base64'):
                            return (base64.b64decode(artifacts[0]['base64']), None)
                        return (None, f'NVIDIA не вернул изображение. Ответ: {json.dumps(data, ensure_ascii=False)[:300]}')
                    resp_text = await resp.text()
                    last_error = f'Ошибка NVIDIA NIM ({resp.status}): {resp_text[:300]}'
                    logging.warning(f'NVIDIA NIM {resp.status} на ключе {api_key[:12]}..., пробую следующий.')
                    if resp.status in [401, 403]:
                        remove_key(api_key, resp.status)
                    continue
            except asyncio.TimeoutError:
                return (None, f'Таймаут NVIDIA NIM: модель не ответила за {NVIDIA_TIMEOUT} секунд.')
            except aiohttp.ClientError as e:
                return (None, f'Сетевая ошибка NVIDIA NIM: {type(e).__name__}: {e}')
            except Exception as e:
                logger.exception('Неожиданная ошибка NVIDIA NIM')
                return (None, f'Ошибка NVIDIA NIM: {type(e).__name__}: {e}')
    return (None, last_error)
