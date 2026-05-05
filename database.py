import aiosqlite
import json
import time
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = "bot_data.db"

async def init_db():
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS chat_history "
                "(chat_id INTEGER PRIMARY KEY, history TEXT)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_generations (
                    id TEXT PRIMARY KEY,
                    gen_type TEXT,
                    user_id INTEGER,
                    chat_id INTEGER,
                    source_message_id INTEGER,
                    message_thread_id INTEGER,
                    prompt TEXT,
                    model TEXT,
                    provider TEXT,
                    file_ids TEXT,
                    veo_operation_name TEXT,
                    veo_api_key TEXT,
                    model_label TEXT,
                    created_at REAL
                )
            """)
            await db.commit()
            logger.info("База данных успешно инициализирована")
    except Exception as e:
        logger.exception(f"Ошибка при инициализации базы данных: {e}")
        raise

async def save_pending_gen(gen_id: str, gen_type: str, user_id: int, chat_id: int,
                            source_message_id: int, message_thread_id: Optional[int],
                            prompt: str, model: str, provider: str,
                            file_ids: list = None, veo_operation_name: str = None,
                            veo_api_key: str = None, model_label: str = ""):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO pending_generations
                (id, gen_type, user_id, chat_id, source_message_id, message_thread_id,
                 prompt, model, provider, file_ids, veo_operation_name, veo_api_key, model_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (gen_id, gen_type, user_id, chat_id, source_message_id, message_thread_id,
                  prompt, model, provider, json.dumps(file_ids or []),
                  veo_operation_name, veo_api_key, model_label, time.time()))
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения pending_gen {gen_id}: {e}")

async def delete_pending_gen(gen_id: str):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM pending_generations WHERE id = ?", (gen_id,))
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка удаления pending_gen {gen_id}: {e}")

async def get_all_pending_gens() -> List[Dict[str, Any]]:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT * FROM pending_generations ORDER BY created_at") as cursor:
                rows = await cursor.fetchall()
                cols = ['id', 'gen_type', 'user_id', 'chat_id', 'source_message_id',
                        'message_thread_id', 'prompt', 'model', 'provider',
                        'file_ids', 'veo_operation_name', 'veo_api_key', 'model_label', 'created_at']
                result = []
                for row in rows:
                    d = dict(zip(cols, row))
                    d['file_ids'] = json.loads(d.get('file_ids') or '[]')
                    result.append(d)
                return result
    except Exception as e:
        logger.error(f"Ошибка загрузки pending_gens: {e}")
        return []

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
