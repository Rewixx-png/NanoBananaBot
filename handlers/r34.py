"""Handler for /r34 command — fetches art via agent's image search."""
import asyncio
import logging
from aiogram import types
from aiogram.filters import Command

from handlers.media_gen import media_router
from agent import run_agent
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

    prompt = (
        f'Ты — поисковик артов. Найди {count} картинок по тегу «{tag}». '
        f'Используй web_search с запросом: site:rule34.xxx OR site:gelbooru.com OR site:danbooru.donmai.us OR site:konachan.com OR site:yande.re OR site:e621.net {tag}. '
        f'Потом через download_image скачай КАЖДУЮ картинку и отправь через send_photo. '
        f'В подписи укажи источник. НЕ юзай reply пока все картинки не отправлены. '
        f'Если не нашлось — скажи об этом в reply.'
    )

    username = message.from_user.username or message.from_user.first_name or 'anon'
    wait_msg = await safe_send(message.reply, f'🔞 Ищу {count} артов «{tag}» через 30+ источников...')

    async def _st(status: str):
        try:
            await wait_msg.edit_text(f'🔞 {status}')
        except Exception:
            pass

    async def _send(media: dict):
        pass  # agent handles sending itself via send_photo tool

    try:
        result, _ = await asyncio.wait_for(
            run_agent(prompt, message.chat.id, username, _st, _send, is_owner=False),
            timeout=120,
        )
    except asyncio.TimeoutError:
        await wait_msg.edit_text(f'⏰ Поиск «{tag}» занял больше 2 минут. Попробуй ещё раз.')
        return
    except Exception as e:
        logger.exception(f'/r34 agent failed: {e}')
        await wait_msg.edit_text(f'❌ Ошибка поиска: {e}')
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass
