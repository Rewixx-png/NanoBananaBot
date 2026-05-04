import aiosqlite
import json
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

DB_PATH = "bot_data.db"

# ==========================================
# База Данных (история чатов)
# ==========================================
async def init_db():
    """Инициализация базы данных"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS chat_history "
                "(chat_id INTEGER PRIMARY KEY, history TEXT)"
            )
            await db.commit()
            logger.info("База данных успешно инициализирована")
    except Exception as e:
        logger.exception(f"Ошибка при инициализации базы данных: {e}")
        raise

async def get_history(chat_id: int) -> List[Dict[str, Any]]:
    """Получить историю чата"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT history FROM chat_history WHERE chat_id = ?", 
                (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return json.loads(row[0])
                return []
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON для chat_id {chat_id}: {e}")
        return []
    except Exception as e:
        logger.exception(f"Ошибка при получении истории для chat_id {chat_id}: {e}")
        return []

async def save_history(chat_id: int, history: List[Dict[str, Any]]):
    """Сохранить историю чата"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO chat_history (chat_id, history) VALUES (?, ?)", 
                (chat_id, json.dumps(history, ensure_ascii=False))
            )
            await db.commit()
    except Exception as e:
        logger.exception(f"Ошибка при сохранении истории для chat_id {chat_id}: {e}")
        raise
