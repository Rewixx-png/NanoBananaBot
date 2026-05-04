import asyncio
import logging
import signal
import sys
from aiogram import Bot, Dispatcher
from config import BOT_TOKEN
from database import init_db
from handlers import router

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Глобальные переменные для graceful shutdown
bot_instance = None
dp_instance = None

def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    logger.info(f"Получен сигнал {signum}, начинаю graceful shutdown...")
    sys.exit(0)

async def on_startup():
    """Действия при запуске бота"""
    logger.info("Инициализация базы данных...")
    await init_db()
    logger.info("База данных инициализирована")

async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("Начинаю остановку бота...")
    if bot_instance:
        await bot_instance.session.close()
    logger.info("Бот успешно остановлен")

async def main():
    global bot_instance, dp_instance
    
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("Запускаю бота...")
    
    try:
        bot_instance = Bot(token=BOT_TOKEN)
        dp_instance = Dispatcher()
        dp_instance.include_router(router)
        
        # Startup действия
        await on_startup()
        
        # Удаляем webhook если был
        logger.info("Удаляю webhook...")
        await bot_instance.delete_webhook(drop_pending_updates=True)
        
        logger.info("Бот успешно запущен и готов к работе!")
        await dp_instance.start_polling(bot_instance)
        
    except Exception as e:
        logger.exception(f"Критическая ошибка при запуске бота: {e}")
        raise
    finally:
        await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.exception(f"Неожиданная ошибка: {e}")
        sys.exit(1)
