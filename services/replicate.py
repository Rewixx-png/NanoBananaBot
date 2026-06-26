import asyncio
import logging
import time
import aiohttp
from typing import Tuple, Optional

from keys import load_replicate_keys, remove_key

logger = logging.getLogger(__name__)

_DYNAMIC_REPLICATE_VERSIONS: dict = {}

_REPLICATE_MODELS = {
    'recraft-ai/recraft-v3': {'version': 'e06217725b21e2c059a09b3f44b4aef56574173aee9976c9726bdc0f474d7f46', 'cfg_key': 'guidance_scale', 'input': lambda p: {'prompt': p, 'size': '1024x1024'}},
    'black-forest-labs/flux-dev': {'version': '93d72f81bd019dde2bfcba9585a6f74e600b13a43a96eb01a42da54f5ab4df6a', 'cfg_key': 'guidance_scale', 'input': lambda p: {'prompt': p, 'width': 1024, 'height': 1024, 'steps': 28, 'guidance_scale': 3.5}},
    'black-forest-labs/flux-schnell': {'version': 'c846a69991daf4c0e5d016514849d14ee5b2e6846ce6b9d6f21369e564cfe51e', 'cfg_key': 'guidance_scale', 'input': lambda p: {'prompt': p, 'width': 1024, 'height': 1024, 'steps': 4}},
    'aisha-ai-official/wai-nsfw-illustrious-v12': {'version': '0fc0fa9885b284901a6f9c0b4d67701fd7647d157b88371427d63f8089ce140e', 'cfg_key': 'cfg_scale', 'input': lambda p: {'prompt': p, 'negative_prompt': 'lowres, bad anatomy, bad hands, text, error, missing fingers, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry', 'width': 896, 'height': 1152, 'steps': 28, 'cfg_scale': 7.0, 'scheduler': 'DPM++ 2M Karras'}},
    'aisha-ai-official/wai-nsfw-illustrious-v11': {'version': 'c1d5b02687df6081c7953c74bcc527858702e8c153c9382012ccc3906752d3ec', 'cfg_key': 'cfg_scale', 'input': lambda p: {'prompt': p, 'negative_prompt': 'lowres, bad anatomy, bad hands, text, error, worst quality, low quality', 'width': 896, 'height': 1152, 'steps': 28, 'cfg_scale': 7.0}},
    'aisha-ai-official/nsfw-flux-dev': {'version': 'fb4f086702d6a301ca32c170d926239324a7b7b2f0afc3d232a9c4be382dc3fa', 'cfg_key': 'guidance_scale', 'input': lambda p: {'prompt': p, 'width': 1024, 'height': 1024, 'steps': 28, 'guidance_scale': 3.5}}
}


async def fetch_replicate_image_models() -> list:
    """Fetch available Replicate models from the text-to-image collection."""
    from ai_services import _models_cache, _MODELS_CACHE_TTL
    cache_key = 'replicate_image'
    now = time.time()
    if cache_key in _models_cache and now - _models_cache[cache_key]['ts'] < _MODELS_CACHE_TTL:
        return _models_cache[cache_key]['data']
    keys = load_replicate_keys()
    if not keys:
        return []
    result = []
    try:
        url = 'https://api.replicate.com/v1/collections/text-to-image'
        headers = {'Authorization': f'Bearer {keys[0]}'}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get('models', []):
                        owner = m.get('owner')
                        name = m.get('name')
                        version_info = m.get('latest_version')
                        if owner and name and version_info and version_info.get('id'):
                            model_path = f'{owner}/{name}'
                            _DYNAMIC_REPLICATE_VERSIONS[model_path] = version_info['id']
                            label = f"{name} ({owner})"
                            result.append((label, model_path))
                    _models_cache[cache_key] = {'ts': now, 'data': result}
                    return result
    except Exception as e:
        logging.warning(f'fetch_replicate_image_models: {e}')
    return []


async def generate_image_with_replicate(
    prompt: str,
    model: str = 'aisha-ai-official/wai-nsfw-illustrious-v12',
    state_data: dict = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Generate an image via Replicate API (supports FLUX, WAI NSFW, etc.)."""
    keys = load_replicate_keys()
    if not keys:
        return (None, 'Нет Replicate ключей.')
    model_cfg = _REPLICATE_MODELS.get(model)
    if not model_cfg:
        dynamic_version = _DYNAMIC_REPLICATE_VERSIONS.get(model)
        if dynamic_version:
            model_cfg = {
                'version': dynamic_version,
                'input': lambda p: {'prompt': p}
            }
        else:
            return (None, f'Неизвестная Replicate модель: {model}')
    version = model_cfg['version']
    model_input = model_cfg['input'](prompt)
    for idx, key in enumerate(keys):
        if state_data:
            state_data['status'] = f'Пробую ключ {idx+1}/{len(keys)} (Replicate)'
        headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'Prefer': 'wait=60'}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post('https://api.replicate.com/v1/predictions', json={'version': version, 'input': model_input}, headers=headers, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    if resp.status not in (200, 201, 202):
                        err = await resp.text()
                        logging.warning(f'Replicate create {resp.status}: {err[:150]}')
                        if resp.status in (401, 403):
                            remove_key(key, resp.status)
                            continue
                        return (None, f'Replicate error {resp.status}: {err[:200]}')
                    prediction = await resp.json()
                pred_id = prediction.get('id')
                status = prediction.get('status')
                output = prediction.get('output')
                for _ in range(30):
                    if status == 'succeeded' and output:
                        break
                    if status == 'failed':
                        return (None, prediction.get('error', 'Replicate: generation failed'))
                    await asyncio.sleep(3)
                    async with session.get(f'https://api.replicate.com/v1/predictions/{pred_id}', headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as poll:
                        prediction = await poll.json()
                        status = prediction.get('status')
                        output = prediction.get('output')
                if not output:
                    return (None, 'Replicate: timeout')
                urls = output if isinstance(output, list) else [output]
                results = []
                for img_url in urls:
                    async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=30)) as dl:
                        if dl.status == 200:
                            results.append(await dl.read())
                if results:
                    return (results, None)
                return (None, 'Replicate download failed')
            except asyncio.TimeoutError:
                return (None, 'Replicate: timeout')
            except Exception as e:
                logging.error(f'Replicate error: {e}')
                return (None, str(e))
    return (None, 'Все Replicate ключи недоступны.')
