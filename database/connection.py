import aiosqlite
import logging

logger = logging.getLogger(__name__)
DB_PATH = 'bot_data.db'


async def init_db():
    """Initialize database schema — creates all tables if they don't exist, sets WAL mode, purges old prompt logs."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute('PRAGMA journal_mode=WAL')
            await db.execute('PRAGMA busy_timeout=5000')
            await db.execute('CREATE TABLE IF NOT EXISTS chat_history (chat_id INTEGER PRIMARY KEY, history TEXT)')
            await db.execute('''
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
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    date_str TEXT,
                    gen_type TEXT,
                    count INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, date_str, gen_type)
                )
            ''')
            await db.execute('CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY)')
            await db.execute('CREATE TABLE IF NOT EXISTS vip_users (user_id INTEGER PRIMARY KEY, paid_until REAL)')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS daily_limits_usage (
                    chat_id INTEGER,
                    user_id INTEGER,
                    period TEXT,
                    count INTEGER,
                    PRIMARY KEY (chat_id, user_id)
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS prompt_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    gen_type TEXT,
                    prompt TEXT,
                    created_at REAL
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS chat_limits (
                    chat_id INTEGER PRIMARY KEY,
                    req_limit INTEGER,
                    days INTEGER
                )
            ''')
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
            await db.execute('DELETE FROM prompt_logs WHERE created_at < unixepoch() - 30 * 86400')
            await db.commit()
            logger.info('База данных успешно инициализирована')
    except Exception as e:
        logger.exception(f'Ошибка при инициализации базы данных: {e}')
        raise
