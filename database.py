import aiosqlite
import json
import time
import logging
from typing import List, Dict, Any, Optional
logger = logging.getLogger(__name__)
DB_PATH = 'bot_data.db'

async def init_db():
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('PRAGMA journal_mode=WAL')
            await db.execute('PRAGMA busy_timeout=5000')
            await db.execute('CREATE TABLE IF NOT EXISTS chat_history (chat_id INTEGER PRIMARY KEY, history TEXT)')
            await db.execute('\n                CREATE TABLE IF NOT EXISTS pending_generations (\n                    id TEXT PRIMARY KEY,\n                    gen_type TEXT,\n                    user_id INTEGER,\n                    chat_id INTEGER,\n                    source_message_id INTEGER,\n                    message_thread_id INTEGER,\n                    prompt TEXT,\n                    model TEXT,\n                    provider TEXT,\n                    file_ids TEXT,\n                    veo_operation_name TEXT,\n                    veo_api_key TEXT,\n                    model_label TEXT,\n                    created_at REAL\n                )\n            ')
            await db.execute('\n                CREATE TABLE IF NOT EXISTS user_stats (\n                    user_id INTEGER,\n                    username TEXT,\n                    first_name TEXT,\n                    date_str TEXT,\n                    gen_type TEXT,\n                    count INTEGER DEFAULT 0,\n                    PRIMARY KEY (user_id, date_str, gen_type)\n                )\n            ')
            await db.execute('CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY)')
            await db.execute('\n                CREATE TABLE IF NOT EXISTS prompt_logs (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    user_id INTEGER,\n                    username TEXT,\n                    first_name TEXT,\n                    gen_type TEXT,\n                    prompt TEXT,\n                    created_at REAL\n                )\n            ')
            await db.execute('\n                CREATE TABLE IF NOT EXISTS chat_limits (\n                    chat_id INTEGER PRIMARY KEY,\n                    req_limit INTEGER,\n                    days INTEGER\n                )\n            ')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    task_text TEXT,
                    workspace_path TEXT,
                    started_at REAL
                )
            ''')
            await db.commit()
            logger.info('База данных успешно инициализирована')
    except Exception as e:
        logger.exception(f'Ошибка при инициализации базы данных: {e}')
        raise

async def save_pending_gen(gen_id: str, gen_type: str, user_id: int, chat_id: int, source_message_id: int, message_thread_id: Optional[int], prompt: str, model: str, provider: str, file_ids: list=None, veo_operation_name: str=None, veo_api_key: str=None, model_label: str=''):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('\n                INSERT OR REPLACE INTO pending_generations\n                (id, gen_type, user_id, chat_id, source_message_id, message_thread_id,\n                 prompt, model, provider, file_ids, veo_operation_name, veo_api_key, model_label, created_at)\n                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ', (gen_id, gen_type, user_id, chat_id, source_message_id, message_thread_id, prompt, model, provider, json.dumps(file_ids or []), veo_operation_name, veo_api_key, model_label, time.time()))
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
                cols = ['id', 'gen_type', 'user_id', 'chat_id', 'source_message_id', 'message_thread_id', 'prompt', 'model', 'provider', 'file_ids', 'veo_operation_name', 'veo_api_key', 'model_label', 'created_at']
                result = []
                for row in rows:
                    d = dict(zip(cols, row))
                    d['file_ids'] = json.loads(d.get('file_ids') or '[]')
                    result.append(d)
                return result
    except Exception as e:
        logger.error(f'Ошибка загрузки pending_gens: {e}')
        return []

async def get_history(chat_id: int) -> List[Dict[str, Any]]:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
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
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('INSERT OR REPLACE INTO chat_history (chat_id, history) VALUES (?, ?)', (chat_id, json.dumps(history, ensure_ascii=False)))
            await db.commit()
    except Exception as e:
        logger.exception(f'Ошибка при сохранении истории для chat_id {chat_id}: {e}')
        raise

async def add_user_stat(user_id: int, username: str, first_name: str, gen_type: str):
    from datetime import date
    date_str = str(date.today())
    username = username or ''
    first_name = first_name or 'Аноним'
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('\n                INSERT INTO user_stats (user_id, username, first_name, date_str, gen_type, count)\n                VALUES (?, ?, ?, ?, ?, 1)\n                ON CONFLICT(user_id, date_str, gen_type) DO UPDATE SET \n                count = count + 1, username=excluded.username, first_name=excluded.first_name\n            ', (user_id, username, first_name, date_str, gen_type))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения статистики: {e}')

async def get_user_stats(date_str: str=None) -> List[dict]:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            if date_str:
                async with db.execute('\n                    SELECT user_id, username, first_name, SUM(count) as c, gen_type \n                    FROM user_stats WHERE date_str = ? \n                    GROUP BY user_id, username, first_name, gen_type ORDER BY c DESC\n                ', (date_str,)) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute('\n                    SELECT user_id, username, first_name, SUM(count) as c, gen_type \n                    FROM user_stats \n                    GROUP BY user_id, username, first_name, gen_type ORDER BY c DESC\n                ') as cur:
                    rows = await cur.fetchall()
            return [{'user_id': r[0], 'username': r[1], 'first_name': r[2], 'count': r[3], 'type': r[4]} for r in rows]
    except Exception as e:
        logger.error(f'Ошибка чтения статистики: {e}')
        return []

async def get_banned_users_db() -> set:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            async with db.execute('SELECT user_id FROM banned_users') as cur:
                rows = await cur.fetchall()
                return {r[0] for r in rows}
    except Exception:
        return set()

async def add_banned_user_db(user_id: int):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)', (user_id,))
            await db.commit()
    except Exception:
        pass

async def remove_banned_user_db(user_id: int):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('DELETE FROM banned_users WHERE user_id = ?', (user_id,))
            await db.commit()
    except Exception:
        pass

async def log_prompt(user_id: int, username: str, first_name: str, gen_type: str, prompt: str):
    try:
        username = username or ''
        first_name = first_name or 'Аноним'
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('INSERT INTO prompt_logs (user_id, username, first_name, gen_type, prompt, created_at) VALUES (?, ?, ?, ?, ?, ?)', (user_id, username, first_name, gen_type, prompt, time.time()))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка логирования промпта: {e}')

async def get_recent_prompts(limit: int=50, user_id: int=None) -> List[Dict[str, Any]]:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            if user_id:
                async with db.execute('SELECT user_id, username, first_name, gen_type, prompt, created_at FROM prompt_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?', (user_id, limit)) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute('SELECT user_id, username, first_name, gen_type, prompt, created_at FROM prompt_logs ORDER BY created_at DESC LIMIT ?', (limit,)) as cur:
                    rows = await cur.fetchall()
            return [{'user_id': r[0], 'username': r[1], 'first_name': r[2], 'gen_type': r[3], 'prompt': r[4], 'created_at': r[5]} for r in rows]
    except Exception as e:
        logger.error(f'Ошибка получения промптов: {e}')
        return []

async def get_all_chat_limits() -> dict:
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            async with db.execute('SELECT chat_id, req_limit, days FROM chat_limits') as cur:
                rows = await cur.fetchall()
                return {r[0]: (r[1], r[2]) for r in rows}
    except Exception:
        return {}

async def set_chat_limit_db(chat_id: int, req_limit: int, days: int):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('INSERT OR REPLACE INTO chat_limits (chat_id, req_limit, days) VALUES (?, ?, ?)', (chat_id, req_limit, days))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения лимита чата: {e}')
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
            async with db.execute('SELECT task_id, chat_id, user_id, username, task_text, workspace_path, started_at FROM agent_tasks') as cur:
                rows = await cur.fetchall()
        return [{'task_id': r[0], 'chat_id': r[1], 'user_id': r[2], 'username': r[3], 'task_text': r[4], 'workspace_path': r[5], 'started_at': r[6]} for r in rows]
    except Exception as e:
        logger.error(f'Ошибка получения agent_tasks: {e}')
        return []
