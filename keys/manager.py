import json
import re
import os
import aiosqlite
import logging
import time
from config import API_KEYS_FILE, OPENAI_API_KEY

REWTEST_DB = "/root/RewTest/keyhunter.db"
_session_dead: dict[str, float] = {}  # key -> expiry timestamp


def _is_dead(key: str) -> bool:
    """Return True if key is in cooldown and hasn't expired yet."""
    exp = _session_dead.get(key)
    if exp is None:
        return False
    if time.time() < exp:
        return True
    del _session_dead[key]  # expired - prune
    return False

def strip_code_fences(content: str) -> str:
    content = content.strip()
    if content.startswith('```json'):
        content = content[7:]
    elif content.startswith('```'):
        content = content[3:]
    if content.endswith('```'):
        content = content[:-3]
    return content.strip()

def normalize_key_list(value):
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.splitlines()
    else:
        return []
    keys = []
    seen = set()
    for item in raw_items:
        for piece in re.split('[\\s,]+', str(item).strip()):
            key = piece.strip().strip('"\'')
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return keys

def load_api_config():
    default_config = {'gemini': [], 'openai': '', 'firecrawl': []}
    enc_path = API_KEYS_FILE + ".enc"
    try:
        from keys.crypto import decrypt_keys_file
        raw_content = decrypt_keys_file(enc_path).decode("utf-8")
    except Exception as e:
        logging.warning(f'Encrypted keys not available ({e}), trying plaintext fallback')
        try:
            with open(API_KEYS_FILE, 'r') as f:
                raw_content = f.read()
        except Exception as e2:
            logging.error(f'Ошибка загрузки ключей: {e2}')
            return default_config
    content = strip_code_fences(raw_content)
    data = {}
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            data = parsed
    except Exception as e:
        logging.warning(f'Файл ключей поврежден, использую аварийный парсер: {e}')
    gemini_keys = normalize_key_list(data.get('gemini', []))
    if not gemini_keys:
        gemini_keys = list(dict.fromkeys(re.findall('AIza[0-9A-Za-z_-]{20,}', content)))
    openai_data = data.get('openai', '')
    if isinstance(openai_data, list):
        openai_value = normalize_key_list(openai_data)
    else:
        openai_value = openai_data.strip() if isinstance(openai_data, str) else ''
    if not openai_value and 'openai' not in data:
        openai_matches = list(dict.fromkeys(re.findall('sk-proj-[A-Za-z0-9_-]{20,}', content)))
        openai_value = openai_matches[0] if openai_matches else ''
    nvidia_data = data.get('nvidia', [])
    nvidia_keys = normalize_key_list(nvidia_data) if isinstance(nvidia_data, list) else [nvidia_data.strip()] if isinstance(nvidia_data, str) and nvidia_data.strip() else []
    openrouter_data = data.get('openrouter', [])
    openrouter_keys = normalize_key_list(openrouter_data) if isinstance(openrouter_data, list) else [openrouter_data.strip()] if isinstance(openrouter_data, str) and openrouter_data.strip() else []
    replicate_data = data.get('replicate', [])
    replicate_keys = normalize_key_list(replicate_data) if isinstance(replicate_data, list) else [replicate_data.strip()] if isinstance(replicate_data, str) and replicate_data.strip() else []
    firecrawl_data = data.get('firecrawl', [])
    firecrawl_keys = normalize_key_list(firecrawl_data) if isinstance(firecrawl_data, list) else [firecrawl_data.strip()] if isinstance(firecrawl_data, str) and firecrawl_data.strip() else []
    if not firecrawl_keys:
        firecrawl_keys = list(dict.fromkeys(re.findall(r'fc-[0-9a-fA-F]{32}', content)))
    return {'gemini': gemini_keys, 'openai': openai_value, 'nvidia': nvidia_keys, 'openrouter': openrouter_keys, 'replicate': replicate_keys, 'firecrawl': firecrawl_keys}

def save_api_config(config):
    gemini_keys = normalize_key_list(config.get('gemini', []))
    openai_value = config.get('openai', '')
    if isinstance(openai_value, list):
        normalized_openai = normalize_key_list(openai_value)
        if len(normalized_openai) == 1:
            openai_value = normalized_openai[0]
        else:
            openai_value = normalized_openai
    elif isinstance(openai_value, str):
        openai_value = openai_value.strip()
    else:
        openai_value = ''
    out_data = {'gemini': gemini_keys}
    if openai_value:
        out_data['openai'] = openai_value
    for key in ['replicate', 'nvidia', 'openrouter', 'firecrawl']:
        val = config.get(key)
        if val:
            if isinstance(val, list):
                out_data[key] = normalize_key_list(val)
            elif isinstance(val, str) and val.strip():
                out_data[key] = val.strip()
    out_json = '```json\n' + json.dumps(out_data, ensure_ascii=False, indent=2) + '\n```\n'
    enc_path = API_KEYS_FILE + ".enc"
    tmp_file = enc_path + ".tmp"
    try:
        from keys.crypto import encrypt_bytes
        encrypted = encrypt_bytes(out_json.encode("utf-8"))
        with open(tmp_file, 'wb') as f:
            f.write(encrypted)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, enc_path)
    except Exception as e:
        logging.error(f'Ошибка при атомарном сохранении API-конфига: {e}')
        if os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception:
                pass

async def load_keys(model_filter: str = None):
    """Load live Gemini keys, optionally filtered by model access from info field."""
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            if model_filter:
                async with db.execute(
                    "SELECT key FROM keys WHERE service='Gemini' AND is_live=1 AND info LIKE ?",
                    (f'%{model_filter}%',)
                ) as cur:
                    rows = await cur.fetchall()
                    keys = [row[0] for row in rows if not _is_dead(row[0])]
                    if keys:
                        return keys
            async with db.execute("SELECT key FROM keys WHERE service='Gemini' AND is_live=1") as cur:
                rows = await cur.fetchall()
                keys = [row[0] for row in rows if not _is_dead(row[0])]
                if keys:
                    return keys
    except Exception:
        pass
    return [k for k in load_api_config().get('gemini', []) if not _is_dead(k)]

async def load_openai_keys():
    # 1. Keyhunter DB first (most reliable, most keys)
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            async with db.execute(
                "SELECT key, info FROM keys WHERE service='OpenAI' AND is_live=1 AND (info NOT LIKE '%quota%' AND info NOT LIKE '%QUOTA%') ORDER BY CASE WHEN info LIKE '%flagship%' THEN 0 ELSE 1 END"
            ) as cur:
                rows = await cur.fetchall()
                keys = [row[0] for row in rows if not _is_dead(row[0]) and not row[1].startswith('⚠️')]
                if keys:
                    return keys
    except Exception:
        logging.exception('load_openai_keys: keyhunter query failed')
    # 2. r.txt.enc fallback
    try:
        openai_data = load_api_config().get('openai', '')
        if isinstance(openai_data, list):
            keys = [k for k in openai_data if not _is_dead(k)]
            if keys:
                return keys
        elif isinstance(openai_data, str) and openai_data.strip():
            if not _is_dead(openai_data.strip()):
                return [openai_data.strip()]
    except Exception as e:
        logging.error(f'Ошибка загрузки OpenAI ключей из r.txt: {e}')
    # 3. Environment variables (lowest priority — often stale/bogus)
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if key:
        return [key]
    if OPENAI_API_KEY.strip():
        return [OPENAI_API_KEY.strip()]
    return []


def load_nvidia_keys():
    try:
        nvidia_data = load_api_config().get('nvidia', [])
        if isinstance(nvidia_data, list) and nvidia_data:
            return nvidia_data
        if isinstance(nvidia_data, str) and nvidia_data.strip():
            return [nvidia_data.strip()]
    except Exception as e:
        logging.error(f'Ошибка загрузки NVIDIA ключей: {e}')
    return []

async def load_openrouter_keys():
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            async with db.execute("SELECT key FROM keys WHERE service='OpenRouter' AND is_live=1") as cur:
                rows = await cur.fetchall()
                keys = [row[0] for row in rows if not _is_dead(row[0])]
                if keys:
                    return keys
    except Exception:
        pass
    try:
        or_data = load_api_config().get('openrouter', [])
        if isinstance(or_data, list) and or_data:
            return or_data
        if isinstance(or_data, str) and or_data.strip():
            return [or_data.strip()]
    except Exception as e:
        logging.error(f'Ошибка загрузки OpenRouter ключей: {e}')
    return []

def load_replicate_keys():
    try:
        return load_api_config().get('replicate', [])
    except Exception as e:
        logging.error(f'Ошибка загрузки Replicate ключей: {e}')
    return []

async def load_groq_keys():
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            async with db.execute("SELECT key FROM keys WHERE service='Groq' AND is_live=1") as cur:
                rows = await cur.fetchall()
                keys = [row[0] for row in rows if not _is_dead(row[0])]
                if keys:
                    return keys
    except Exception as e:
        logging.warning(f'load_groq_keys DB error: {e}')
    try:
        groq_data = load_api_config().get('groq', [])
        if isinstance(groq_data, list) and groq_data:
            return groq_data
        if isinstance(groq_data, str) and groq_data.strip():
            return [groq_data.strip()]
    except Exception as e:
        logging.error(f'Ошибка загрузки Groq ключей: {e}')
    return []

async def load_firecrawl_keys():
    """Load Firecrawl keys sorted by remaining credits (highest first)."""
    import re
    env_keys = normalize_key_list(os.getenv('FIRECRAWL_KEYS', ''))
    single_env_key = os.getenv('FIRECRAWL_API_KEY', '').strip()
    if single_env_key:
        env_keys.insert(0, single_env_key)
    env_keys = list(dict.fromkeys([k for k in env_keys if not _is_dead(k)]))
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            async with db.execute(
                "SELECT key, info FROM keys WHERE service='Firecrawl' AND is_live=1"
            ) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        logging.warning(f'load_firecrawl_keys DB error: {e}')
        return env_keys or []
    # Parse credits from info (e.g. "Firecrawl LIVE · 85585/100000 credits")
    def _credits(info: str) -> int:
        m = re.search(r'(\d+)/\d+\s*credits', info or '')
        return int(m.group(1)) if m else 0
    pairs = [(r[0], _credits(r[1] or '')) for r in rows if not _is_dead(r[0])]
    pairs.sort(key=lambda x: x[1], reverse=True)
    result = [k for k, _ in pairs]
    # Append env keys as fallback (deduplicated)
    seen = set(result)
    for k in env_keys:
        if k not in seen:
            result.append(k)
            seen.add(k)
    return result if result else []


def remove_key(key_to_remove, status_code=None):
    if status_code == 429:
        _session_dead[key_to_remove] = time.time() + 65
        logging.info(f"Ключ {key_to_remove[:10]}... в кулдауне 65с (429 rate limit).")
        return
    if status_code == 403:
        _session_dead[key_to_remove] = time.time() + 300
        logging.info(f"Ключ {key_to_remove[:10]}... в кулдауне 300с (403 forbidden).")
        return
    _session_dead[key_to_remove] = time.time() + 86400 * 365
    config = load_api_config()
    removed = False
    for k, v in list(config.items()):
        if isinstance(v, list):
            if key_to_remove in v:
                v.remove(key_to_remove)
                config[k] = v
                removed = True
                logging.info(f"Ключ {key_to_remove[:10]}... удален из списка '{k}' (нет бабок/лимитов/ошибка 401/400).")
        elif isinstance(v, str):
            if v.strip() == key_to_remove:
                config[k] = ""
                removed = True
                logging.info(f"Ключ {key_to_remove[:10]}... удален из строки '{k}' (нет бабок/лимитов/ошибка 401/400).")
    if removed:
        save_api_config(config)
