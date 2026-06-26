import aiosqlite
import time
import logging
from typing import List, Dict, Any

from database.connection import DB_PATH

logger = logging.getLogger(__name__)


async def save_agent_task(task_id: str, chat_id: int, user_id: int, username: str, task_text: str, workspace_path: str):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute(
                'INSERT OR REPLACE INTO agent_tasks (task_id, chat_id, user_id, username, task_text, workspace_path, started_at) VALUES (?,?,?,?,?,?,?)',
                (task_id, chat_id, user_id, username, task_text[:500], workspace_path, time.time())
            )
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения agent_task: {e}')


async def delete_agent_task(task_id: str):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('DELETE FROM agent_tasks WHERE task_id=?', (task_id,))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка удаления agent_task: {e}')


async def get_interrupted_agent_tasks() -> List[Dict[str, Any]]:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            async with db.execute(
                'SELECT task_id, chat_id, user_id, username, task_text, workspace_path, started_at FROM agent_tasks'
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                'task_id': r[0], 'chat_id': r[1], 'user_id': r[2], 'username': r[3],
                'task_text': r[4], 'workspace_path': r[5], 'started_at': r[6]
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f'Ошибка получения agent_tasks: {e}')
        return []


async def log_prompt(user_id: int, username: str, first_name: str, gen_type: str, prompt: str):
    try:
        username = username or ''
        first_name = first_name or 'Аноним'
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute(
                'INSERT INTO prompt_logs (user_id, username, first_name, gen_type, prompt, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, username, first_name, gen_type, prompt, time.time())
            )
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка логирования промпта: {e}')


async def get_recent_prompts(limit: int = 50, user_id: int = None) -> List[Dict[str, Any]]:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
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
