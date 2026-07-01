"""Handler for /r34 command — fetches art from 16 booru sources via DuckDuckGo."""
import asyncio
import logging
import re
import aiohttp
from aiogram import types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

from handlers.media_gen import media_router
from services.r34_service import search_r34, download_image_bytes
from handlers.common import safe_send

logger = logging.getLogger(__name__)


@media_router.message(Command('r34'))
async def cmd_r34(message: types.Message):
    text = (message.text or '').replace('/r34', '').strip()
    if message.caption:
        text = message.caption.replace('/r34', '').strip()

    if not text:
        await message.reply(
            'Использование: /r34 <тег> <кол-во>\n'
            'Пример: /r34 hutao 5\n'
            'Кол-во: 1-8 (по умолчанию 3)'
        )
        return

    parts = text.rsplit(maxsplit=1)
    count = 3
    if len(parts) == 2 and parts[1].isdigit():
        tag = parts[0].strip()
        count = max(1, min(int(parts[1]), 8))
    else:
        tag = text.strip()
    tag = re.sub(r'[^a-zA-Z0-9_\- ]', '', tag)[:80]

    wait_msg = await safe_send(message.reply, f'🔞 Ищу {count} артов «{tag}» на 16 booru-источниках...')

    try:
        results = await asyncio.wait_for(search_r34(tag, count), timeout=25)
    except asyncio.TimeoutError:
        await wait_msg.edit_text(f'⏰ Поиск «{tag}» занял больше 25 секунд. Попробуй ещё раз.')
        return

    if not results:
        await wait_msg.edit_text(f'🔞 Ничего не нашёл по тегу «{tag}». Может, другой тег?')
        return

    await wait_msg.edit_text(f'🔞 Нашёл {len(results)}/{count} артов «{tag}», скачиваю...')

    async with aiohttp.ClientSession() as session:
        sent = 0
        for source, url in results:
            img_bytes = await download_image_bytes(session, url)
            if not img_bytes:
                continue
            try:
                ext = url.rsplit('.', 1)[-1].split('?')[0][:4] or 'jpg'
                filename = f'r34_{tag}_{sent+1}.{ext}'
                caption = f'🔞 {tag} #{sent+1} — {source}' if sent == 0 else f'🔞 #{sent+1} — {source}'
                await safe_send(
                    message.reply_document,
                    document=BufferedInputFile(img_bytes, filename=filename),
                    caption=caption[:1024],
                )
                sent += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f'Failed to send r34 image: {e}')

    try:
        await wait_msg.delete()
    except Exception:
        pass
    if sent == 0:
        await safe_send(message.reply, f'🔞 Нашлись ссылки, но не смог скачать ни одной картинки «{tag}».')
