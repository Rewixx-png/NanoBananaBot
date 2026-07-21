import aiosqlite
import logging
from contextlib import asynccontextmanager

from config import DB_PATH, DB_BUSY_TIMEOUT_MS, PROMPT_LOG_RETENTION_DAYS

logger = logging.getLogger(__name__)


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH, timeout=10)
    await db.execute('PRAGMA journal_mode=WAL')
    await db.execute(f'PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}')
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    """Initialize database schema using versioned migrations."""
    try:
        async with get_db() as db:
            from database.migrations import apply_migrations
            await apply_migrations(db)
            # Periodic cleanup of old prompt logs
            await db.execute(f"DELETE FROM prompt_logs WHERE created_at < CAST(strftime('%s', 'now') AS REAL) - {PROMPT_LOG_RETENTION_DAYS} * 86400")
            await db.commit()
            logger.info('База данных успешно инициализирована')
    except Exception as e:
        logger.exception(f'Ошибка при инициализации базы данных: {e}')
        raise
