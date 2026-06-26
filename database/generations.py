import aiosqlite
import json
import time
import logging
from typing import List, Dict, Any, Optional

from database.connection import DB_PATH

logger = logging.getLogger(__name__)


async def save_pending_gen(
    gen_id: str, gen_type: str, user_id: int, chat_id: int,
    source_message_id: int, message_thread_id: Optional[int],
    prompt: str, model: str, provider: str,
    file_ids: list = None, veo_operation_name: str = None,
    veo_api_key: str = None, model_label: str = ''
):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('''
                INSERT OR REPLACE INTO pending_generations
                (id, gen_type, user_id, chat_id, source_message_id, message_thread_id,
                 prompt, model, provider, file_ids, veo_operation_name, veo_api_key, model_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                gen_id, gen_type, user_id, chat_id, source_message_id, message_thread_id,
                prompt, model, provider, json.dumps(file_ids or []),
                veo_operation_name, veo_api_key, model_label, time.time()
            ))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения pending_gen {gen_id}: {e}')


async def delete_pending_gen(gen_id: str):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('DELETE FROM pending_generations WHERE id = ?', (gen_id,))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка удаления pending_gen {gen_id}: {e}')


async def get_all_pending_gens() -> List[Dict[str, Any]]:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            async with db.execute('SELECT * FROM pending_generations ORDER BY created_at') as cursor:
                rows = await cursor.fetchall()
                cols = [
                    'id', 'gen_type', 'user_id', 'chat_id', 'source_message_id',
                    'message_thread_id', 'prompt', 'model', 'provider', 'file_ids',
                    'veo_operation_name', 'veo_api_key', 'model_label', 'created_at'
                ]
                result = []
                for row in rows:
                    d = dict(zip(cols, row))
                    d['file_ids'] = json.loads(d.get('file_ids') or '[]')
                    result.append(d)
                return result
    except Exception as e:
        logger.error(f'Ошибка загрузки pending_gens: {e}')
        return []
