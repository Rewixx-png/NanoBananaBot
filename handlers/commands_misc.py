import asyncio
import json
import logging
import re
import uuid

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import FULL_ACCESS_CHAT_ID
from dual_bot import BOT1_DUAL_NAME, BOT2_DUAL_NAME, start_dual, stop_dual
from services.upscale_service import MODE_LABELS, upscale_image
from utils import check_membership, is_banned

from services.gemini_text import generate_text_with_gemini
from handlers.common import _track_user, safe_send

logger = logging.getLogger(__name__)
commands_misc_router = Router()

_upscale_pending: dict[str, dict] = {}


def _upscale_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Аниме', callback_data=f'upscale:anime:{request_id}'),
            InlineKeyboardButton(text='Реалистичное фото', callback_data=f'upscale:photo:{request_id}'),
        ],
        [InlineKeyboardButton(text='Отмена', callback_data=f'upscale:cancel:{request_id}')],
    ])


def _drop_upscale_pending(uid: int) -> None:
    for request_id, item in list(_upscale_pending.items()):
        if item.get('user_id') == uid:
            _upscale_pending.pop(request_id, None)


@commands_misc_router.message(Command("up"))
async def cmd_up(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply('Нет доступа. Сначала вступи в обязательную беседу.')
        return
    if not message.photo:
        await message.reply('Фото не прикреплено. Отправь одно фото с подписью /up.')
        return
    wait_msg = await message.reply('Скачиваю исходное фото...')
    try:
        photo = message.photo[-1]
        downloaded = await message.bot.download(photo.file_id)
        image_bytes = downloaded.read()
    except Exception as e:
        logger.error(f'/up download failed: {type(e).__name__}: {e}', exc_info=True)
        await wait_msg.edit_text('Не удалось скачать фото. Пришли JPG или PNG и повтори /up.')
        return

    _drop_upscale_pending(message.from_user.id)
    request_id = uuid.uuid4().hex[:10]
    thread_id = message.message_thread_id if getattr(message.chat, 'is_forum', False) else None
    _upscale_pending[request_id] = {
        'user_id': message.from_user.id,
        'chat_id': message.chat.id,
        'source_message_id': message.message_id,
        'message_id': wait_msg.message_id,
        'image_bytes': image_bytes,
        'message_thread_id': thread_id,
    }
    await wait_msg.edit_text(
        "<b>Как улучшить фото?</b>\n"
        "Выбери режим. Локально, 2x, без сжатия. Качественные модели, дольше. Фото до 2048 px.\n\n"
        "Аниме: рисунки, иллюстрации, скриншоты.\n"
        "Реалистичное: обычные фото.",
        reply_markup=_upscale_keyboard(request_id),
        parse_mode='HTML',
    )


@commands_misc_router.callback_query(F.data.startswith('upscale:'))
async def handle_upscale_choice(callback: types.CallbackQuery):
    parts = callback.data.split(':', 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, mode, request_id = parts
    item = _upscale_pending.pop(request_id, None)
    if not item or callback.from_user.id != item['user_id']:
        await callback.answer('Запрос устарел. Отправь /up заново.', show_alert=True)
        return
    if mode == 'cancel':
        await callback.answer('Отменено.')
        try:
            await callback.message.edit_text('Апскейл отменён.')
        except Exception:
            pass
        return
    if mode not in MODE_LABELS:
        await callback.answer('Неизвестный режим.', show_alert=True)
        return

    await callback.answer('Улучшаю...')
    label = MODE_LABELS[mode]
    try:
        await callback.message.edit_text(
            f'Улучшаю фото локально: {label}. Качественная модель, может занять несколько минут...'
        )
    except Exception:
        pass

    try:
        upscaled, error = await asyncio.wait_for(
            upscale_image(item['image_bytes'], mode=mode), timeout=600
        )
    except asyncio.TimeoutError:
        upscaled, error = None, 'Локальный апскейлер не успел за 600 секунд.'
    if not upscaled:
        logger.error(f'/up upscale failed ({mode}): {error}')
        try:
            await callback.message.edit_text(
                f'Не удалось улучшить фото.\n\n{error}'
            )
        except Exception:
            pass
        return

    kwargs = {}
    if item.get('message_thread_id'):
        kwargs['message_thread_id'] = item['message_thread_id']
    if item.get('source_message_id'):
        kwargs['reply_to_message_id'] = item['source_message_id']
    try:
        await callback.message.bot.send_document(
            chat_id=item['chat_id'],
            document=BufferedInputFile(upscaled, filename='upscaled.png'),
            caption=f'Готово. {label}, 2x, без сжатия.',
            **kwargs,
        )
    except Exception as e:
        logger.error(f'/up send failed: {type(e).__name__}: {e}', exc_info=True)
        try:
            await callback.message.edit_text(
                'Не удалось отправить результат. Попробуй фото поменьше.'
            )
        except Exception:
            pass
        return
    try:
        await callback.message.delete()
    except Exception as e:
        logger.warning(f'/up status cleanup failed after successful send: {type(e).__name__}: {e}')


@commands_misc_router.message(Command('figma'))
async def cmd_figma(message: types.Message):
    _track_user(message)
    uid = message.from_user.id
    if is_banned(uid):
        return
    prompt = (message.text or '').replace('/figma', '', 1).strip()
    if not prompt:
        await message.reply('Описание пустое. Напиши, что собрать: /figma экран плеера в тёмной теме')
        return
    thread_id = message.message_thread_id if message.chat.is_forum else None
    reply_kwargs = {'message_thread_id': thread_id} if thread_id else {}
    thinking_msg = await message.reply('Генерирую дизайн в Figma...', **reply_kwargs)
    try:
        spec_prompt = (
            'Ты — профессиональный UI/UX дизайнер. Сгенерируй JSON-спецификацию дизайна для Figma Plugin API.\n'
            'ВЕРНИ ТОЛЬКО JSON БЕЗ ОБЪЯСНЕНИЙ И БЕЗ MARKDOWN-БЛОКОВ.\n\n'
            'Формат JSON:\n'
            '{\n'
            '  "frame": {\n'
            '    "name": "Design",\n'
            '    "width": 1280,\n'
            '    "height": 720,\n'
            '    "backgroundColor": {"r": 1, "g": 1, "b": 1}\n'
            '  },\n'
            '  "nodes": [\n'
            '    {"type": "RECTANGLE", "name": "bg", "x": 0, "y": 0, "width": 1280, "height": 720,\n'
            '     "fill": {"r": 0.1, "g": 0.1, "b": 0.9}, "cornerRadius": 0},\n'
            '    {"type": "TEXT", "name": "title", "x": 100, "y": 200, "width": 600,\n'
            '     "content": "Заголовок", "fontSize": 48, "fontStyle": "Bold",\n'
            '     "color": {"r": 1, "g": 1, "b": 1}},\n'
            '    {"type": "ELLIPSE", "name": "circle", "x": 900, "y": 300, "width": 200, "height": 200,\n'
            '     "fill": {"r": 1, "g": 0.5, "b": 0}}\n'
            '  ]\n'
            '}\n\n'
            'Доступные типы нод: RECTANGLE, ELLIPSE, TEXT, LINE.\n'
            'Цвета — float от 0 до 1. fontSize — число. fontStyle: "Regular" или "Bold".\n'
            'Сделай красивый, насыщенный дизайн с минимум 6-10 нодами, используй градиентные блоки, типографику, геометрию.\n'
            f'Описание дизайна: {prompt}'
        )
        raw = await asyncio.wait_for(
            generate_text_with_gemini(spec_prompt, message.chat.id, username='figma_gen', web_query=None),
            timeout=60
        )
        json_match = re.search(r'```(?:json)?\s*\n([\s\S]*?)```', raw)
        if json_match:
            raw = json_match.group(1).strip()
        else:
            brace = raw.find('{')
            if brace >= 0:
                raw = raw[brace:]
        spec = json.loads(raw)

        from figma_bridge import enqueue_and_wait
        session_id = uuid.uuid4().hex
        await thinking_msg.edit_text('Жду пока плагин создаст дизайн в Figma...')
        node_id = await enqueue_and_wait(session_id, spec, timeout=120.0)

        if node_id is None:
            try:
                await thinking_msg.edit_text(
                    'Плагин не ответил за 2 минуты.\n'
                    'Убедись что плагин NanoHatani Bridge запущен в Figma и файл открыт.'
                )
            except Exception as e:
                logger.warning(f'Ошибка отображения тайм-аута Figma: {e}')
            return

        from config import FIGMA_TOKEN
        import aiohttp as _aiohttp
        file_key = spec.get('file_key', '')
        node_id_enc = node_id.replace(':', '-').replace(';', '-')
        render_url = f'https://api.figma.com/v1/images/{file_key}?ids={node_id}&format=png&scale=2' if file_key else None

        png_bytes = None
        if render_url:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(render_url, headers={'X-Figma-Token': FIGMA_TOKEN}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        img_url = (data.get('images') or {}).get(node_id) or (data.get('images') or {}).get(node_id.replace(':', '-'))
                        if img_url:
                            async with sess.get(img_url) as img_resp:
                                if img_resp.status == 200:
                                    png_bytes = await img_resp.read()

        try:
            await thinking_msg.delete()
        except Exception as e:
            logger.warning(f'Ошибка удаления thinking_msg в figma: {e}')

        if png_bytes:
            doc = BufferedInputFile(png_bytes, filename=f'figma_{uuid.uuid4().hex[:6]}.png')
            await safe_send(
                message.bot.send_document,
                chat_id=message.chat.id,
                document=doc,
                caption=f'{prompt[:180]}',
                reply_to_message_id=message.message_id,
                **reply_kwargs
            )
        else:
            await safe_send(
                message.bot.send_message,
                chat_id=message.chat.id,
                text=f'Дизайн создан в Figma (node `{node_id}`). Рендер недоступен без file_key.',
                reply_to_message_id=message.message_id,
                **reply_kwargs
            )
        logger.info(f'cmd_figma: done session={session_id} node_id={node_id} uid={uid}')
    except asyncio.TimeoutError:
        try:
            await thinking_msg.edit_text('Тайм-аут, Gemini тупит. Попробуй ещё раз.')
        except Exception as e:
            logger.warning(f'Ошибка отображения тайм-аута Figma (TimeoutError): {e}')
    except Exception as _figma_err:
        logger.exception(f'cmd_figma error: {_figma_err}')
        try:
            await thinking_msg.edit_text(f'Упало: {type(_figma_err).__name__}. Попробуй ещё раз.')
        except Exception as e:
            logger.warning(f'Ошибка отображения ошибки Figma: {e}')


@commands_misc_router.message(Command("dual"))
async def cmd_dual(message: Message):
    if message.chat.id != FULL_ACCESS_CHAT_ID:
        await message.reply('Команда /dual работает только в специальной беседе с полным доступом. Здесь используй обычный чат или /help.')
        return
    from dual_bot import bot2_id
    if not bot2_id:
        await message.reply("Второй бот (@HatabiAiibot) сейчас не подключён или токен не валиден.")
        return
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    started = start_dual(chat_id, thread_id)
    if started:
        await message.reply(f"{BOT1_DUAL_NAME} vs {BOT2_DUAL_NAME}. /stopdual чтобы остановить.")
    else:
        await message.reply("Уже идёт.")


@commands_misc_router.message(Command("stopdual"))
async def cmd_stopdual(message: Message):
    stopped = stop_dual(message.chat.id)
    if stopped:
        await message.reply("Заткнулись.")
    else:
        await message.reply("Никто не говорит.")
