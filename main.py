import asyncio
import logging
import signal
import sys
from aiogram import Bot, Dispatcher, BaseMiddleware


from aiogram.types import TelegramObject, Message, CallbackQuery
from config import BOT_TOKEN, BANNED_USER_IDS
from database import init_db, get_all_pending_gens, delete_pending_gen, get_banned_users_db, get_all_chat_limits, get_all_vip_users, get_all_daily_limits_usage
from handlers import router, refresh_models
from handlers.core import PUBLIC_COMMANDS
from handlers.common import callback_has_access
from state import banned_user_ids, chat_custom_limits, paid_unlimited_until, daily_gen_limits
from typing import Callable, Any, Awaitable

class BanMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]], event: TelegramObject, data: dict) -> Any:
        user_id = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
        if user_id and (user_id in BANNED_USER_IDS or user_id in banned_user_ids):
            return
        return await handler(event, data)

class CallbackAccessMiddleware(BaseMiddleware):
    _SAFE_EXACT = {
        "menu:home", "menu:close", "menu:chat", "menu:create", "menu:tools", "voice:cancel",
    }
    _SAFE_PREFIXES = ("help:", "guide:")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable[Any]],
        event: CallbackQuery,
        data: dict,
    ) -> Any:
        callback_data = event.data or ""
        if callback_data in self._SAFE_EXACT or callback_data.startswith(self._SAFE_PREFIXES):
            return await handler(event, data)
        if not await callback_has_access(event):
            return None
        return await handler(event, data)

class DualHistoryMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]], event: TelegramObject, data: dict) -> Any:
        if isinstance(event, Message) and event.from_user and not event.from_user.is_bot:
            from dual_bot import add_dual_message
            from state import chat_context_buffer
            from config import MAX_HISTORY_MESSAGES
            user = event.from_user
            name = f"@{user.username}" if user.username else (user.first_name or str(user.id))
            buf = chat_context_buffer.setdefault(event.chat.id, [])
            import re as _re
            def _smeta(s: str, n: int = 80) -> str:
                return _re.sub(r'[\[\]{}<>]', '', str(s))[:n]

            if event.text:
                add_dual_message(event.chat.id, name, event.text)
                buf.append(f"{name}: {event.text}")
            elif event.photo:
                cap = _smeta(event.caption or '')
                buf.append(f"{name}: [фото]{': ' + cap if cap else ''}")
            elif event.audio or event.voice or event.document:
                media = event.audio or event.voice or event.document
                fname = _smeta(getattr(media, 'file_name', '') or getattr(media, 'mime_type', 'файл'))
                cap = _smeta(event.caption or '')
                buf.append(f"{name}: [аудио/файл: {fname}]{': ' + cap if cap else ''}")
            elif event.video or event.video_note:
                cap = _smeta(event.caption or '')
                buf.append(f"{name}: [видео]{': ' + cap if cap else ''}")
            if len(buf) > MAX_HISTORY_MESSAGES:
                chat_context_buffer[event.chat.id] = buf[-MAX_HISTORY_MESSAGES:]
        return await handler(event, data)

if not logging.root.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.FileHandler('bot.log'), logging.StreamHandler(sys.stdout)])
# Apply API key masking to file handler
from log_filter import APIKeyMaskingFilter
for h in logging.root.handlers:
    if isinstance(h, logging.FileHandler):
        h.addFilter(APIKeyMaskingFilter())
logger = logging.getLogger(__name__)
bot_instance = None
dp_instance = None

def signal_handler(signum, frame):
    logger.info(f'Получен сигнал {signum}, начинаю graceful shutdown...')
    sys.exit(0)

async def resume_pending_generations(bot: Bot):
    from services.video_service import poll_veo_operation
    from services.gemini_image import generate_image_with_gemini
    from services.openai_service import generate_image_with_gpt
    from services.nvidia import generate_image_with_nvidia
    from services.openrouter import generate_image_with_openrouter
    from aiogram.types import BufferedInputFile
    from utils import make_safe_caption
    pending = await get_all_pending_gens()
    if not pending:
        return
    logger.info(f'Найдено {len(pending)} незавершённых задач, восстанавливаю...')
    for task in pending:
        gen_id = task['id']
        chat_id = task['chat_id']
        source_msg_id = task['source_message_id']
        thread_id = task.get('message_thread_id')
        prompt = task['prompt']
        model = task['model']
        provider = task['provider']
        model_label = task.get('model_label', model)
        reply_kwargs = {'message_thread_id': thread_id} if thread_id else {}
        try:
            await bot.send_message(chat_id=chat_id, text=f'🔄 Бот был перезапущен. Продолжаю генерацию: {model_label}...', reply_to_message_id=source_msg_id, **reply_kwargs)
            if task['gen_type'] == 'video' and task.get('veo_operation_name'):
                (video_bytes, error) = await poll_veo_operation(task['veo_operation_name'], task['veo_api_key'])
                await delete_pending_gen(gen_id)
                if video_bytes:
                    caption = make_safe_caption(f'🎬 Видео ({model_label}) по запросу: ', prompt)
                    await bot.send_video(chat_id=chat_id, video=BufferedInputFile(video_bytes, filename='generated.mp4'), caption=caption, reply_to_message_id=source_msg_id, **reply_kwargs)
                else:
                    await bot.send_message(chat_id=chat_id, text=f'❌ Ошибка видео после перезапуска:\n{error}', reply_to_message_id=source_msg_id, **reply_kwargs)
            elif task['gen_type'] == 'image':
                images_bytes = []
                for fid in task.get('file_ids', []):
                    try:
                        file_info = await bot.get_file(fid)
                        dl = await bot.download_file(file_info.file_path)
                        images_bytes.append(dl.read())
                    except Exception:
                        pass
                imgs = images_bytes or None
                if provider == 'gemini':
                    (result, error) = await generate_image_with_gemini(prompt, images_bytes=imgs, model=model)
                elif provider == 'gpt':
                    (result, error) = await generate_image_with_gpt(prompt, images_bytes=imgs, model=model)
                elif provider == 'flux':
                    (result, error) = await generate_image_with_nvidia(prompt, model=model)
                else:
                    (result, error) = await generate_image_with_openrouter(prompt, model=model)
                await delete_pending_gen(gen_id)
                if result:
                    caption = make_safe_caption(f'🎨 Результат ({model_label}) после перезапуска: ', prompt)
                    await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(result, filename='generated.jpg'), caption=caption, reply_to_message_id=source_msg_id, **reply_kwargs)
                else:
                    await bot.send_message(chat_id=chat_id, text=f'❌ Ошибка изображения после перезапуска:\n{error}', reply_to_message_id=source_msg_id, **reply_kwargs)
            else:
                await delete_pending_gen(gen_id)
        except Exception as e:
            logger.error(f'Ошибка восстановления задачи {gen_id}: {e}')
            await delete_pending_gen(gen_id)



async def on_startup(bot: Bot):
    try:
        await bot.set_my_commands(PUBLIC_COMMANDS)
    except Exception as e:
        logger.warning(f'Не удалось зарегистрировать меню команд: {type(e).__name__}: {e}', exc_info=True)
    from agent.workspace import cleanup_stale_workspaces
    cleanup_stale_workspaces()
    logger.info('Инициализация базы данных...')
    await init_db()
    banned = await get_banned_users_db()
    for b in banned:
        banned_user_ids.add(b)
    chat_lims = await get_all_chat_limits()
    for (cid, (req_limit, days)) in chat_lims.items():
        chat_custom_limits[cid] = (req_limit, days)
    vips = await get_all_vip_users()
    for uid, paid_until in vips.items():
        paid_unlimited_until[uid] = paid_until
    usage = await get_all_daily_limits_usage()
    for key, val in usage.items():
        daily_gen_limits[key] = val
    logger.info(f'База данных инициализирована. Загружено банов: {len(banned_user_ids)}, лимитов чатов: {len(chat_custom_limits)}, VIP-пользователей: {len(vips)}, лимитов использования: {len(usage)}')
    asyncio.create_task(resume_pending_generations(bot))
    asyncio.create_task(refresh_models())

async def on_shutdown():
    logger.info('Начинаю остановку бота...')
    from dual_bot import bot2
    if bot_instance:
        await bot_instance.session.close()
    try:
        await bot2.session.close()
    except Exception:
        pass
    try:
        from services.elevenlabs_service import close_elevenlabs
        await close_elevenlabs()
    except Exception as e:
        logger.warning(f"Error closing ElevenLabs: {e}")
    logger.info('Бот успешно остановлен')

async def _nano_keys_sync_loop():
    from config import NANO_KEY_SYNC_INTERVAL
    from keys import init_db, sync_from_keyhunter
    await init_db()
    await sync_from_keyhunter()
    while True:
        await asyncio.sleep(NANO_KEY_SYNC_INTERVAL)
        await sync_from_keyhunter()


async def main():
    global bot_instance, dp_instance
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    logger.info('Запускаю бота...')
    try:
        from dual_bot import bot2, dp2, router2, set_bot1_ref, init_bot2
        from aiogram.client.telegram import TelegramAPIServer
        from aiogram.client.session.aiohttp import AiohttpSession
        from config import TELEGRAM_API_URL

        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API_URL, is_local=True))
        bot_instance = Bot(token=BOT_TOKEN, session=session)
        from aiogram.fsm.storage.memory import MemoryStorage
        dp_instance = Dispatcher(storage=MemoryStorage())
        dp_instance.message.middleware(BanMiddleware())
        dp_instance.message.middleware(DualHistoryMiddleware())
        dp_instance.callback_query.middleware(BanMiddleware())
        dp_instance.callback_query.middleware(CallbackAccessMiddleware())
        dp_instance.include_router(router)

        # ── Global error handler: forward all exceptions to owner ──
        from config import OWNER_USER_ID
        async def _owner_error_handler(error_event):
            exception = error_event.exception
            import traceback as _tb
            tb = ''.join(_tb.format_exception(type(exception), exception, exception.__traceback__))[-2000:]
            try:
                await bot_instance.send_message(
                    OWNER_USER_ID,
                    f"❌ <b>УПАЛО:</b> <code>{type(exception).__name__}: {exception}</code>\n"
                    f"<pre>{tb[:1500]}</pre>",
                    parse_mode='HTML',
                )
            except Exception:
                logger.error(f"Failed to notify owner about error: {exception}")
        dp_instance.errors.register(_owner_error_handler)
        dp2.include_router(router2)
        await on_startup(bot_instance)
        set_bot1_ref(bot_instance)
        await asyncio.gather(
            bot_instance.delete_webhook(drop_pending_updates=True),
            bot2.delete_webhook(drop_pending_updates=True),
        )
        await init_bot2()
        logger.info('Оба бота запущены и готовы к работе!')
        from figma_bridge import start_bridge
        await asyncio.gather(
            dp_instance.start_polling(bot_instance),
            dp2.start_polling(bot2),
            start_bridge(),
            _nano_keys_sync_loop(),
        )
    except Exception as e:
        logger.exception(f'Критическая ошибка при запуске бота: {e}')
        raise
    finally:
        await on_shutdown()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Бот остановлен пользователем')
    except Exception as e:
        logger.exception(f'Неожиданная ошибка: {e}')
        sys.exit(1)
