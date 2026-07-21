import time
import logging
from typing import List, Dict, Any

from database.connection import get_db

logger = logging.getLogger(__name__)








async def log_prompt(user_id: int, username: str, first_name: str, gen_type: str, prompt: str):
    try:
        username = username or ''
        first_name = first_name or 'Аноним'
        async with get_db() as db:
            await db.execute(
                'INSERT INTO prompt_logs (user_id, username, first_name, gen_type, prompt, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, username, first_name, gen_type, prompt, time.time())
            )
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка логирования промпта: {e}')


async def get_recent_prompts(limit: int = 50, user_id: int = None) -> List[Dict[str, Any]]:
    try:
        async with get_db() as db:
            if user_id:
                async with db.execute(
                    'SELECT user_id, username, first_name, gen_type, prompt, created_at FROM prompt_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
                    (user_id, limit)
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    'SELECT user_id, username, first_name, gen_type, prompt, created_at FROM prompt_logs ORDER BY created_at DESC LIMIT ?',
                    (limit,)
                ) as cur:
                    rows = await cur.fetchall()
            return [
                {'user_id': r[0], 'username': r[1], 'first_name': r[2], 'gen_type': r[3], 'prompt': r[4], 'created_at': r[5]}
                for r in rows
            ]
    except Exception as e:
        logger.error(f'Ошибка получения промптов: {e}')
        return []
