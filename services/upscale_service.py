import asyncio
import logging
import aiohttp
from typing import Tuple, Optional

from services.security_utils import is_safe_url
from config import UPSCALE_BASE_URL, UPSCALE_CLIENT_ID

logger = logging.getLogger(__name__)



async def _upscale_imageupscaling(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    if not UPSCALE_CLIENT_ID:
        return None, "UPSCALE_CLIENT_ID не задан в окружении. Добавьте его в .env."
    cookies = {'client_id': UPSCALE_CLIENT_ID}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(cookies=cookies) as session:
        form = aiohttp.FormData()
        form.add_field('scale', '2')
        form.add_field('model', 'plus')
        form.add_field('image', image_bytes, filename='image.jpg', content_type='image/jpeg')
        try:
            async with session.post(f'{UPSCALE_BASE_URL}/upscaling_upload', data=form, timeout=timeout) as resp:
                if resp.status != 200:
                    return (None, await resp.text())
                original_filename = (await resp.text()).strip()
        except Exception as e:
            return (None, str(e))
        dl_timeout = aiohttp.ClientTimeout(total=15)
        for _ in range(20):
            await asyncio.sleep(4)
            try:
                async with session.get(f'{UPSCALE_BASE_URL}/upscaling_get_status_v2', timeout=dl_timeout) as resp:
                    if resp.status != 200:
                        continue
                    items = await resp.json()
                    for item in items:
                        if item.get('original_filename') == original_filename and item.get('completed'):
                            if not is_safe_url(item['image_url']):
                                logger.warning(f'Upscale returned unsafe image URL, blocked: {str(item.get("image_url"))[:120]}')
                                continue
                            async with session.get(item['image_url'], timeout=dl_timeout) as dl:
                                if dl.status == 200:
                                    return (await dl.read(), None)
            except Exception:
                continue
    return (None, 'Upscale timeout')




async def upscale_image(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    return await _upscale_imageupscaling(image_bytes)

