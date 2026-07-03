import json
import os
import time
import logging
from typing import List, Dict, Any, Optional

from database.connection import get_db

logger = logging.getLogger(__name__)

# ── Fernet encryption for veo_api_key ─────────────────────────────────────

_fernet = None


def _get_fernet():
    """Load or create a Fernet instance from DB_ENCRYPTION_KEY env var."""
    global _fernet
    if _fernet is not None:
        return _fernet
    from cryptography.fernet import Fernet
    key = os.getenv('DB_ENCRYPTION_KEY', '').strip()
    if key:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    else:
        logger.warning(
            'DB_ENCRYPTION_KEY not set in .env — using a temporary key. '
            'Set DB_ENCRYPTION_KEY to persist Veo API keys across restarts.'
        )
        _fernet = Fernet(Fernet.generate_key())
    return _fernet


def _encrypt_key(plain: Optional[str]) -> Optional[str]:
    if not plain:
        return plain
    try:
        return _get_fernet().encrypt(plain.encode()).decode()
    except Exception as e:
        logger.error(f'Failed to encrypt veo_api_key: {e}')
        return None


def _decrypt_key(encrypted: Optional[str]) -> Optional[str]:
    if not encrypted:
        return encrypted
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.error(f'Failed to decrypt veo_api_key: {e}')
        return None


async def save_pending_gen(
    gen_id: str, gen_type: str, user_id: int, chat_id: int,
    source_message_id: int, message_thread_id: Optional[int],
    prompt: str, model: str, provider: str,
    file_ids: list = None, veo_operation_name: str = None,
    veo_api_key: str = None, model_label: str = ''
):
    try:
        async with get_db() as db:
            await db.execute('''
                INSERT OR REPLACE INTO pending_generations
                (id, gen_type, user_id, chat_id, source_message_id, message_thread_id,
                 prompt, model, provider, file_ids, veo_operation_name, veo_api_key, model_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                gen_id, gen_type, user_id, chat_id, source_message_id, message_thread_id,
                prompt, model, provider, json.dumps(file_ids or []),
                veo_operation_name, _encrypt_key(veo_api_key), model_label, time.time()
            ))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения pending_gen {gen_id}: {e}')


async def delete_pending_gen(gen_id: str):
    try:
        async with get_db() as db:
            await db.execute('DELETE FROM pending_generations WHERE id = ?', (gen_id,))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка удаления pending_gen {gen_id}: {e}')


async def get_all_pending_gens() -> List[Dict[str, Any]]:
    try:
        async with get_db() as db:
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
                    d['veo_api_key'] = _decrypt_key(d.get('veo_api_key'))
                    result.append(d)
                return result
    except Exception as e:
        logger.error(f'Ошибка загрузки pending_gens: {e}')
        return []
