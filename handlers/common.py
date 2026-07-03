import asyncio
import re
import uuid
import tempfile
import os
import time
import io
import json
import zipfile
import subprocess
import sys
import logging
import random
import secrets
import posixpath
from urllib.parse import unquote
from html.parser import HTMLParser
from typing import Any

from aiogram import types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import (
    IMAGE_COOLDOWN_SECONDS,
    TEXT_COOLDOWN_SECONDS,
    DELETE_MESSAGE_DELAY_SECONDS,
    TEXT_ONLY_CHAT_ID,
    FULL_ACCESS_CHAT_ID,
    FULL_ACCESS_CHAT_IMAGE_COOLDOWN,
    PAYMENT_PHONE,
    ALLOWED_USER_IDS,
    OWNER_USER_ID,
    ADMIN_IDS,
    DAILY_GEN_LIMIT,
    PAYMENT_USERNAME,
    CHAT_ID,
    GEMINI_IMAGE_TIMEOUT,
)

from state import (
    pending_image_requests,
    pending_video_requests,
    pending_media_groups,
    user_image_cooldowns,
    user_text_cooldowns,
    user_video_cooldowns,
    full_access_image_cooldowns,
    paid_unlimited_until,
    pending_prompt_requests,
    pending_nsfw_configs,
    chat_members_cache,
    daily_gen_limits,
    banned_user_ids,
    chat_custom_limits,
    pending_tts_requests,
    pending_tts_configs,
    pending_file_tasks,
    tts_voice_previews,
    generated_draw_messages,
    generated_code_messages,
)

logger = logging.getLogger(__name__)

MAX_DOCUMENT_UPLOAD_BYTES = 5_000_000


async def _ensure_image_generation_allowed(message: types.Message) -> bool:
    from utils import check_membership
    from handlers.admin import _check_daily_limit
    if not await check_membership(message.bot, message.from_user.id, message.chat.id):
        return False
    allowed, limit_msg = _check_daily_limit(message.from_user.id, message.chat.id)
    if not allowed:
        await message.reply(limit_msg)
        return False
    return True


async def _download_message_photo(bot, message: types.Message):
    if not message or not message.photo:
        return None
    try:
        file_info = await bot.get_file(message.photo[-1].file_id)
        downloaded = await bot.download_file(file_info.file_path)
        return downloaded.read()
    except Exception as e:
        logger.warning(f'Ошибка скачивания фото сообщения: {e}')
        return None
MAX_TEXT_DOCUMENT_BYTES = 80_000
MAX_ZIP_TEXT_BYTES = 200_000
MAX_ZIP_TEXT_FILE_BYTES = 50_000
MAX_ZIP_FILES = 20
MAX_ZIP_DECLARED_BYTES = 5_000_000
MAX_ZIP_ENTRY_DECLARED_BYTES = 1_000_000

SAFE_TEXT_EXTS = {
    '.txt', '.md', '.markdown', '.py', '.js', '.jsx', '.ts', '.tsx', '.json', '.yaml', '.yml', '.csv', '.html', '.htm', '.xml',
    '.css', '.sh', '.bash', '.env', '.ini', '.cfg', '.conf', '.toml', '.sql', '.log', '.rs', '.go', '.java', '.kt', '.kts',
    '.swift', '.php', '.rb', '.c', '.h', '.cpp', '.hpp', '.cs', '.lua', '.dart', '.vue', '.svelte'
}

SAFE_TEXT_MIMES = {
    'text/plain', 'text/csv', 'text/markdown', 'text/html', 'text/css', 'text/xml', 'application/json', 'application/xml',
    'application/x-yaml', 'application/yaml', 'application/toml', 'application/javascript', 'application/x-sh', 'application/sql'
}

_MAX_TRACKED_DRAW_MESSAGES = 120
_MAX_TRACKED_CODE_MESSAGES = 60

_WEB_SEARCH_DIRECTIVE = 'WEB_SEARCH:'
_KICK_DIRECTIVE = 'KICK_USER:'

_KIRIESHKI_CHAT_ID = -1002830734467
_KIRIESHKI_STICKER_SET = 'kirieshkikirieshki'
_KIRIESHKI_STICKER_CHANCE = 0.10
_PERMA_STICKER_SET = 'SHCHperma9740'
_PERMA_STICKER_CHANCE = 0.05
_KIRIESHKI_STICKER_CACHE_TTL = 86400
_kirieshki_sticker_file_ids: list[str] = []
_kirieshki_sticker_cache_ts = 0.0
_perma_sticker_file_ids: list[str] = []
_perma_sticker_cache_ts = 0.0
_RANDOM_GIF_CHANCE = 0.05
_RANDOM_MEDIA_MIN_INTERVAL = 30
_random_media_last_ts_by_chat: dict[int, float] = {}

_RANDOM_GIF_PATHS = [
    os.path.join(os.path.dirname(os.path.dirname(__file__)), 'media', 'random_gifs', 'kirieshki_1.mp4'),
    os.path.join(os.path.dirname(os.path.dirname(__file__)), 'media', 'random_gifs', 'kirieshki_2.mp4'),
    os.path.join(os.path.dirname(os.path.dirname(__file__)), 'media', 'random_gifs', 'kirieshki_3.mp4'),
    os.path.join(os.path.dirname(os.path.dirname(__file__)), 'media', 'random_gifs', 'kirieshki_4.mp4'),
]

_TEMP_OPTIONS = [
    (0.1, '🎯 Точный', 'строго следует промпту, почти без вариаций'),
    (0.5, '⚖️ Умеренный', 'баланс точности и разнообразия'),
    (1.0, '✨ Стандарт', 'стандартная генерация (по умолчанию)'),
    (1.5, '🎨 Творческий', 'больше вариативности и интерпретации'),
    (2.0, '🌀 Безумный', 'максимальная непредсказуемость')
]

_NSFW_STEPS = [20, 25, 28, 35, 50]
_NSFW_CFG = [5.0, 6.5, 7.0, 8.5, 10.0]
_NSFW_SIZES = ['512x768', '768x1024', '896x1152', '1024x1024', '1024x1536']
_NSFW_DEFAULT_NEG = 'lowres, bad anatomy, bad hands, text, error, missing fingers, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, blurry'
_NSFW_SCHEDULERS = ['DPM++ 2M Karras', 'DPM++ 2M SDE Karras', 'DPM++ SDE Karras', 'DPM++ 2M', 'DPM++ 2M SDE', 'DPM++ SDE', 'Euler a', 'Euler', 'DDIM', 'DDPM', 'DPM2 Karras', 'DPM2 a Karras', 'DPM2', 'DPM2 a', 'LMS Karras', 'LMS', 'UniPC', 'Heun', 'PNDM', 'DEIS']
_NSFW_CLIP_SKIP = [1, 2, 3]
_NSFW_PAG = [0, 1, 2, 3, 5]
_NSFW_RESCALE = [0.5, 0.7, 1.0]

_TTS_LANGS = [('🇷🇺 RU', 'ru-RU'), ('🇬🇧 EN', 'en-US'), ('🇯🇵 JA', 'ja-JP')]
_TTS_TEMPS = [0.1, 0.5, 1.0, 1.5, 2.0]
TTS_VOICES = ['Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede', 'Callirrhoe', 'Autonoe', 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba', 'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar', 'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi', 'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat']

# ── Keyboard layout definitions ──────────────────────────────────────────
# Each layout tuple: (kind, wai_only?, payload)
#   kind='actions':    payload = ((text, input_suffix), …)          → input buttons row
#   kind='header':     payload = text                               → noop header row
#   kind='options':    payload = (field, values)                    → option buttons row
#   kind='chunked':    payload = (field, values, chunk_size)        → chunked option rows
#   kind='special_wai': payload = None                              → prepend+seed row (WAI only)
#   kind='preview':    payload = text                               → tts preview button
#   kind='generate':   payload = text                               → generate button

_NSFW_CFG_DEFAULTS: dict[str, Any] = {
    'steps': 28, 'cfg': 7.0, 'size': '896x1152',
    'batch': 1, 'scheduler': 'DPM++ 2M Karras',
    'clip_skip': 2, 'pag_scale': 0, 'rescale': 1.0,
    'prepend': True, 'seed': -1,
}

_NSFW_KEYBOARD_LAYOUT = (
    ('actions',      False, (('✏️ Промпт', 'prompt'), ('🚫 Негативный', 'neg'))),
    ('header',       False, '— Шаги —'),
    ('options',      False, ('steps',     _NSFW_STEPS)),
    ('header',       False, '— CFG Scale —'),
    ('options',      False, ('cfg',       _NSFW_CFG)),
    ('header',       False, '— Размер —'),
    ('chunked',      False, ('size',      _NSFW_SIZES, 3)),
    ('header',       True,  '— Количество изображений —'),
    ('options',      True,  ('batch',     tuple(range(1, 5)))),
    ('header',       True,  '— Планировщик (Sampler) —'),
    ('chunked',      True,  ('scheduler', _NSFW_SCHEDULERS, 3)),
    ('header',       True,  '— CLIP Skip —'),
    ('options',      True,  ('clip_skip', _NSFW_CLIP_SKIP)),
    ('header',       True,  '— PAG Scale (улучшение качества) —'),
    ('options',      True,  ('pag_scale', _NSFW_PAG)),
    ('header',       True,  '— Guidance Rescale —'),
    ('options',      True,  ('rescale',   _NSFW_RESCALE)),
    ('special_wai',  True,  None),
    ('generate',     False, '🚀 Генерировать'),
)

_TTS_CFG_DEFAULTS: dict[str, Any] = {
    'voice': 'Puck', 'temp': 1.0, 'lang': 'ru-RU',
}

_TTS_KEYBOARD_LAYOUT = (
    ('actions',  False, (('✏️ Текст', 'prompt'), ('🎭 Сцена', 'scene'))),
    ('actions',  False, (('🎭 Стиль', 'style'), ('🎭 Темп', 'pace'), ('🎭 Акцент', 'accent'))),
    ('header',   False, '— Температура —'),
    ('options',  False, ('temp',  _TTS_TEMPS)),
    ('header',   False, '— Язык —'),
    ('options',  False, ('lang',  _TTS_LANGS)),
    ('header',   False, '— Голос —'),
    ('chunked',  False, ('voice', TTS_VOICES, 3)),
    ('preview',  False, '🔊 Прослушать голос'),
    ('generate', False, '🚀 Генерировать'),
)

async def safe_send(coro_func, *args, **kwargs):
    for attempt in range(3):
        try:
            return await coro_func(*args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if "retry after" in err_str or "flood" in err_str or "too many requests" in err_str:
                import re as _re
                secs_match = _re.search(r'retry after (\d+)', err_str)
                secs = int(secs_match.group(1)) if secs_match else 10
                logging.warning(f"Telegram Flood Control hit. Waiting {secs}s before retry...")
                await asyncio.sleep(secs)
            else:
                raise

def _clean_plain_reply(text: str) -> str:
    text = re.sub(r'</?(?:b|strong|i|em|u|s|code|pre|blockquote|a)(?:\s+[^>]*)?>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^\s{0,3}#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = text.replace('```', '').replace('`', '')
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*\n]+)\*', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def _project_filename(name: str, fallback: str = 'project') -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_.-]+', '_', name.strip())[:80].strip('._-')
    return cleaned or fallback

def _validate_generated_files(files: list[dict[str, str]]) -> tuple[bool, list[str]]:
    errors = []
    for file_item in files:
        path = file_item['path']
        content = file_item['content']
        ext = os.path.splitext(path.lower())[1]
        try:
            if ext == '.py':
                proc = subprocess.run(
                    [sys.executable, '-c', 'import sys; compile(sys.stdin.read(), sys.argv[1], "exec")', path],
                    input=content,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if proc.returncode != 0:
                    errors.append(f'{path}: {proc.stderr.strip()[:300] or "Python syntax error"}')
            elif ext == '.json':
                json.loads(content)
            elif ext in ('.html', '.htm'):
                parser = HTMLParser()
                parser.feed(content)
                lowered = content.lower()
                if '<meta charset=' not in lowered and "charset='utf-8'" not in lowered and 'charset="utf-8"' not in lowered and 'charset=utf-8' not in lowered:
                    errors.append(f'{path}: нет meta charset')
        except subprocess.TimeoutExpired:
            errors.append(f'{path}: Python compile timeout')
        except Exception as e:
            errors.append(f'{path}: {type(e).__name__}: {e}')
    return (not errors, errors)

def _fallback_generation_error_explanation(error_msg: str) -> str:
    lowered = (error_msg or '').lower()
    if 'ключ' in lowered or 'quota' in lowered or '429' in lowered or 'исчерпан' in lowered:
        return 'Ключи или квота на генерацию сейчас сдохли. Это не твой промпт, это сервисы опять легли мордой в пол.'
    if 'policy' in lowered or 'safety' in lowered or 'цензур' in lowered or 'blocked' in lowered:
        return 'Похоже, генератор словил цензуру или safety-фильтр. Переформулируй мягче, а то он обосрался раньше меня.'
    return 'Генератор вернул ошибку, а мозги для пояснения тоже не ответили. Короче, сервис тупит — попробуй позже или упрости запрос.'

def _is_text_filename(filename: str) -> bool:
    return os.path.splitext((filename or '').lower())[1] in SAFE_TEXT_EXTS

def _is_text_mime(mime: str) -> bool:
    mime = (mime or '').split(';', 1)[0].strip().lower()
    return mime.startswith('text/') or mime in SAFE_TEXT_MIMES

def _is_zip_document(filename: str, mime: str) -> bool:
    filename = (filename or '').lower()
    mime = (mime or '').split(';', 1)[0].strip().lower()
    return filename.endswith('.zip') or mime in {'application/zip', 'application/x-zip-compressed', 'multipart/x-zip'}

def _looks_binary(data: bytes) -> bool:
    sample = data[:4096]
    if not sample:
        return False
    if b'\x00' in sample:
        return True
    control_chars = sum(1 for byte in sample if byte < 32 and byte not in (9, 10, 12, 13))
    return control_chars / max(len(sample), 1) > 0.05

def _decode_text_payload(data: bytes, max_bytes: int = MAX_TEXT_DOCUMENT_BYTES) -> str | None:
    if _looks_binary(data):
        return None
    truncated = len(data) > max_bytes
    chunk = data[:max_bytes]
    for encoding in ('utf-8-sig', 'utf-8', 'cp1251', 'latin-1'):
        try:
            text = chunk.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = chunk.decode('utf-8', errors='replace')
    if truncated:
        text += '\n\n[...файл обрезан по лимиту...]'
    return text.strip()

def _extract_text_from_document(data: bytes, filename: str) -> str:
    text = _decode_text_payload(data, MAX_TEXT_DOCUMENT_BYTES)
    if text is None:
        raise ValueError(f'«{filename}» похож на бинарный мусор, такое я читать не буду.')
    if not text:
        raise ValueError(f'«{filename}» пустой, там нечего жевать.')
    return text

def _safe_zip_name(name: str) -> str:
    cleaned = (name or '').replace('\\', '/').strip()
    if not cleaned or cleaned.startswith('/') or os.path.isabs(cleaned):
        raise ValueError('В zip найден опасный путь, архив отклонён.')
    parts = [part for part in cleaned.split('/') if part]
    if any(part == '..' for part in parts):
        raise ValueError('В zip найден path traversal через .., архив отклонён.')
    normalized = os.path.normpath('/'.join(parts)).replace('\\', '/')
    if normalized == '.' or normalized.startswith('../') or normalized == '..':
        raise ValueError('В zip найден опасный путь, архив отклонён.')
    return normalized

def _extract_zip_contents(data: bytes) -> str:
    parts = []
    skipped = []
    total_text_bytes = 0
    declared_bytes = 0
    files_read = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                safe_name = _safe_zip_name(info.filename)
                if info.is_dir():
                    continue
                declared_bytes += int(info.file_size or 0)
                if declared_bytes > MAX_ZIP_DECLARED_BYTES:
                    raise ValueError('Zip слишком жирный после распаковки, похоже на zip-bomb.')
                if info.file_size and info.file_size > MAX_ZIP_ENTRY_DECLARED_BYTES:
                    skipped.append(f'{safe_name}: слишком большой файл')
                    continue
                if files_read >= MAX_ZIP_FILES:
                    skipped.append('остальные файлы: лимит по количеству')
                    break
                if not _is_text_filename(safe_name):
                    skipped.append(f'{safe_name}: не текстовый тип')
                    continue
                remaining_total = MAX_ZIP_TEXT_BYTES - total_text_bytes
                if remaining_total <= 0:
                    skipped.append('остальные файлы: лимит по общему тексту')
                    break
                read_limit = min(MAX_ZIP_TEXT_FILE_BYTES, remaining_total)
                with archive.open(info) as file_obj:
                    raw = file_obj.read(read_limit + 1)
                text = _decode_text_payload(raw, read_limit)
                if text is None:
                    skipped.append(f'{safe_name}: бинарный мусор')
                    continue
                if not text:
                    skipped.append(f'{safe_name}: пустой')
                    continue
                encoded_len = len(raw[:read_limit])
                total_text_bytes += encoded_len
                files_read += 1
                parts.append(f'# {safe_name}\n{text}')
                if len(raw) > read_limit:
                    skipped.append(f'{safe_name}: обрезан по лимиту')
    except zipfile.BadZipFile as e:
        raise ValueError('Zip битый или это вообще не zip.') from e
    if not parts:
        details = (' Пропущено: ' + '; '.join(skipped[:6])) if skipped else ''
        raise ValueError('В zip нет читаемых текстовых файлов.' + details)
    if skipped:
        parts.append('[Пропущено]\n' + '\n'.join(f'- {item}' for item in skipped[:12]))
    return '\n\n'.join(parts)

def _code_block_ext(lang: str) -> str:
    ext = (lang or '').strip().lower() or 'txt'
    if ext in ['python', 'py']:
        return 'py'
    if ext in ['javascript', 'js']:
        return 'js'
    if ext in ['typescript', 'ts']:
        return 'ts'
    if ext in ['html', 'htm']:
        return 'html'
    if ext in ['css']:
        return 'css'
    if ext in ['c++', 'cpp']:
        return 'cpp'
    if ext in ['c#', 'cs']:
        return 'cs'
    if ext in ['php']:
        return 'php'
    if ext in ['bash', 'sh']:
        return 'sh'
    if ext in ['json']:
        return 'json'
    if ext in ['xml']:
        return 'xml'
    return re.sub(r'[^a-z0-9]+', '', ext)[:8] or 'txt'

def _has_kick_execution_signal(message, reason: str, rest: str) -> bool:
    source = f'{message.text or message.caption or ""}\n{reason}\n{rest}'.casefold()
    explicit_target = bool(message.reply_to_message) or bool(re.search(r'@[A-Za-z0-9_]{3,32}', source))
    kick_words = ('кик', 'кикн', 'выкин', 'вышвыр', 'kick')
    owner_words = ('я ревикс', 'я rewix', 'я rewixx', 'я rewi', 'я владелец', 'я создатель', 'притворяется rewix', 'притворяется ревикс', 'косит под rewix', 'косит под ревикс', 'закос под rewix', 'закос под ревикс')
    severe_words = ('спам', 'флуд', 'скам', 'реклама', 'бот-спам', 'докс', 'деанон', 'угроз', 'рейд')
    return any(word in source for word in owner_words) or any(word in source for word in severe_words) or (explicit_target and any(word in source for word in kick_words))

def _is_code_generation_request(prompt: str) -> bool:
    lower_prompt = prompt.lower()
    direct_phrases = ['напиши код', 'напиши скрипт', 'сделай скрипт', 'напиши программу', 'напиши функцию', 'создай скрипт', 'создай код', 'напиши бота', 'сделай бота', 'напиши сайт', 'сделай сайт', 'напиши программу', 'создай сайт', 'создай приложение', 'создай проект', 'сделай проект', 'напиши проект', 'собери проект', 'новый мессенджер', 'напиши парсер', 'сделай парсер', 'напиши апи', 'сделай апи', 'напиши api', 'напиши хэндлер', 'реализуй', 'write code', 'write a script', 'write a bot', 'write a site', 'write a website', 'write an app', 'create project', 'build project']
    if any(phrase in lower_prompt for phrase in direct_phrases):
        return True
    talk_only_markers = ['что такое', 'как работает', 'объясни', 'расскажи', 'найди', 'поищи', 'загугли', 'что нового', 'почему', 'зачем', 'кто такой', 'что за']
    if any(marker in lower_prompt for marker in talk_only_markers):
        return False
    actions = ['напиши', 'сделай', 'создай', 'собери', 'накидай', 'скинь', 'кинь', 'дай', 'нужен', 'нужна', 'нужно']
    artifacts = ['код', 'скрипт', 'проект', 'сайт', 'бот', 'приложение', 'прогу', 'программа', 'zip', 'зип', 'архив', 'pydroid', 'pydroid3', '.py']
    return any(action in lower_prompt for action in actions) and any(artifact in lower_prompt for artifact in artifacts)

def _remember_generated_draw_message(chat_id: int, message_id: int, image_bytes: bytes, prompt: str, user_id: int, username: str='', first_name: str='Аноним'):
    if not image_bytes:
        return
    generated_draw_messages[(chat_id, message_id)] = {
        'image_bytes': image_bytes,
        'prompt': prompt or '',
        'user_id': user_id,
        'username': username or '',
        'first_name': first_name or 'Аноним',
        'created_at': time.time(),
    }
    while len(generated_draw_messages) > _MAX_TRACKED_DRAW_MESSAGES:
        oldest_key = min(generated_draw_messages, key=lambda k: generated_draw_messages[k].get('created_at', 0))
        generated_draw_messages.pop(oldest_key, None)

def _remember_generated_code_message(chat_id: int, message_id: int, files: list[dict], prompt: str):
    if not files:
        return
    truncated = [{'path': f['path'], 'content': f['content'][:8000]} for f in files[:6]]
    generated_code_messages[(chat_id, message_id)] = {
        'files': truncated,
        'prompt': prompt or '',
        'created_at': time.time(),
    }
    while len(generated_code_messages) > _MAX_TRACKED_CODE_MESSAGES:
        oldest_key = min(generated_code_messages, key=lambda k: generated_code_messages[k].get('created_at', 0))
        generated_code_messages.pop(oldest_key, None)

async def delete_message_after_delay(bot, chat_id: int, message_id: int, delay: int=DELETE_MESSAGE_DELAY_SECONDS):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f'Не удалось удалить сообщение {message_id}: {e}')

async def run_progress_bar(bot, chat_id: int, message_id: int, model_label: str, state_data: dict = None):
    BAR_LEN = 10
    start = asyncio.get_event_loop().time()
    pos = 0
    while True:
        elapsed = int(asyncio.get_event_loop().time() - start)
        if elapsed < 60:
            time_str = f'00:{elapsed:02d}'
        else:
            time_str = f'{elapsed // 60}:{elapsed % 60:02d}'
        filled = pos % BAR_LEN
        bar = '■' * filled + '□' * (BAR_LEN - filled)
        status_info = f"\nℹ️ {state_data['status']}" if state_data and state_data.get('status') else ""
        text = f'⏳ Генерация...\n[{bar}]\nПрошло: {time_str}\nМодель: {model_label}{status_info}'
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        except Exception as e:
            logger.warning(f'Ошибка обновления прогресс-бара: {e}')
        await asyncio.sleep(random.uniform(3.5, 5.5))
        pos += 1

async def _send_kirieshki_sticker(message: types.Message) -> bool:
    global _kirieshki_sticker_file_ids, _kirieshki_sticker_cache_ts
    if message.chat.id != _KIRIESHKI_CHAT_ID:
        return False
    bot = message.bot
    if bot is None:
        return False
    try:
        now = time.monotonic()
        if not _kirieshki_sticker_file_ids or now - _kirieshki_sticker_cache_ts > _KIRIESHKI_STICKER_CACHE_TTL:
            sticker_set = await bot.get_sticker_set(name=_KIRIESHKI_STICKER_SET)
            _kirieshki_sticker_file_ids = [sticker.file_id for sticker in sticker_set.stickers if sticker.file_id]
            _kirieshki_sticker_cache_ts = now
        if _kirieshki_sticker_file_ids:
            sent = await safe_send(message.reply_sticker, sticker=random.choice(_kirieshki_sticker_file_ids))
            return bool(sent)
    except Exception as e:
        logging.warning(f'Kirieshki sticker reply failed: {type(e).__name__}: {e}')
    return False

async def _send_perma_sticker(message: types.Message) -> bool:
    global _perma_sticker_file_ids, _perma_sticker_cache_ts
    if message.chat.id != _KIRIESHKI_CHAT_ID:
        return False
    bot = message.bot
    if bot is None:
        return False
    try:
        now = time.monotonic()
        if not _perma_sticker_file_ids or now - _perma_sticker_cache_ts > _KIRIESHKI_STICKER_CACHE_TTL:
            sticker_set = await bot.get_sticker_set(name=_PERMA_STICKER_SET)
            _perma_sticker_file_ids = [sticker.file_id for sticker in sticker_set.stickers if sticker.file_id]
            _perma_sticker_cache_ts = now
        if _perma_sticker_file_ids:
            sent = await safe_send(message.reply_sticker, sticker=random.choice(_perma_sticker_file_ids))
            return bool(sent)
    except Exception as e:
        logging.warning(f'Perma sticker reply failed: {type(e).__name__}: {e}')
    return False

async def _send_random_gif(message: types.Message) -> bool:
    if message.chat.id != _KIRIESHKI_CHAT_ID:
        return False
    path = random.choice(_RANDOM_GIF_PATHS)
    if not os.path.exists(path):
        logging.warning(f'Random gif file missing: {path}')
        return False
    try:
        sent = await safe_send(message.reply_animation, animation=FSInputFile(path))
        return bool(sent)
    except Exception as e:
        logging.warning(f'Random gif reply failed: {type(e).__name__}: {e}')
    return False

async def _maybe_send_random_chat_media(message: types.Message):
    if message.chat.id != _KIRIESHKI_CHAT_ID:
        return
    now = time.monotonic()
    if now - _random_media_last_ts_by_chat.get(message.chat.id, 0.0) < _RANDOM_MEDIA_MIN_INTERVAL:
        return
    roll = secrets.randbelow(100)
    sticker_threshold = int(_KIRIESHKI_STICKER_CHANCE * 100)
    perma_sticker_threshold = sticker_threshold + int(_PERMA_STICKER_CHANCE * 100)
    gif_threshold = perma_sticker_threshold + int(_RANDOM_GIF_CHANCE * 100)
    sent = False
    if roll < sticker_threshold:
        sent = await _send_kirieshki_sticker(message)
    elif roll < perma_sticker_threshold:
        sent = await _send_perma_sticker(message)
    elif roll < gif_threshold:
        sent = await _send_random_gif(message)
    if sent:
        _random_media_last_ts_by_chat[message.chat.id] = now

def _track_user(message: types.Message):
    if message.from_user and message.chat.type != 'private':
        cid = message.chat.id
        uid = message.from_user.id
        if cid not in chat_members_cache:
            chat_members_cache[cid] = {}
        chat_members_cache[cid][uid] = (message.from_user.first_name or 'Аноним', message.from_user.username)

def _temp_message() -> str:
    lines = ['🌡️ Выберите температуру генерации:\n', 'Температура влияет на то, насколько точно ИИ следует промпту.', 'Диапазон: 0.1 (точно) — 2.0 (творческий хаос)\n']
    for (val, label, desc) in _TEMP_OPTIONS:
        lines.append(f'{label} — {desc}')
    return '\n'.join(lines)

def _temp_keyboard(request_id: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f'{label} ({val})', callback_data=f'ptmp:{request_id}:{i}')] for (i, (val, label, _)) in enumerate(_TEMP_OPTIONS)]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _providers_keyboard(request_id: str, chat_id: int, photo_count: int=0) -> InlineKeyboardMarkup:
    providers = ['gemini', 'flux', 'nsfw'] if chat_id == TEXT_ONLY_CHAT_ID else ['gemini', 'gpt', 'flux', 'nsfw']
    labels = {'gemini': 'Gemini', 'gpt': 'GPT', 'flux': 'FLUX', 'nsfw': 'Replicate'}
    photo_label = f' 📎{photo_count} фото' if photo_count > 1 else ''
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=labels[p] + (photo_label if p not in ('flux', 'nsfw') else ''), callback_data=f'imgprov:{request_id}:{p}') for p in providers]])

def _nsfw_default_cfg(model: str) -> dict:
    if 'flux' in model:
        return {'steps': 28, 'cfg': 3.5, 'size': '1024x1024', 'neg': ''}
    return {'steps': 28, 'cfg': 7.0, 'size': '896x1152', 'neg': _NSFW_DEFAULT_NEG, 'scheduler': 'DPM++ 2M Karras', 'clip_skip': 2, 'pag_scale': 0, 'rescale': 1.0, 'prepend': True, 'seed': -1, 'batch': 1}

def _nsfw_cfg_text(request_id: str) -> str:
    d = pending_nsfw_configs.get(request_id, {})
    cfg = d.get('cfg', {})
    prompt = d.get('prompt', '')[:100]
    neg_full = cfg.get('neg', '')
    neg = neg_full[:150]
    label = d.get('label', 'NSFW')
    neg_display = f'''"{neg}{('...' if len(neg_full) > 150 else '')}"''' if neg_full else 'не задан (стандартный)'
    is_wai = 'flux' not in d.get('model', '')
    extra = ''
    if is_wai:
        extra = f"\n\nКол-во: {cfg.get('batch', 1)} шт  |  Планировщик: {cfg.get('scheduler', 'DPM++ 2M Karras')}\nCLIP Skip: {cfg.get('clip_skip', 2)}  |  PAG: {cfg.get('pag_scale', 0)}  |  Rescale: {cfg.get('rescale', 1.0)}\nПрепромпт: {('✅' if cfg.get('prepend', True) else '❌')}  |  Seed: {cfg.get('seed', -1)}"
    return f'''⚙️ {label}\n\n📝 Промпт:\n"{prompt}"\n\n🚫 Негативный промпт:\n{neg_display}\n\nШаги: {cfg.get('steps', 28)}  |  CFG: {cfg.get('cfg', 7.0)}  |  Размер: {cfg.get('size', '896x1152')}{extra}'''

def _nsfw_cfg_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Build NSFW config keyboard from layout data."""
    d = pending_nsfw_configs.get(request_id, {})
    cfg = d.get('cfg', {})
    is_wai = 'flux' not in d.get('model', '')

    def _norm_opt(v):
        """Normalise an option value to (display, callback_value)."""
        return v if isinstance(v, tuple) else (str(v), v)

    def _checked(cb_val, current):
        return '✅' if str(cb_val) == str(current) else ''

    def _option_row(field, values, current):
        return [InlineKeyboardButton(
            text=f'{_checked(_norm_opt(v)[1], current)}{_norm_opt(v)[0]}',
            callback_data=f'nsfwcfg:{request_id}:{field}:{_norm_opt(v)[1]}',
        ) for v in values]

    rows: list[list[InlineKeyboardButton]] = []
    for kind, wai_only, payload in _NSFW_KEYBOARD_LAYOUT:
        if wai_only and not is_wai:
            continue
        if kind == 'actions':
            rows.append([InlineKeyboardButton(text=t, callback_data=f'nsfwinput:{request_id}:{s}')
                         for t, s in payload])
        elif kind == 'header':
            rows.append([InlineKeyboardButton(text=payload, callback_data='noop')])
        elif kind == 'options':
            field, values = payload
            rows.append(_option_row(field, values, cfg.get(field, _NSFW_CFG_DEFAULTS[field])))
        elif kind == 'chunked':
            field, values, chunk = payload
            cur = cfg.get(field, _NSFW_CFG_DEFAULTS[field])
            for i in range(0, len(values), chunk):
                rows.append(_option_row(field, values[i:i + chunk], cur))
        elif kind == 'special_wai':
            cur_pre = cfg.get('prepend', True)
            cur_seed = cfg.get('seed', -1)
            rows.append([
                InlineKeyboardButton(
                    text=f"{'✅' if cur_pre else '❌'} Препромпт качества",
                    callback_data=f"nsfwcfg:{request_id}:prepend:{'0' if cur_pre else '1'}",
                ),
                InlineKeyboardButton(
                    text=f'🎲 Seed: {cur_seed}',
                    callback_data=f'nsfwinput:{request_id}:seed',
                ),
            ])
        elif kind == 'generate':
            rows.append([InlineKeyboardButton(text=payload, callback_data=f'nsfwgen:{request_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _prompt_ai_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Использовать этот промт', callback_data=f'puse:{request_id}')], [InlineKeyboardButton(text='🔄 Другой вариант', callback_data=f'pother:{request_id}'), InlineKeyboardButton(text='📝 Мой промт', callback_data=f'pbase:{request_id}')]])

def _tts_cfg_text(request_id: str) -> str:
    d = pending_tts_configs.get(request_id, {})
    cfg = d.get('cfg', {})
    prompt = d.get('prompt', '')[:100]
    label = d.get('label', 'TTS')
    scene = cfg.get('scene', '')[:50]
    style = cfg.get('style', '')[:50]
    pace = cfg.get('pace', '')[:50]
    accent = cfg.get('accent', '')[:50]
    extra = ''
    if scene:
        extra += f'\n🎭 Сцена: "{scene}..."'
    if style:
        extra += f'\n🎭 Стиль: "{style}..."'
    if pace:
        extra += f'\n🎭 Темп: "{pace}..."'
    if accent:
        extra += f'\n🎭 Акцент: "{accent}..."'
    return f'''🎙️ {label}\n\n📝 Текст:\n"{prompt}"\n\nГолос: {cfg.get('voice', 'Puck')}\nТемпература: {cfg.get('temp', 1.0)}\nЯзык: {cfg.get('lang', 'ru-RU')}{extra}'''

def _tts_cfg_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Build TTS config keyboard from layout data."""
    d = pending_tts_configs.get(request_id, {})
    cfg = d.get('cfg', {})

    def _norm_opt(v):
        """Normalise an option value to (display, callback_value)."""
        return v if isinstance(v, tuple) else (str(v), v)

    def _checked(cb_val, current):
        return '✅' if str(cb_val) == str(current) else ''

    def _option_row(field, values, current):
        return [InlineKeyboardButton(
            text=f'{_checked(_norm_opt(v)[1], current)}{_norm_opt(v)[0]}',
            callback_data=f'ttscfg:{request_id}:{field}:{_norm_opt(v)[1]}',
        ) for v in values]

    rows: list[list[InlineKeyboardButton]] = []
    for kind, _wai, payload in _TTS_KEYBOARD_LAYOUT:
        if kind == 'actions':
            rows.append([InlineKeyboardButton(text=t, callback_data=f'ttsinput:{request_id}:{s}')
                         for t, s in payload])
        elif kind == 'header':
            rows.append([InlineKeyboardButton(text=payload, callback_data='noop')])
        elif kind == 'options':
            field, values = payload
            rows.append(_option_row(field, values, cfg.get(field, _TTS_CFG_DEFAULTS[field])))
        elif kind == 'chunked':
            field, values, chunk = payload
            cur = cfg.get(field, _TTS_CFG_DEFAULTS[field])
            for i in range(0, len(values), chunk):
                rows.append(_option_row(field, values[i:i + chunk], cur))
        elif kind == 'preview':
            rows.append([InlineKeyboardButton(text=payload, callback_data=f'ttsprev:{request_id}')])
        elif kind == 'generate':
            rows.append([InlineKeyboardButton(text=payload, callback_data=f'ttsgen:{request_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)
