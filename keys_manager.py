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

    if not openai_value:
        openai_matches = list(dict.fromkeys(re.findall(r'sk-[A-Za-z0-9_-]{20,}', content)))
        openai_value = openai_matches[0] if openai_matches else ""

    return {
        "gemini": gemini_keys,
        "openai": openai_value
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

def load_openai_key():
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key:
        return key

    if OPENAI_API_KEY.strip():
        return OPENAI_API_KEY.strip()

    try:
        openai_data = load_api_config().get("openai", "")
        if isinstance(openai_data, str):
            return openai_data.strip()
        if isinstance(openai_data, list) and openai_data:
            return str(openai_data[0]).strip()
    except Exception as e:
        logging.error(f"Ошибка загрузки OpenAI ключа: {e}")

    return ""

def remove_key(key_to_remove):
    config = load_api_config()
    keys = config.get("gemini", [])
    if key_to_remove in keys:
        keys.remove(key_to_remove)
        config["gemini"] = keys
        save_api_config(config)
        logging.info(f"Ключ {key_to_remove[:10]}... удален (нет бабок/лимитов).")
