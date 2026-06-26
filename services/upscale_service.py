import logging
import aiohttp
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

_UPSCALE_CLIENT_ID = 'b4f2e8a1c6d9f3b0e7a2c5d8f1b4e7a0'

_UPSCALE_BASE = 'https://image-upscaling.net'


async def _upscale_imageupscaling(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    cookies = {'client_id': _UPSCALE_CLIENT_ID}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(cookies=cookies) as session:
        form = aiohttp.FormData()
        form.add_field('scale', '2')
        form.add_field('model', 'plus')
        form.add_field('image', image_bytes, filename='image.jpg', content_type='image/jpeg')
        try:
            async with session.post(f'{_UPSCALE_BASE}/upscaling_upload', data=form, timeout=timeout) as resp:
                if resp.status != 200:
                    return (None, await resp.text())
                original_filename = (await resp.text()).strip()
        except Exception as e:
            return (None, str(e))
        dl_timeout = aiohttp.ClientTimeout(total=15)
        for _ in range(20):
            await asyncio.sleep(4)
            try:
                async with session.get(f'{_UPSCALE_BASE}/upscaling_get_status_v2', timeout=dl_timeout) as resp:
                    if resp.status != 200:
                        continue
                    items = await resp.json()
                    for item in items:
                        if item.get('original_filename') == original_filename and item.get('completed'):
                            async with session.get(item['image_url'], timeout=dl_timeout) as dl:
                                if dl.status == 200:
                                    return (await dl.read(), None)
            except Exception:
                continue
    return (None, 'Upscale timeout')


async def _upscale_picwish(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        from picwish import PicWish
        pw = PicWish()
        result = await asyncio.wait_for(pw.enhance(image_bytes), timeout=60)
        data = await asyncio.wait_for(result.get_bytes(), timeout=30)
        return (data, None)
    except asyncio.TimeoutError:
        return (None, 'PicWish timeout')
    except Exception as e:
        return (None, str(e))


async def upscale_image(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    (result, err) = await _upscale_imageupscaling(image_bytes)
    if result:
        return (result, None)
    logging.warning(f'image-upscaling.net failed ({err}), trying PicWish')
    (result, err2) = await _upscale_picwish(image_bytes)
    if result:
        return (result, None)
    return (None, f'Все апскейлеры недоступны. upscaling.net: {err} | picwish: {err2}')

