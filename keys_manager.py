import json
import re
import os
import logging
from config import API_KEYS_FILE, OPENAI_API_KEY

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
        for piece in re.split(r'[\s,]+', str(item).strip()):
            key = piece.strip().strip('"\'')
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return keys

def load_api_config():
    default_config = {"gemini": [], "openai": ""}

    try:
        with open(API_KEYS_FILE, 'r') as f:
            raw_content = f.read()
    except Exception as e:
        logging.error(f"Ошибка загрузки ключей: {e}")
        return default_config

    content = strip_code_fences(raw_content)
    data = {}

    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            data = parsed
    except Exception as e:
        logging.warning(f"Файл ключей поврежден, использую аварийный парсер: {e}")

    gemini_keys = normalize_key_list(data.get("gemini", []))
    if not gemini_keys:
        gemini_keys = list(dict.fromkeys(re.findall(r'AIza[0-9A-Za-z_-]{20,}', content)))

    openai_data = data.get("openai", "")
    if isinstance(openai_data, list):
        openai_value = normalize_key_list(openai_data)
    else:
        openai_value = openai_data.strip() if isinstance(openai_data, str) else ""

    if not openai_value and "openai" not in data:
        openai_matches = list(dict.fromkeys(re.findall(r'sk-proj-[A-Za-z0-9_-]{20,}', content)))
        openai_value = openai_matches[0] if openai_matches else ""

    nvidia_data = data.get("nvidia", [])
    nvidia_keys = normalize_key_list(nvidia_data) if isinstance(nvidia_data, list) else (
        [nvidia_data.strip()] if isinstance(nvidia_data, str) and nvidia_data.strip() else []
    )

    openrouter_data = data.get("openrouter", [])
    openrouter_keys = normalize_key_list(openrouter_data) if isinstance(openrouter_data, list) else (
        [openrouter_data.strip()] if isinstance(openrouter_data, str) and openrouter_data.strip() else []
    )

    return {
        "gemini": gemini_keys,
        "openai": openai_value,
        "nvidia": nvidia_keys,
        "openrouter": openrouter_keys,
    }

def save_api_config(config):
    gemini_keys = normalize_key_list(config.get("gemini", []))
    openai_value = config.get("openai", "")

    if isinstance(openai_value, list):
        normalized_openai = normalize_key_list(openai_value)
        if len(normalized_openai) == 1:
            openai_value = normalized_openai[0]
        else:
            openai_value = normalized_openai
    elif isinstance(openai_value, str):
        openai_value = openai_value.strip()
    else:
        openai_value = ""

    out_data = {"gemini": gemini_keys}
    if openai_value:
        out_data["openai"] = openai_value

    out_json = "```json\n" + json.dumps(out_data, ensure_ascii=False, indent=2) + "\n```\n"
    with open(API_KEYS_FILE, 'w') as f:
        f.write(out_json)

def load_keys():
    return load_api_config().get("gemini", [])

def load_openai_keys():
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key:
        return [key]

    if OPENAI_API_KEY.strip():
        return [OPENAI_API_KEY.strip()]

    try:
        openai_data = load_api_config().get("openai", "")
        if isinstance(openai_data, list) and openai_data:
            return openai_data
        if isinstance(openai_data, str) and openai_data.strip():
            return [openai_data.strip()]
    except Exception as e:
        logging.error(f"Ошибка загрузки OpenAI ключей: {e}")

    return []

def load_openai_key():
    keys = load_openai_keys()
    return keys[0] if keys else ""

def load_nvidia_keys():
    try:
        nvidia_data = load_api_config().get("nvidia", [])
        if isinstance(nvidia_data, list) and nvidia_data:
            return nvidia_data
        if isinstance(nvidia_data, str) and nvidia_data.strip():
            return [nvidia_data.strip()]
    except Exception as e:
        logging.error(f"Ошибка загрузки NVIDIA ключей: {e}")
    return []

def load_openrouter_keys():
    try:
        or_data = load_api_config().get("openrouter", [])
        if isinstance(or_data, list) and or_data:
            return or_data
        if isinstance(or_data, str) and or_data.strip():
            return [or_data.strip()]
    except Exception as e:
        logging.error(f"Ошибка загрузки OpenRouter ключей: {e}")
    return []

def load_replicate_keys():
    try:
        with open(API_KEYS_FILE, 'r') as f:
            raw = f.read()
        content = strip_code_fences(raw)
        data = json.loads(content)
        rep_data = data.get("replicate", [])
        if isinstance(rep_data, list) and rep_data:
            return rep_data
        if isinstance(rep_data, str) and rep_data.strip():
            return [rep_data.strip()]
    except Exception as e:
        logging.error(f"Ошибка загрузки Replicate ключей: {e}")
    return []

def remove_key(key_to_remove):
    config = load_api_config()
    keys = config.get("gemini", [])
    if key_to_remove in keys:
        keys.remove(key_to_remove)
        config["gemini"] = keys
        save_api_config(config)
        logging.info(f"Ключ {key_to_remove[:10]}... удален (нет бабок/лимитов).")
