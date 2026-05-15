import asyncio
import logging
import signal
import sys
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from config import BOT_TOKEN, BANNED_USER_IDS
from database import init_db, get_all_pending_gens, delete_pending_gen, get_banned_users_db, get_all_chat_limits
from handlers import router, refresh_models
from state import banned_user_ids, chat_custom_limits
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.FileHandler('bot.log'), logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)
bot_instance = None
dp_instance = None

def signal_handler(signum, frame):
    logger.info(f'Получен сигнал {signum}, начинаю graceful shutdown...')
    sys.exit(0)

async def resume_pending_generations(bot: Bot):
    from ai_services import poll_veo_operation, generate_image_with_gemini, generate_image_with_gpt, generate_image_with_nvidia, generate_image_with_openrouter
    from aiogram.types import BufferedInputFile
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
                    await bot.send_video(chat_id=chat_id, video=BufferedInputFile(video_bytes, filename='generated.mp4'), caption=f'🎬 Видео ({model_label}) по запросу: {prompt}', reply_to_message_id=source_msg_id, **reply_kwargs)
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
                    await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(result, filename='generated.jpg'), caption=f'🎨 Результат ({model_label}) после перезапуска: {prompt}', reply_to_message_id=source_msg_id, **reply_kwargs)
                else:
                    await bot.send_message(chat_id=chat_id, text=f'❌ Ошибка изображения после перезапуска:\n{error}', reply_to_message_id=source_msg_id, **reply_kwargs)
            else:
                await delete_pending_gen(gen_id)
        except Exception as e:
            logger.error(f'Ошибка восстановления задачи {gen_id}: {e}')
            await delete_pending_gen(gen_id)

async def on_startup(bot: Bot):
    logger.info('Инициализация базы данных...')
    await init_db()
    banned = await get_banned_users_db()
    for b in banned:
        banned_user_ids.add(b)
    chat_lims = await get_all_chat_limits()
    for (cid, (req_limit, days)) in chat_lims.items():
        chat_custom_limits[cid] = (req_limit, days)
    logger.info(f'База данных инициализирована. Загружено банов: {len(banned_user_ids)}, лимитов чатов: {len(chat_custom_limits)}')
    asyncio.create_task(resume_pending_generations(bot))
    asyncio.create_task(refresh_models())

async def on_shutdown():
    logger.info('Начинаю остановку бота...')
    if bot_instance:
        await bot_instance.session.close()
    logger.info('Бот успешно остановлен')

async def main():
    global bot_instance, dp_instance
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    logger.info('Запускаю бота...')
    try:
        bot_instance = Bot(token=BOT_TOKEN)
        dp_instance = Dispatcher()
        dp_instance.message.middleware(BanMiddleware())
        dp_instance.callback_query.middleware(BanMiddleware())
        dp_instance.include_router(router)
        await on_startup(bot_instance)
        logger.info('Удаляю webhook...')
        await bot_instance.delete_webhook(drop_pending_updates=True)
        logger.info('Бот успешно запущен и готов к работе!')
        await dp_instance.start_polling(bot_instance)
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