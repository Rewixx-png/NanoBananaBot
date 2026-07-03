import json
import logging
from typing import List, Dict, Any

from database.connection import get_db

logger = logging.getLogger(__name__)


async def get_history(chat_id: int) -> List[Dict[str, Any]]:
    try:
        async with get_db() as db:
            async with db.execute('SELECT history FROM chat_history WHERE chat_id = ?', (chat_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return json.loads(row[0])
                return []
    except json.JSONDecodeError as e:
        logger.error(f'Ошибка декодирования JSON для chat_id {chat_id}: {e}')
        return []
    except Exception as e:
        logger.exception(f'Ошибка при получении истории для chat_id {chat_id}: {e}')
        return []


async def save_history(chat_id: int, history: List[Dict[str, Any]]):
    try:
        async with get_db() as db:
            await db.execute(
                'INSERT OR REPLACE INTO chat_history (chat_id, history) VALUES (?, ?)',
                (chat_id, json.dumps(history, ensure_ascii=False))
            )
            await db.commit()
    except Exception as e:
        logger.exception(f'Ошибка при сохранении истории для chat_id {chat_id}: {e}')
        raise
