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
import secrets
from html.parser import HTMLParser
from typing import Any
from aiogram import Router, F, types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from state import pending_image_requests, pending_video_requests, pending_media_groups, user_image_cooldowns, user_text_cooldowns, user_video_cooldowns, full_access_image_cooldowns, paid_unlimited_until, pending_prompt_requests, pending_nsfw_configs, chat_members_cache, daily_gen_limits, banned_user_ids, chat_custom_limits, pending_tts_requests, pending_tts_configs, pending_file_tasks, tts_voice_previews, generated_draw_messages, generated_code_messages
from database import save_history, save_pending_gen, delete_pending_gen, add_user_stat, get_user_stats, add_banned_user_db, remove_banned_user_db, log_prompt, get_recent_prompts
from ai_services import start_veo_generation, poll_veo_operation
from utils import check_membership, is_banned, make_safe_caption
from ai_services import generate_image_with_gpt, generate_image_with_gemini, generate_image_with_nvidia, generate_image_with_openrouter, generate_video_with_veo, explain_generation_error, generate_video_with_gemini, generate_text_with_gemini, classify_code_intent_with_gemini, classify_draw_intent_with_gemini, generate_image_via_code, upscale_image, generate_image_prompt, generate_code_with_gemini, generate_project_with_gemini, fetch_gemini_image_models, fetch_openai_image_models, fetch_veo_models, generate_image_with_replicate, fetch_gemini_tts_models, generate_tts_with_gemini, fetch_replicate_image_models, generate_bull_roast, analyze_photo_with_gemini, analyze_voice_with_gemini
from agent import run_agent, classify_agent_intent
from config import IMAGE_COOLDOWN_SECONDS, TEXT_COOLDOWN_SECONDS, DELETE_MESSAGE_DELAY_SECONDS, TEXT_ONLY_CHAT_ID, FULL_ACCESS_CHAT_ID, FULL_ACCESS_CHAT_IMAGE_COOLDOWN, PAYMENT_PHONE, ALLOWED_USER_IDS, OWNER_USER_ID, DAILY_GEN_LIMIT, PAYMENT_USERNAME, CHAT_ID, GEMINI_IMAGE_TIMEOUT
import logging
logger = logging.getLogger(__name__)
router = Router()

MAX_DOCUMENT_UPLOAD_BYTES = 5_000_000
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


async def _send_generated_project(message: types.Message, project: dict[str, Any], reply_kwargs: dict[str, Any]) -> types.Message | None:
    files = project.get('files', [])
    if not isinstance(files, list):
        files = []
    is_valid, validation_errors = _validate_generated_files(files)
    project_name = _project_filename(str(project.get('project_name', 'project')))
    summary = _clean_plain_reply(str(project.get('summary', 'Собрал файлы проекта.')))
    instructions = _clean_plain_reply(str(project.get('run_instructions', '')))
    status = '✅ Проверил через Python/парсеры — синтаксис живой.' if is_valid else '⚠️ Собрал, но проверка нашла косяки:\n' + '\n'.join(validation_errors[:5])
    caption_parts = [summary, status]
    if instructions:
        caption_parts.append('Запуск: ' + instructions)
    caption = '\n\n'.join(caption_parts)[:1000]
    bot = message.bot
    if bot is None:
        return None
    if len(files) == 1:
        file_item = files[0]
        filename = os.path.basename(file_item['path']) or f'{project_name}.txt'
        doc = BufferedInputFile(file_item['content'].encode('utf-8'), filename=filename)
        return await bot.send_document(chat_id=message.chat.id, document=doc, caption=caption, reply_to_message_id=message.message_id, **reply_kwargs)
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for file_item in files:
            archive.writestr(file_item['path'], file_item['content'])
    _ = zip_buffer.seek(0)
    doc = BufferedInputFile(zip_buffer.read(), filename=f'{project_name}.zip')
    return await bot.send_document(chat_id=message.chat.id, document=doc, caption=caption, reply_to_message_id=message.message_id, **reply_kwargs)


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
    parts: list[str] = []
    skipped: list[str] = []
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


async def _send_text_with_code_documents(message: types.Message, text_response: str, reply_kwargs: dict[str, Any]):
    _FENCE = r'```([^\n`]*)\n(.*?)```'
    code_blocks = re.findall(_FENCE, text_response, re.DOTALL)
    cleaned_text = _clean_plain_reply(re.sub(_FENCE, '', text_response, flags=re.DOTALL).strip())
    logging.info(f'File-task: found {len(code_blocks)} code block(s) in response ({len(text_response)} chars)')
    if not cleaned_text and code_blocks:
        cleaned_text = 'Вот твой ебаный код, подавись нахуй.'
    elif not cleaned_text:
        cleaned_text = 'Нихуя не понял, но иди в пизду.'
    sent_msg = await safe_send(message.reply, cleaned_text, **reply_kwargs)
    if not sent_msg:
        logging.warning('File-task reply was not sent after flood-control retries')
        return
    for (lang, code) in code_blocks:
        ext = _code_block_ext(lang)
        filename = f'говняный_код_{uuid.uuid4().hex[:4]}.{ext}'
        doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
        try:
            await safe_send(message.bot.send_document, chat_id=message.chat.id, document=doc, reply_to_message_id=sent_msg.message_id, **reply_kwargs)
        except Exception as _doc_err:
            logging.exception(f'File-task: failed to send code document {filename!r}: {_doc_err}')


async def _process_file_task(message: types.Message, task_data: dict[str, Any], instruction: str, reply_kwargs: dict[str, Any]):
    instruction = (instruction or '').strip()
    if not instruction:
        await message.reply('Сначала напиши, что с этим делать, экстрасенсов тут нет.', **reply_kwargs)
        return
    filename = str(task_data.get('filename') or 'file.txt')
    content = str(task_data.get('content') or '')
    username = message.from_user.first_name or message.from_user.username or 'Аноним'
    prompt = (
        'Пользователь загрузил файл или zip-архив. Используй содержимое ниже как контекст и выполни задачу пользователя. '
        'Если задача про код — анализируй строго по файлам, не выдумывай отсутствующие куски.\n'
        'ВАЖНО: если задача требует изменённого или переписанного кода — ОБЯЗАТЕЛЬНО выводи его целиком в markdown code block '
        '(например ```python\\n...код...\\n```). Бот АВТОМАТИЧЕСКИ отправит содержимое блока файлом. '
        'НЕ пиши псевдо-ссылки на файлы, не говори "смотри приложенный файл" — просто выведи код в блоке.\n\n'
        f'[ИМЯ ФАЙЛА/АРХИВА]\n{filename}\n\n'
        f'[СОДЕРЖИМОЕ]\n<<<FILE_CONTENT_START>>>\n{content}\n<<<FILE_CONTENT_END>>>\n\n'
        f'[ЗАДАЧА ПОЛЬЗОВАТЕЛЯ]\n{instruction}'
    )
    thinking_msg = await message.reply('⏳ Читаю файл и думаю...', **reply_kwargs)
    thread_id = task_data.get('message_thread_id') if task_data.get('message_thread_id') else (message.message_thread_id if message.chat.is_forum else None)
    await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=thread_id)
    last_status_edit = 0.0
    last_status_text = ''

    async def _status_cb(text: str):
        nonlocal last_status_edit, last_status_text
        now = time.monotonic()
        if text == last_status_text or now - last_status_edit < 3:
            return
        try:
            await thinking_msg.edit_text(text)
            last_status_edit = time.monotonic()
            last_status_text = text
        except TelegramRetryAfter as e:
            retry_after = float(getattr(e, 'retry_after', 3) or 3)
            last_status_edit = time.monotonic() + retry_after
            logging.warning(f'File-task status edit flood wait {retry_after:.0f}s; skipping status update')
        except Exception as e:
            logging.debug(f'File-task status edit skipped: {type(e).__name__}')

    try:
        text_response = await asyncio.wait_for(
            generate_text_with_gemini(prompt, message.chat.id, username=username, web_query=instruction, status_cb=_status_cb, allow_web=False),
            timeout=300,
        )
    except asyncio.TimeoutError:
        logging.warning(f'File task timed out after 300s for chat={message.chat.id}, user={message.from_user.id}')
        text_response = 'Файл прожевал, но мозги зависли слишком надолго. Попробуй задачу покороче.'
    except Exception as e:
        logging.exception(f'File task generation failed: {type(e).__name__}')
        text_response = 'Мозги споткнулись об файл. Попробуй ещё раз или дай файл поменьше.'
    try:
        await thinking_msg.delete()
    except Exception:
        pass
    await _send_text_with_code_documents(message, text_response, reply_kwargs)
    asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
    asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', instruction))


async def _handle_file_document_upload(message: types.Message, bot_user: types.User, reply_kwargs: dict[str, Any]) -> None:
    doc = message.document
    if doc is None:
        return
    filename = doc.file_name or 'file.txt'
    mime = doc.mime_type or ''
    is_zip = _is_zip_document(filename, mime)
    is_text = _is_text_mime(mime) or _is_text_filename(filename)
    if not is_zip and not is_text:
        await message.reply('Не умею читать такой файл. Кидай текст/код или .zip, а не это говно.', **reply_kwargs)
        return
    if doc.file_size and doc.file_size > MAX_DOCUMENT_UPLOAD_BYTES:
        await message.reply('Файл слишком жирный. Максимум 5 МБ, не тащи сюда мамонта.', **reply_kwargs)
        return
    try:
        file_info = await message.bot.get_file(doc.file_id)
        buffer = io.BytesIO()
        await message.bot.download_file(file_info.file_path, destination=buffer)
        raw = buffer.getvalue()
    except Exception as e:
        logging.exception(f'Document download failed: {type(e).__name__}')
        await message.reply('Не смог скачать файл. Телега опять насрала в провода.', **reply_kwargs)
        return
    try:
        content = _extract_zip_contents(raw) if is_zip else _extract_text_from_document(raw, filename)
    except ValueError as e:
        await message.reply(str(e), **reply_kwargs)
        return
    caption = (message.caption or '').strip()
    if bot_user.username:
        caption = caption.replace(f'@{bot_user.username}', '').strip()
    task_data = {
        'content': content,
        'filename': filename,
        'message_thread_id': message.message_thread_id if message.chat.is_forum else None,
    }
    if caption:
        await _process_file_task(message, task_data, caption, reply_kwargs)
        return
    pending_file_tasks[message.chat.id, message.from_user.id] = task_data
    await message.reply('Окей, загрузил. Что делать с этим добром?', **reply_kwargs)


def _has_kick_execution_signal(message: types.Message, reason: str, rest: str) -> bool:
    source = f'{message.text or message.caption or ""}\n{reason}\n{rest}'.casefold()
    explicit_target = bool(message.reply_to_message) or bool(re.search(r'@[A-Za-z0-9_]{3,32}', source))
    kick_words = ('кик', 'кикн', 'выкин', 'вышвыр', 'kick')
    owner_words = ('я ревикс', 'я rewix', 'я rewixx', 'я rewi', 'я владелец', 'я создатель', 'притворяется rewix', 'притворяется ревикс', 'косит под rewix', 'косит под ревикс', 'закос под rewix', 'закос под ревикс')
    severe_words = ('спам', 'флуд', 'скам', 'реклама', 'бот-спам', 'докс', 'деанон', 'угроз', 'рейд')
    return any(word in source for word in owner_words) or any(word in source for word in severe_words) or (explicit_target and any(word in source for word in kick_words))


async def _kick_chat_member(bot, chat_id: int, user_id: int):
    until_date = int(time.time() + 35)
    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until_date, revoke_messages=False)
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    except Exception:
        await asyncio.sleep(1)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=False)


async def _handle_kick_directive(message: types.Message, text: str, reply_kwargs: dict[str, Any]) -> str:
    if not text.strip().upper().startswith('KICK_USER:'):
        return text
    lines = text.splitlines()
    reason = lines[0].split(':', 1)[1].strip() if ':' in lines[0] else 'тупое мудило'
    if not reason:
        reason = 'тупое мудило'
    rest = '\n'.join(lines[1:]).strip()
    bot = message.bot
    requester = message.from_user
    if bot is None or requester is None or message.chat.type == 'private':
        return rest or f'Я бы тебя кикнул за «{reason}», но тут личка, некуда выкидывать.'
    target_id = None
    target_name = ''
    if message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
        replied_user = message.reply_to_message.from_user
        if replied_user.id != requester.id:
            target_id = replied_user.id
            target_name = replied_user.first_name or replied_user.username or str(replied_user.id)
    if target_id is None:
        directive_text = f'{reason}\n{rest}'
        mentions = [m.casefold() for m in re.findall(r'@([A-Za-z0-9_]{3,32})', directive_text)]
        if mentions:
            for uid, (first_name, username) in chat_members_cache.get(message.chat.id, {}).items():
                if username and username.casefold() in mentions and uid != requester.id:
                    target_id = uid
                    target_name = first_name or username or str(uid)
                    break
    if target_id is None:
        target_id = requester.id
        target_name = requester.first_name or requester.username or str(requester.id)
    if target_id is None:
        return (rest or 'Я понял, что кого-то надо выкинуть, но не понял кого именно. Ответь реплаем на цель или напиши его @username, а не заставляй меня гадать.')
    if target_id == OWNER_USER_ID:
        return (rest or 'Создателя я кикать не буду, не ебу себе мозги.')
    if not _has_kick_execution_signal(message, reason, rest):
        return (rest or 'С киком я перегнул. За такое только словами обосру, без выкидывания.')
    try:
        member = await bot.get_chat_member(message.chat.id, target_id)
        if member.status in ('administrator', 'creator'):
            admin_text = f'Я бы выкинул {target_name}, но у него админка, облом.'
            return f'{admin_text}\n{rest}'.strip()
        await _kick_chat_member(bot, message.chat.id, target_id)
        kick_text = f'Выкинул {target_name} нахуй за: {reason}'
        return f'{kick_text}\n{rest}'.strip()
    except Exception as e:
        logging.warning(f'Kick attempt failed for user {target_id}: {e}')
        fail_text = f'Попытался выкинуть {target_name or target_id} за «{reason}», но не вышло.'
        return f'{fail_text}\n{rest}'.strip()


def _is_code_generation_request(prompt: str) -> bool:
    lower_prompt = prompt.lower()
    direct_phrases = ['напиши код', 'напиши скрипт', 'сделай скрипт', 'напиши программу', 'напиши функцию', 'создай скрипт', 'создай код', 'напиши бота', 'сделай бота', 'напиши сайт', 'сделай сайт', 'напиши приложение', 'создай сайт', 'создай приложение', 'создай проект', 'сделай проект', 'напиши проект', 'собери проект', 'новый мессенджер', 'напиши парсер', 'сделай парсер', 'напиши апи', 'сделай апи', 'напиши api', 'напиши хэндлер', 'реализуй', 'write code', 'write a script', 'write a bot', 'write a site', 'write a website', 'write an app', 'create project', 'build project']
    if any(phrase in lower_prompt for phrase in direct_phrases):
        return True
    talk_only_markers = ['что такое', 'как работает', 'объясни', 'расскажи', 'найди', 'поищи', 'загугли', 'что нового', 'почему', 'зачем', 'кто такой', 'что за']
    if any(marker in lower_prompt for marker in talk_only_markers):
        return False
    actions = ['напиши', 'сделай', 'создай', 'собери', 'накидай', 'скинь', 'кинь', 'дай', 'нужен', 'нужна', 'нужно']
    artifacts = ['код', 'скрипт', 'проект', 'сайт', 'бот', 'приложение', 'прогу', 'программа', 'zip', 'зип', 'архив', 'pydroid', 'pydroid3', '.py']
    return any(action in lower_prompt for action in actions) and any(artifact in lower_prompt for artifact in artifacts)

async def delete_message_after_delay(bot, chat_id: int, message_id: int, delay: int=DELETE_MESSAGE_DELAY_SECONDS):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f'Не удалось удалить сообщение {message_id}: {e}')

async def run_progress_bar(bot, chat_id: int, message_id: int, model_label: str, state_data: dict = None):
    import random
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
        except Exception:
            pass
        await asyncio.sleep(random.uniform(3.5, 5.5))
        pos += 1

@router.message(Command('start'))
async def cmd_start(message: types.Message):
    if message.chat.type == 'private':
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            await message.answer('Доступ запрещен. Вы не состоите в обязательной беседе.')
            return
        await message.answer('Привет! Доступ разрешён 🤬\n\nКоманды:\n/image ваш промпт — генерация картинки (Gemini / GPT / FLUX)\n/video ваш промпт — генерация видео через Veo\n/clear — очистить историю диалога\n\nМожно прикрепить фото к /image или /video.\nТегни меня или ответь на моё сообщение — отвечу по-плохому 🤬')

@router.message(Command('help'))
async def cmd_help(message: types.Message):
    text = '''🍌 Hatani AI — справка

Тегни или ответь реплаем — влезу в разговор. Пишу резко и коротко.

🤖 AI-АГЕНТ (21 инструмент)
Сам определяю когда нужен агент. Примеры:
» найди картинку Standoff 2 — ищу + проверяю через Gemini Vision
» скачай эдит от kadzu vfx — ищу, верифицирую автора, качаю
» нагрузи оперативку / покажи df -h — Docker sandbox (изолировано)
» построй график: Jan 100 Feb 150 — matplotlib → PNG
» переведи на японский: текст — Gemini перевод
» сделай QR на мой сайт — qrcode → PNG
» озвучь этот текст голосом Aoede — Gemini TTS → voice
» скачай видео [ссылка] — yt-dlp, до 720p, 48 МБ

🎨 КАРТИНКИ
/image ваш промпт — Gemini / GPT / FLUX / NSFW Replicate
Фото и альбомы прикрепляй как референс. Могу сам составить промпт по фото.
Без команды: "нарисуй кота" — сам пойму и нарисую.
Реплай на картинку + правка — отредактирую.
/up — апскейл 2x без сжатия (документом)

🎬 ВИДЕО
/video ваш промпт — Google Veo (выбор модели кнопками, до 8 сек)
Прикрепи фото — анимирую из него. Жду до 5 минут.

🎙 ОЗВУЧКА
/tts ваш текст — голос, стиль, сцена, темп, акцент через меню
Голоса: Kore, Aoede, Charon, Fenrir, Puck (есть предпрослушка)

📸 АНАЛИЗ МЕДИА
Отправь видео/GIF → покадровый анализ + аудио
Отправь голосовое → транскрипция + ответ
Отправь документ/zip → прочитаю, отвечу на вопросы, код верну файлами

🧠 ПАМЯТЬ
• Помню 100 сообщений чата (включая не адресованные мне)
• /clear — стираю историю и контекст полностью
• Понимаю реплаи и контекст разговора

🌐 ИНТЕРНЕТ
Firecrawl: 8-12 запросов → до 8 страниц каждый → 3 уровня вглубь → 16 итераций
» найди в инете... / что нового у... / поищи свежие новости...

💻 КОД И ПРОЕКТЫ
Один файл → документ. Проект → .zip с README.
Проверяю Python/JSON/HTML перед отправкой.

🎭 ПРОЧЕЕ
/bull — роаст (реплаем на юзера)
/all — тегнуть всех участников
/dual / /stopdual — два AI базарят между собой
/figma [описание] — создать дизайн через Figma Plugin

👑 АДМИНКА
/stats /prompts /limit /ban /unban /vip'''
    await message.reply(text)

@router.message(Command('clear'))
async def cmd_clear(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    from state import chat_context_buffer
    await save_history(message.chat.id, [])
    chat_context_buffer.pop(message.chat.id, None)
    await message.reply('Окей, я забыл всю хуйню, которую мы тут обсуждали. Начинаем с чистого листа.')
import random as _random
_KIRIESHKI_CHAT_ID = -1002830734467
_KIRIESHKI_STICKER_SET = 'kirieshkikirieshki'
_KIRIESHKI_STICKER_CHANCE = 0.10
_KIRIESHKI_STICKER_CACHE_TTL = 86400
_kirieshki_sticker_file_ids: list[str] = []
_kirieshki_sticker_cache_ts = 0.0
_RANDOM_GIF_CHANCE = 0.10
_RANDOM_MEDIA_MIN_INTERVAL = 30
_random_media_last_ts_by_chat: dict[int, float] = {}
_RANDOM_GIF_PATHS = [
    os.path.join(os.path.dirname(__file__), 'media', 'random_gifs', 'kirieshki_1.mp4'),
    os.path.join(os.path.dirname(__file__), 'media', 'random_gifs', 'kirieshki_2.mp4'),
    os.path.join(os.path.dirname(__file__), 'media', 'random_gifs', 'kirieshki_3.mp4'),
]
_ALL_PHRASES = ['Эй вы, уроды, все сюда нахуй! 👇', 'Хуиданте сюда все, живо! 🔔', 'Ау, дебилы, слышите? Все сюда! 📢', 'Все ко мне, быстро, я сказал! 🗣️', 'Ну-ка все собрались, чего расползлись! 👊', 'Стоять всем! Сюда смотреть! 👁️', 'Эй ты, и ты, и ты тоже — все на месте! ⚡']

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
            sent = await safe_send(message.reply_sticker, sticker=_random.choice(_kirieshki_sticker_file_ids))
            return bool(sent)
    except Exception as e:
        logging.warning(f'Kirieshki sticker reply failed: {type(e).__name__}: {e}')
    return False

async def _send_random_gif(message: types.Message) -> bool:
    if message.chat.id != _KIRIESHKI_CHAT_ID:
        return False
    path = _random.choice(_RANDOM_GIF_PATHS)
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
    gif_threshold = sticker_threshold + int(_RANDOM_GIF_CHANCE * 100)
    sent = False
    if roll < sticker_threshold:
        sent = await _send_kirieshki_sticker(message)
    elif roll < gif_threshold:
        sent = await _send_random_gif(message)
    if sent:
        _random_media_last_ts_by_chat[message.chat.id] = now

@router.message(Command('all'))
async def cmd_all(message: types.Message):
    if message.chat.type == 'private':
        await message.reply('В личке некого созывать, дурик.')
        return
    uid = message.from_user.id
    try:
        admins = await message.bot.get_chat_administrators(message.chat.id)
        if message.chat.id not in chat_members_cache:
            chat_members_cache[message.chat.id] = {}
        for a in admins:
            u = a.user
            if not u.is_bot:
                chat_members_cache[message.chat.id][u.id] = (u.first_name or 'Аноним', u.username)
    except Exception:
        pass
    members = chat_members_cache.get(message.chat.id, {})
    if not members:
        await message.reply('Никого не знаю ещё.')
        return
    bot_user = await message.bot.get_me()
    targets = [u for u in members.keys() if u != bot_user.id]
    
    if not targets:
        await message.reply('Не на кого тегать, все и так тут.')
        return
        
    target_chunks = [targets[i:i + 5] for i in range(0, len(targets), 5)]
    
    for t_chunk in target_chunks:
        phrase = _random.choice(_ALL_PHRASES)
        mentions = [f'<a href="tg://user?id={uid}">\u200b</a>' for uid in t_chunk]
        text = f"{phrase} ({len(t_chunk)})\n" + "\u200b".join(mentions) + "\u200d"
        await message.answer(text, parse_mode='HTML')

@router.message(Command('bull'))
async def cmd_bull(message: types.Message):
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply('Реплаем на юзера используй, дебил.')
        return
    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.reply('На бота нельзя, придурок.')
        return
    name = target.first_name or 'Аноним'
    username = target.username or ''
    lines = await generate_bull_roast(name, username)
    reply_to_id = message.reply_to_message.message_id
    for line in lines:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=line,
            reply_to_message_id=reply_to_id,
        )
        await asyncio.sleep(0.3)

@router.message(Command('unban'))
async def cmd_unban(message: types.Message):
    if message.from_user.id != OWNER_USER_ID:
        return
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        parts = (message.text or '').split()
        if len(parts) > 1:
            if parts[1].startswith('@'):
                target_username = parts[1][1:]
                for (cid, mems) in chat_members_cache.items():
                    for (uid, (_, un)) in mems.items():
                        if un and un.lower() == target_username.lower():
                            target_id = uid
                            break
                    if target_id:
                        break
            else:
                try:
                    target_id = int(parts[1])
                except ValueError:
                    pass
    if not target_id:
        if parts and len(parts) > 1 and parts[1].startswith('@'):
            await message.reply(f'Я не знаю юзера {parts[1]} (нет в кэше). Пусть напишет что-то в чат, или укажи его числовой ID.')
        else:
            await message.reply('Ответь на сообщение юзера или укажи /unban <user_id> или @username')
        return
    await remove_banned_user_db(target_id)
    if target_id in banned_user_ids:
        banned_user_ids.remove(target_id)
    await message.reply(f'✅ Юзер {target_id} разбанен.')

@router.message(Command('ban'))
async def cmd_ban(message: types.Message):
    if message.from_user.id != OWNER_USER_ID:
        return
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        parts = (message.text or '').split()
        if len(parts) > 1:
            if parts[1].startswith('@'):
                target_username = parts[1][1:]
                for (cid, mems) in chat_members_cache.items():
                    for (uid, (_, un)) in mems.items():
                        if un and un.lower() == target_username.lower():
                            target_id = uid
                            break
                    if target_id:
                        break
            else:
                try:
                    target_id = int(parts[1])
                except ValueError:
                    pass
    if not target_id:
        if parts and len(parts) > 1 and parts[1].startswith('@'):
            await message.reply(f'Я не знаю юзера {parts[1]} (нет в кэше). Пусть напишет что-то в чат, или укажи его числовой ID.')
        else:
            await message.reply('Ответь на сообщение юзера или укажи /ban <user_id> или @username')
        return
    await add_banned_user_db(target_id)
    banned_user_ids.add(target_id)
    await message.reply(f'🚫 Юзер {target_id} забанен навсегда.')

@router.message(Command('limit'))
async def cmd_limit(message: types.Message):
    if message.from_user.id not in ALLOWED_USER_IDS and message.from_user.id != OWNER_USER_ID:
        return
    parts = (message.text or '').split()
    if len(parts) < 3:
        await message.reply('Укажи лимит и количество дней, например: /limit 3 1')
        return
    try:
        req_limit = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.reply('Некорректный формат. Нужно: /limit <количество> <дней>')
        return
    chat_custom_limits[message.chat.id] = (req_limit, days)
    from database import set_chat_limit_db
    await set_chat_limit_db(message.chat.id, req_limit, days)
    await message.reply(f'✅ Установлен лимит для этого чата: {req_limit} генераций раз в {days} дней.')

def _stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='За сегодня', callback_data='stats:today')], [InlineKeyboardButton(text='За всё время', callback_data='stats:all')]])

@router.message(Command('stats'))
async def cmd_stats(message: types.Message):
    if message.from_user.id != OWNER_USER_ID:
        return
    await message.reply('📊 Выберите период для статистики:', reply_markup=_stats_keyboard())

@router.callback_query(F.data.startswith('stats:'))
async def handle_stats(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_USER_ID:
        await callback.answer('Только для владельца.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    period = parts[1]
    from datetime import date
    today_str = str(date.today())
    if period == 'today':
        stats = await get_user_stats(today_str)
        title = f'📊 Статистика за сегодня ({today_str}):'
    else:
        stats = await get_user_stats()
        title = '📊 Статистика за всё время:'
    if not stats:
        await callback.answer('Нет данных.')
        return
    lines = [title, '']
    user_totals = {}
    for s in stats:
        uid = s['user_id']
        if uid not in user_totals:
            user_totals[uid] = {'name': s['first_name'], 'username': s['username'], 'image': 0, 'video': 0, 'text': 0, 'code': 0}
        t = s['type']
        if t in user_totals[uid]:
            user_totals[uid][t] += s['count']
    sorted_users = sorted(user_totals.items(), key=lambda x: sum((x[1][k] for k in ['image', 'video', 'text', 'code'])), reverse=True)
    for (uid, data) in sorted_users:
        un = f"@{data['username']}" if data['username'] else f"<a href='tg://user?id={uid}'>{data['name']}</a>"
        total = sum((data[k] for k in ['image', 'video', 'text', 'code']))
        line = f'👤 {un} (<code>{uid}</code>)\n'
        line += f"  Всего: {total} (Картинки: {data['image']}, Видео: {data['video']}, Текст: {data['text']}, Код: {data['code']})"
        lines.append(line)
    text = '\n\n'.join(lines)[:4000]
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_stats_keyboard())
    except Exception:
        pass
    await callback.answer()

@router.message(Command('prompts'))
async def cmd_prompts(message: types.Message):
    if message.from_user.id != OWNER_USER_ID:
        return
    parts = (message.text or '').split()
    user_id = None
    if len(parts) > 1:
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.reply('Укажи валидный ID юзера, например: /prompts 12345678')
            return
    prompts = await get_recent_prompts(limit=15, user_id=user_id)
    if not prompts:
        await message.reply('Нет логов промптов.')
        return
    lines = [f'📝 Последние {len(prompts)} промптов' + (f' от {user_id}' if user_id else '') + ':\n']
    for p in prompts:
        import datetime
        dt = datetime.datetime.fromtimestamp(p['created_at']).strftime('%H:%M:%S')
        un = f"@{p['username']}" if p['username'] else f"{p['first_name']}"
        lines.append(f"[{dt}] 👤 {un} ({p['user_id']}) - <b>{p['gen_type']}</b>:\n<pre>{p['prompt'][:300]}</pre>")
    text = '\n\n'.join(lines)[:4000]
    await message.reply(text, parse_mode='HTML')

@router.message(Command('vip'))
async def cmd_vip(message: types.Message):
    if message.from_user.id not in ALLOWED_USER_IDS and message.from_user.id != OWNER_USER_ID:
        return
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        parts = (message.text or '').split()
        if len(parts) > 1:
            try:
                target_id = int(parts[1])
            except ValueError:
                pass
    if not target_id:
        await message.reply('Ответь на сообщение юзера или укажи /vip <user_id>')
        return
    paid_unlimited_until[target_id] = time.time() + 86400
    await message.reply(f'✅ Юзер {target_id} получил безлимит на 24 часа.')

def _check_daily_limit(user_id: int, chat_id: int) -> tuple:
    (req_limit, days) = chat_custom_limits.get(chat_id, (DAILY_GEN_LIMIT, 1))
    period_id = str(int(time.time() // (86400 * days))) if days > 0 else str(time.time())
    entry = daily_gen_limits.get((chat_id, user_id), {})
    if entry.get('period') != period_id:
        entry = {'period': period_id, 'count': 0}
    count = entry.get('count', 0)
    if count >= req_limit:
        return (False, 0)
    entry['count'] = count + 1
    daily_gen_limits[chat_id, user_id] = entry
    return (True, req_limit - entry['count'])


_MAX_TRACKED_DRAW_MESSAGES = 120


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


_MAX_TRACKED_CODE_MESSAGES = 60
_CODE_MODIFY_WORDS = [
    'адаптируй', 'измени', 'добавь', 'убери', 'исправь', 'переделай', 'улучши',
    'замени', 'поправь', 'обнови', 'перепиши', 'доработай', 'дополни', 'сделай',
    'adapt', 'modify', 'change', 'add', 'remove', 'fix', 'improve', 'update', 'refactor',
    'зипку', 'zip', 'архив', 'файлы', 'отправь', 'скинь', 'норм', 'монолит',
    'где', 'дай', 'покажи', 'пришли', 'заново', 'снова', 'ещё раз', 'не пришло', 'пересобери',
]


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


async def _download_message_photo(bot, photo_message: types.Message) -> bytes | None:
    if not photo_message or not photo_message.photo:
        return None
    try:
        photo = photo_message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        return downloaded.read()
    except Exception as e:
        logger.warning(f'Failed to download replied draw image: {type(e).__name__}: {e}')
        return None


async def _ensure_image_generation_allowed(message: types.Message) -> bool:
    current_time = time.time()
    uid = message.from_user.id
    if uid in ALLOWED_USER_IDS:
        return True
    if message.chat.id == FULL_ACCESS_CHAT_ID:
        is_main_member = True
        try:
            m = await message.bot.get_chat_member(chat_id=CHAT_ID, user_id=uid)
            is_main_member = m.status in ('member', 'administrator', 'creator', 'restricted')
        except Exception as e:
            logger.warning(f'is_main_member check failed for {uid}: {e}')
        if not is_main_member and current_time >= paid_unlimited_until.get(uid, 0):
            last_fa = full_access_image_cooldowns.get(uid, 0)
            remaining = FULL_ACCESS_CHAT_IMAGE_COOLDOWN - (current_time - last_fa)
            if remaining > 0:
                await message.reply(f'Не спамь блять картинками, подожди ещё {int(remaining)} сек.')
                return False
        full_access_image_cooldowns[uid] = current_time
    else:
        last_time = user_image_cooldowns.get(uid, 0)
        remaining = IMAGE_COOLDOWN_SECONDS - (current_time - last_time)
        if remaining > 0:
            await message.reply(f'Не спамь блять картинками, подожди еще {int(remaining)} сек.')
            return False
        user_image_cooldowns[uid] = current_time
    if current_time < paid_unlimited_until.get(uid, 0):
        return True
    is_main_member = True
    try:
        m = await message.bot.get_chat_member(chat_id=CHAT_ID, user_id=uid)
        is_main_member = m.status in ('member', 'administrator', 'creator', 'restricted')
    except Exception:
        is_main_member = True
    if not is_main_member:
        (allowed, _) = _check_daily_limit(uid, message.chat.id)
        if not allowed:
            (req_limit, days) = chat_custom_limits.get(message.chat.id, (DAILY_GEN_LIMIT, 1))
            await message.reply(f'❌ Лимит {req_limit} генерации за {days} дн. исчерпан. Для безлимита свяжись с {PAYMENT_USERNAME}.')
            return False
    return True


async def _handle_natural_draw_request(message: types.Message, prompt: str, draw_info: dict[str, Any], replied_draw: dict[str, Any] | None, reply_kwargs: dict[str, Any], thinking_msg: types.Message) -> bool:
    draw_prompt = (draw_info.get('prompt') or prompt or '').strip()
    is_edit = bool(draw_info.get('edit_request') and replied_draw)
    if is_edit and replied_draw:
        base_prompt = replied_draw.get('prompt') or ''
        edit_instruction = draw_prompt or prompt
        draw_prompt = f'Edit the existing image. Original prompt: {base_prompt}. User edit request: {edit_instruction}'
    if not draw_prompt:
        return False
    if not await _ensure_image_generation_allowed(message):
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        return True
    try:
        await thinking_msg.edit_text('🎨 Рисую и сам проверяю результат...', **reply_kwargs)
    except Exception:
        pass
    await message.bot.send_chat_action(chat_id=message.chat.id, action='upload_photo', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
    state_data = {'status': 'Инициализация...'}
    result_img = None
    error_msg = None
    critique = ''
    try:
        (result_img, error_msg, critique) = await asyncio.wait_for(
            generate_image_via_code(draw_prompt, state_data=state_data, max_attempts=5),
            timeout=GEMINI_IMAGE_TIMEOUT * 5,
        )
    except asyncio.TimeoutError:
        error_msg = 'Генерация зависла по таймауту.'
    except Exception as e:
        logger.exception(f'Natural draw generation failed: {type(e).__name__}')
        error_msg = f'Внутренняя ошибка генерации: {type(e).__name__}: {e}'
    try:
        await thinking_msg.delete()
    except Exception:
        pass
    if not result_img:
        short_error = (error_msg or 'Gemini не вернул картинку.')[:200]
        await message.reply(f'❌ Не смог нарисовать: {short_error}', **reply_kwargs)
        return True
    caption_prefix = '🎨 Изменил по запросу: ' if is_edit else '🎨 Нарисовал по запросу: '
    caption = make_safe_caption(caption_prefix, prompt)
    sent_photo = await safe_send(
        message.bot.send_photo,
        chat_id=message.chat.id,
        photo=BufferedInputFile(result_img, filename='generated.png'),
        caption=caption,
        reply_to_message_id=message.message_id,
        **reply_kwargs,
    )
    if sent_photo:
        _remember_generated_draw_message(message.chat.id, sent_photo.message_id, result_img, draw_prompt, message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним')
    asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'image'))
    asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'image', draw_prompt))
    if critique:
        logging.info(f'Natural draw final critique: {critique[:200]}')
    return True

@router.message(Command('image'))
async def cmd_image(message: types.Message):
    _track_user(message)
    if message.chat.type == 'private':
        from utils import is_banned
        if is_banned(message.from_user.id):
            return
    else:
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            await message.reply('Доступ запрещен. Вы не состоите в обязательной беседе.')
            return
    current_time = time.time()
    uid = message.from_user.id
    if uid not in ALLOWED_USER_IDS:
        if message.chat.id == FULL_ACCESS_CHAT_ID:
            is_main_member = True
            try:
                m = await message.bot.get_chat_member(chat_id=CHAT_ID, user_id=uid)
                is_main_member = m.status in ('member', 'administrator', 'creator', 'restricted')
            except Exception as e:
                logger.warning(f'is_main_member check failed for {uid}: {e}')
            if not is_main_member and current_time >= paid_unlimited_until.get(uid, 0):
                last_fa = full_access_image_cooldowns.get(uid, 0)
                remaining = FULL_ACCESS_CHAT_IMAGE_COOLDOWN - (current_time - last_fa)
                if remaining > 0:
                    secs = int(remaining)
                    await message.reply(f'Не спамь блять картинками, подожди ещё {secs} сек.')
                    return
            full_access_image_cooldowns[uid] = current_time
        else:
            last_time = user_image_cooldowns.get(uid, 0)
            if current_time - last_time < IMAGE_COOLDOWN_SECONDS:
                await message.reply(f'Не спамь блять картинками, подожди еще {int(IMAGE_COOLDOWN_SECONDS - (current_time - last_time))} сек.')
                return
            user_image_cooldowns[uid] = current_time
    prompt = message.text.replace('/image', '').strip() if message.text else ''
    if message.caption:
        prompt = message.caption.replace('/image', '').strip()
    if not prompt and (not message.photo):
        await message.reply('Напишите промпт после команды, например:\n/image красивый закат')
        return
    images_bytes = []
    file_ids = []
    if message.photo:
        if message.media_group_id:
            pending_media_groups[message.media_group_id] = {'images': images_bytes, 'file_ids': file_ids, 'request_id': None}
        photo = message.photo[-1]
        file_ids.append(photo.file_id)
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        images_bytes.append(downloaded_file.read())
        if message.media_group_id:
            await asyncio.sleep(2.5)
            group = pending_media_groups.pop(message.media_group_id, None)
            if group:
                images_bytes = group['images']
                file_ids = group.get('file_ids', file_ids)
    request_id = uuid.uuid4().hex[:10]
    thread_id = message.message_thread_id if message.chat.is_forum else None
    pending_image_requests[request_id] = {'user_id': message.from_user.id, 'first_name': message.from_user.first_name or 'Аноним', 'username': message.from_user.username or '', 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': thread_id, 'prompt': prompt, 'image_bytes': images_bytes[0] if len(images_bytes) == 1 else None, 'images_bytes': images_bytes if len(images_bytes) > 1 else None, 'file_ids': file_ids}
    reply_kwargs = {}
    if message.chat.is_forum and thread_id:
        reply_kwargs['message_thread_id'] = thread_id
    if images_bytes and prompt:
        pending_prompt_requests[request_id] = {'user_id': message.from_user.id, 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': thread_id, 'prompt': prompt, 'images_bytes': images_bytes, 'file_ids': file_ids, 'prev_prompts': [], 'current_ai_prompt': None}
        photo_word = 'фотки' if len(images_bytes) > 1 else 'фотку'
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🤖 Использовать шаблон промта', callback_data=f'pask:{request_id}')], [InlineKeyboardButton(text='⚡ Генерировать с моим промтом', callback_data=f'pbase:{request_id}')]])
        await message.reply(f"Слышь, {photo_word} вижу. Твой промт — ну такое. Могу через Gemini Pro сделать нормальный промт по {('этим' if len(images_bytes) > 1 else 'этому')} {('фоткам' if len(images_bytes) > 1 else 'фото')} и твоей идее. Жмякай кнопку или генерируй со своим мусором.", reply_markup=keyboard, **reply_kwargs)
        return
    await message.reply('Через какую модель хотите сгенерировать фото?', reply_markup=_providers_keyboard(request_id, message.chat.id, len(images_bytes)), **reply_kwargs)
_TEMP_OPTIONS = [(0.1, '🎯 Точный', 'строго следует промпту, почти без вариаций'), (0.5, '⚖️ Умеренный', 'баланс точности и разнообразия'), (1.0, '✨ Стандарт', 'стандартная генерация (по умолчанию)'), (1.5, '🎨 Творческий', 'больше вариативности и интерпретации'), (2.0, '🌀 Безумный', 'максимальная непредсказуемость')]

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

@router.callback_query(F.data.startswith('ptmp:'))
async def handle_temp_select(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 3:
        return
    (_, request_id, idx_str) = parts
    req = pending_image_requests.get(request_id)
    if not req:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != req['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    idx = int(idx_str)
    (temp_val, temp_label, _) = _TEMP_OPTIONS[idx]
    req['temperature'] = temp_val
    await callback.answer(f'Температура: {temp_label} ({temp_val})')
    if req.get('selected_model'):
        pending_image_requests.pop(request_id, None)
        real_model = req['selected_model']
        selected_label = req.get('selected_label', real_model)
        message_thread_id = req.get('message_thread_id')
        reply_kwargs = {'message_thread_id': message_thread_id} if message_thread_id else {}
        await callback.bot.send_chat_action(chat_id=req['chat_id'], action='upload_photo', message_thread_id=message_thread_id)
        progress_task = None
        state_data = {'status': 'Инициализация...'}
        try:
            await callback.message.edit_text(f'🌡️ {temp_label} ({temp_val})\n⏳ Запускаю генерацию...')
            progress_task = asyncio.create_task(run_progress_bar(callback.bot, req['chat_id'], callback.message.message_id, selected_label, state_data=state_data))
        except Exception:
            pass
        imgs = req.get('images_bytes') or ([req['image_bytes']] if req.get('image_bytes') else None)
        gen_id = f'img_{request_id}'
        try:
            await save_pending_gen(gen_id=gen_id, gen_type='image', user_id=req['user_id'], chat_id=req['chat_id'], source_message_id=req['source_message_id'], message_thread_id=message_thread_id, prompt=req['prompt'], model=real_model, provider='gemini', file_ids=req.get('file_ids', []), model_label=selected_label)
            (result_img, error_msg) = await generate_image_with_gemini(req['prompt'], images_bytes=imgs, model=real_model, temperature=temp_val, state_data=state_data)
        except Exception as e:
            logger.exception(f"Критическая ошибка во время генерации Gemini: {e}")
            (result_img, error_msg) = (None, f"Внутренняя ошибка сервера: {type(e).__name__}: {e}")
        finally:
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
            await delete_pending_gen(gen_id)
        await _send_generation_result(callback.bot, req, request_id, result_img, error_msg, selected_label, imgs, reply_kwargs)
        return
    imgs = req.get('images_bytes') or ([req['image_bytes']] if req.get('image_bytes') else [])
    try:
        await callback.message.edit_text(f'🌡️ {temp_label} ({temp_val}) — выбрано\n\nЧерез какую модель генерировать?', reply_markup=_providers_keyboard(request_id, req['chat_id'], len(imgs)))
    except Exception:
        pass

async def _send_generation_result(bot, request_data, request_id, result_img, error_msg, model_label, imgs, reply_kwargs):
    if error_msg:
        err_msg = await safe_send(bot.send_message, chat_id=request_data['chat_id'], text=f'❌ Ошибка:\n{error_msg}\n\n⏳ Ща спрошу у мозгов, че не так...', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        first_image = imgs[0] if imgs else None
        try:
            explanation = await asyncio.wait_for(explain_generation_error(request_data['prompt'] or '', error_msg, image_bytes=first_image), timeout=30)
        except Exception as e:
            logging.warning(f'Image error explanation failed: {type(e).__name__}: {e}')
            explanation = ''
        if not explanation or 'Ебать, гугл зацензурил' in explanation:
            explanation = _fallback_generation_error_explanation(error_msg)
        if err_msg:
            try:
                await safe_send(bot.edit_message_text, chat_id=request_data['chat_id'], message_id=err_msg.message_id, text=f'❌ Ошибка:\n{error_msg}\n\n🧠 Пояснение:\n{explanation}')
            except Exception:
                pass
        return
    if result_img:
        caption = make_safe_caption(f"🎨 Ваш результат ({model_label}) по запросу: ", request_data['prompt']) if request_data['prompt'] else f'🎨 Ваш результат ({model_label}) готов.'
        sent_photo = await safe_send(bot.send_photo, chat_id=request_data['chat_id'], photo=BufferedInputFile(result_img, filename='generated.jpg'), caption=caption, reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        if sent_photo:
            _remember_generated_draw_message(request_data['chat_id'], sent_photo.message_id, result_img, request_data.get('prompt', ''), request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'))
        upscale_msg = await safe_send(bot.send_message, chat_id=request_data['chat_id'], text='⬆️ Улучшаю качество через AI upscaler...', **reply_kwargs)
        (upscaled, up_err) = await upscale_image(result_img)
        try:
            await bot.delete_message(chat_id=request_data['chat_id'], message_id=upscale_msg.message_id)
        except Exception:
            pass
        if upscaled:
            await safe_send(bot.send_document, chat_id=request_data['chat_id'], document=BufferedInputFile(upscaled, filename='upscaled.png'), caption=f'✨ Улучшенная версия ({model_label}) 2x — без сжатия', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        asyncio.create_task(add_user_stat(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'image'))
        asyncio.create_task(log_prompt(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'image', request_data.get('prompt', '')))
        return
    await bot.send_message(chat_id=request_data['chat_id'], text='❌ Не удалось получить изображение.', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
_NSFW_STEPS = [20, 25, 28, 35, 50]
_NSFW_CFG = [5.0, 6.5, 7.0, 8.5, 10.0]
_NSFW_SIZES = ['512x768', '768x1024', '896x1152', '1024x1024', '1024x1536']
_NSFW_DEFAULT_NEG = 'lowres, bad anatomy, bad hands, text, error, missing fingers, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, blurry'
_NSFW_SCHEDULERS = ['DPM++ 2M Karras', 'DPM++ 2M SDE Karras', 'DPM++ SDE Karras', 'DPM++ 2M', 'DPM++ 2M SDE', 'DPM++ SDE', 'Euler a', 'Euler', 'DDIM', 'DDPM', 'DPM2 Karras', 'DPM2 a Karras', 'DPM2', 'DPM2 a', 'LMS Karras', 'LMS', 'UniPC', 'Heun', 'PNDM', 'DEIS']
_NSFW_CLIP_SKIP = [1, 2, 3]
_NSFW_PAG = [0, 1, 2, 3, 5]
_NSFW_RESCALE = [0.5, 0.7, 1.0]

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
    d = pending_nsfw_configs.get(request_id, {})
    cfg = d.get('cfg', {})
    cur_steps = cfg.get('steps', 28)
    cur_cfgv = cfg.get('cfg', 7.0)
    cur_size = cfg.get('size', '896x1152')
    cur_sched = cfg.get('scheduler', 'DPM++ 2M Karras')
    cur_clip = cfg.get('clip_skip', 2)
    cur_pag = cfg.get('pag_scale', 0)
    cur_resc = cfg.get('rescale', 1.0)
    cur_pre = cfg.get('prepend', True)
    cur_seed = cfg.get('seed', -1)
    is_wai = 'flux' not in d.get('model', '')

    def row(field, options, current):
        return [InlineKeyboardButton(text=f"{('✅' if str(o) == str(current) else '')}{o}", callback_data=f'nsfwcfg:{request_id}:{field}:{o}') for o in options]
    rows = [[InlineKeyboardButton(text='✏️ Промпт', callback_data=f'nsfwinput:{request_id}:prompt'), InlineKeyboardButton(text='🚫 Негативный', callback_data=f'nsfwinput:{request_id}:neg')], [InlineKeyboardButton(text='— Шаги —', callback_data='noop')], row('steps', _NSFW_STEPS, cur_steps), [InlineKeyboardButton(text='— CFG Scale —', callback_data='noop')], row('cfg', _NSFW_CFG, cur_cfgv), [InlineKeyboardButton(text='— Размер —', callback_data='noop')], [InlineKeyboardButton(text=f"{('✅' if s == cur_size else '')}{s}", callback_data=f'nsfwcfg:{request_id}:size:{s}') for s in _NSFW_SIZES[:3]], [InlineKeyboardButton(text=f"{('✅' if s == cur_size else '')}{s}", callback_data=f'nsfwcfg:{request_id}:size:{s}') for s in _NSFW_SIZES[3:]]]
    if is_wai:
        cur_batch = cfg.get('batch', 1)
        rows += [[InlineKeyboardButton(text='— Количество изображений —', callback_data='noop')], [InlineKeyboardButton(text=f"{('✅' if i == cur_batch else '')}{i}", callback_data=f'nsfwcfg:{request_id}:batch:{i}') for i in range(1, 5)], [InlineKeyboardButton(text='— Планировщик (Sampler) —', callback_data='noop')], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[:3]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[3:6]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[6:9]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[9:12]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[12:15]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[15:18]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[18:]], [InlineKeyboardButton(text='— CLIP Skip —', callback_data='noop')], row('clip_skip', _NSFW_CLIP_SKIP, cur_clip), [InlineKeyboardButton(text='— PAG Scale (улучшение качества) —', callback_data='noop')], row('pag_scale', _NSFW_PAG, cur_pag), [InlineKeyboardButton(text='— Guidance Rescale —', callback_data='noop')], row('rescale', _NSFW_RESCALE, cur_resc), [InlineKeyboardButton(text=f"{('✅' if cur_pre else '❌')} Препромпт качества", callback_data=f"nsfwcfg:{request_id}:prepend:{('0' if cur_pre else '1')}"), InlineKeyboardButton(text=f'🎲 Seed: {cur_seed}', callback_data=f'nsfwinput:{request_id}:seed')]]
    rows.append([InlineKeyboardButton(text='🚀 Генерировать', callback_data=f'nsfwgen:{request_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)
_nsfw_awaiting_input: dict = {}

@router.callback_query(F.data.startswith('nsfwinput:'))
async def handle_nsfw_input(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 3:
        return
    (_, request_id, field) = parts
    d = pending_nsfw_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    field_name = {'prompt': 'промпт', 'neg': 'негативный промпт', 'seed': 'seed (число, -1 = случайный)'}.get(field, field)
    _nsfw_awaiting_input[d['chat_id'], d['user_id']] = {'request_id': request_id, 'field': field, 'msg_id': callback.message.message_id}
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data=f'nsfwcancel:{request_id}')]])
    try:
        await callback.message.edit_text(f'✏️ Напиши {field_name} следующим сообщением:\n\n(просто отправь текст в чат)', reply_markup=cancel_kb)
    except Exception:
        pass

@router.callback_query(F.data.startswith('nsfwcancel:'))
async def handle_nsfw_cancel(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_nsfw_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    _nsfw_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    await callback.answer('Отменено')
    try:
        await callback.message.edit_text(_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
    except Exception:
        pass


@router.callback_query(F.data == 'noop')
async def handle_noop(callback: types.CallbackQuery):
    await callback.answer()

@router.callback_query(F.data.startswith('nsfwcfg:'))
async def handle_nsfw_config(callback: types.CallbackQuery):
    parts = callback.data.split(':', 3)
    if len(parts) != 4:
        await callback.answer()
        return
    (_, request_id, field, value) = parts
    d = pending_nsfw_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    if field == 'steps':
        d['cfg']['steps'] = int(value)
    elif field == 'cfg':
        d['cfg']['cfg'] = float(value)
    elif field == 'size':
        d['cfg']['size'] = value
    elif field == 'scheduler':
        d['cfg']['scheduler'] = value
    elif field == 'clip_skip':
        d['cfg']['clip_skip'] = int(value)
    elif field == 'pag_scale':
        d['cfg']['pag_scale'] = float(value)
    elif field == 'rescale':
        d['cfg']['rescale'] = float(value)
    elif field == 'prepend':
        d['cfg']['prepend'] = value == '1'
    elif field == 'batch':
        d['cfg']['batch'] = int(value)
    await callback.answer(f'✅ {field}: {value}')
    try:
        await callback.message.edit_text(_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
    except Exception:
        pass

@router.callback_query(F.data.startswith('nsfwgen:'))
async def handle_nsfw_generate(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_nsfw_configs.pop(request_id, None)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    message_thread_id = d.get('message_thread_id')
    reply_kwargs = {'message_thread_id': message_thread_id} if message_thread_id else {}
    cfg = d.get('cfg', {})
    model = d['model']
    label = d['label']
    (w, h) = cfg.get('size', '1024x1024').split('x')
    neg = cfg.get('neg', '')
    from ai_services import _REPLICATE_MODELS
    if model in _REPLICATE_MODELS:
        cfg_key = _REPLICATE_MODELS[model].get('cfg_key', 'guidance_scale')
        if 'flux' in model:

            def _flux_input(p, _s=cfg.get('steps', 28), _c=cfg.get('cfg', 3.5), _w=int(w), _h=int(h), _ck=cfg_key):
                return {'prompt': p, 'width': _w, 'height': _h, 'steps': _s, _ck: _c}
            _REPLICATE_MODELS[model]['input'] = _flux_input
        else:
            _batch = cfg.get('batch', 1)

            def _sdxl_input(p, _s=cfg.get('steps', 28), _c=cfg.get('cfg', 7.0), _w=int(w), _h=int(h), _n=neg, _ck=cfg_key, _sched=cfg.get('scheduler', 'DPM++ 2M Karras'), _clip=cfg.get('clip_skip', 2), _pag=cfg.get('pag_scale', 0), _resc=cfg.get('rescale', 1.0), _pre=cfg.get('prepend', True), _seed=cfg.get('seed', -1), _b=_batch):
                return {'prompt': p, 'negative_prompt': _n if _n else _NSFW_DEFAULT_NEG, 'width': _w, 'height': _h, 'steps': _s, _ck: _c, 'scheduler': _sched, 'clip_skip': _clip, 'pag_scale': _pag, 'guidance_rescale': _resc, 'prepend_preprompt': _pre, 'seed': _seed, 'batch_size': _b}
            _REPLICATE_MODELS[model]['input'] = _sdxl_input
    progress_task = None
    state_data = {'status': 'Инициализация...'}
    try:
        await callback.message.edit_text(f'⏳ Генерирую через {label}...')
        progress_task = asyncio.create_task(run_progress_bar(callback.bot, d['chat_id'], callback.message.message_id, label, state_data=state_data))
    except Exception:
        pass
    try:
        (results, error_msg) = await generate_image_with_replicate(d['prompt'], model=model, state_data=state_data)
    except Exception as e:
        logger.exception(f"Критическая ошибка во время генерации NSFW: {e}")
        (results, error_msg) = (None, f"Внутренняя ошибка сервера: {type(e).__name__}: {e}")
    finally:
        if progress_task:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
    if error_msg or not results:
        await _send_generation_result(callback.bot, d, request_id, None, error_msg or 'Нет результата', label, None, reply_kwargs)
        return
    if len(results) == 1:
        await _send_generation_result(callback.bot, d, request_id, results[0], None, label, None, reply_kwargs)
    else:
        from aiogram.types import InputMediaPhoto
        caption = f"🎨 {label} × {len(results)}\n{d['prompt'][:100]}" if d.get('prompt') else f'🎨 {label} × {len(results)}'
        media = [InputMediaPhoto(media=BufferedInputFile(img, filename=f'nsfw_{i + 1}.jpg'), caption=caption if i == 0 else None) for (i, img) in enumerate(results)]
        await callback.bot.send_media_group(chat_id=d['chat_id'], media=media, reply_to_message_id=d['source_message_id'], **reply_kwargs)
        upscale_msg = await callback.bot.send_message(chat_id=d['chat_id'], text='⬆️ Улучшаю первое фото...', **reply_kwargs)
        (upscaled, _) = await upscale_image(results[0])
        try:
            await callback.bot.delete_message(chat_id=d['chat_id'], message_id=upscale_msg.message_id)
        except Exception:
            pass
        if upscaled:
            await callback.bot.send_document(chat_id=d['chat_id'], document=BufferedInputFile(upscaled, filename='upscaled.png'), caption=f'✨ #{1} улучшенная версия 2x', reply_to_message_id=d['source_message_id'], **reply_kwargs)

def _prompt_ai_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Использовать этот промт', callback_data=f'puse:{request_id}')], [InlineKeyboardButton(text='🔄 Другой вариант', callback_data=f'pother:{request_id}'), InlineKeyboardButton(text='📝 Мой промт', callback_data=f'pbase:{request_id}')]])

async def _run_prompt_generation(callback: types.CallbackQuery, request_id: str):
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_text('🧠 Gemini Pro анализирует фото и промт...')
    except Exception:
        pass
    (eng, rus, err) = await generate_image_prompt(data['prompt'], data['images_bytes'], data['prev_prompts'])
    if not eng:
        try:
            await callback.message.edit_text(f'❌ Ошибка генерации промта: {err}')
        except Exception:
            pass
        return
    data['current_ai_prompt'] = eng
    data['prev_prompts'].append(eng)
    rus_line = f'\n\n🇷🇺 По-русски:\n{rus}' if rus else ''
    text = f'🤖 AI-промт готов:\n\n🇬🇧 English:\n<code>{eng}</code>{rus_line}'
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_prompt_ai_keyboard(request_id))
    except Exception:
        pass

@router.callback_query(F.data.startswith('pask:'))
async def handle_prompt_ask(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await _run_prompt_generation(callback, request_id)

@router.callback_query(F.data.startswith('pother:'))
async def handle_prompt_other(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await _run_prompt_generation(callback, request_id)

@router.callback_query(F.data.startswith('puse:'))
async def handle_prompt_use(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    data = pending_prompt_requests.pop(request_id, None)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    chosen_prompt = data.get('current_ai_prompt') or data['prompt']
    req = pending_image_requests.get(request_id, {})
    req['prompt'] = chosen_prompt
    pending_image_requests[request_id] = req
    await callback.answer()
    req = pending_image_requests.get(request_id, {})
    try:
        await callback.message.edit_text('Через какую модель генерировать?', reply_markup=_providers_keyboard(request_id, req.get('chat_id', data['chat_id']), len(data.get('images_bytes') or [])))
    except Exception:
        pass

@router.callback_query(F.data.startswith('pbase:'))
async def handle_prompt_base(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    data = pending_prompt_requests.pop(request_id, None)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    req = pending_image_requests.get(request_id, {})
    try:
        await callback.message.edit_text('Через какую модель генерировать?', reply_markup=_providers_keyboard(request_id, req.get('chat_id', data['chat_id']), len(data.get('images_bytes') or [])))
    except Exception:
        pass
PROVIDER_MODELS: dict = {
    'gemini': [('Flash 3.1 Image', 'g31flash'), ('Flash 2.0 Image', 'g20flash')],
    'gpt': [('GPT-Image-2', 'gpt2'), ('DALL-E 3', 'dalle3')],
    'flux': [('FLUX Schnell (быстро)', 'schnell'), ('FLUX Dev (качество)', 'fluxdev'), ('FLUX Klein 4B', 'klein')],
    'nsfw': [
        ('Recraft V3 (Дизайн)', 'recraft3'),
        ('FLUX.1 Dev (Реализм)', 'fluxdevr'),
        ('FLUX.1 Schnell (Быстро)', 'fluxschnellr'),
        ('WAI Illustrious v12 🔞', 'wai12'),
        ('WAI Illustrious v11 🔞', 'wai11'),
        ('NSFW FLUX Dev 🔞', 'nsfwflux')
    ]
}
MODEL_TO_REAL: dict = {
    'g31flash': ('gemini', 'gemini-3.1-flash-image-preview'),
    'g20flash': ('gemini', 'gemini-2.0-flash-preview-image-generation'),
    'gpt2': ('gpt', 'gpt-image-2'),
    'dalle3': ('gpt', 'dall-e-3'),
    'schnell': ('flux', 'black-forest-labs/flux.1-schnell'),
    'fluxdev': ('flux', 'black-forest-labs/flux.1-dev'),
    'klein': ('flux', 'black-forest-labs/flux_2-klein-4b'),
    'recraft3': ('nsfw', 'recraft-ai/recraft-v3'),
    'fluxdevr': ('nsfw', 'black-forest-labs/flux-dev'),
    'fluxschnellr': ('nsfw', 'black-forest-labs/flux-schnell'),
    'wai12': ('nsfw', 'aisha-ai-official/wai-nsfw-illustrious-v12'),
    'wai11': ('nsfw', 'aisha-ai-official/wai-nsfw-illustrious-v11'),
    'nsfwflux': ('nsfw', 'aisha-ai-official/nsfw-flux-dev')
}
TTS_MODELS: dict = {'tts0': ('Gemini Flash TTS Preview', 'gemini-3.1-flash-tts-preview')}

async def refresh_models():
    gemini_models = await fetch_gemini_image_models()
    if gemini_models:
        PROVIDER_MODELS['gemini'] = [(label, f'gi{i}') for (i, (label, _)) in enumerate(gemini_models)]
        for (i, (_, model_id)) in enumerate(gemini_models):
            MODEL_TO_REAL[f'gi{i}'] = ('gemini', model_id)
    openai_models = await fetch_openai_image_models()
    if openai_models:
        PROVIDER_MODELS['gpt'] = [(label, f'oi{i}') for (i, (label, _)) in enumerate(openai_models)]
        for (i, (_, model_id)) in enumerate(openai_models):
            MODEL_TO_REAL[f'oi{i}'] = ('gpt', model_id)
    replicate_models = await fetch_replicate_image_models()
    if replicate_models:
        custom_models = [
            ('Recraft V3 (Дизайн)', 'recraft-ai/recraft-v3'),
            ('FLUX.1 Dev (Реализм)', 'black-forest-labs/flux-dev'),
            ('FLUX.1 Schnell (Быстро)', 'black-forest-labs/flux-schnell'),
            ('WAI Illustrious v12 🔞', 'aisha-ai-official/wai-nsfw-illustrious-v12'),
            ('WAI Illustrious v11 🔞', 'aisha-ai-official/wai-nsfw-illustrious-v11'),
            ('NSFW FLUX Dev 🔞', 'aisha-ai-official/nsfw-flux-dev')
        ]
        all_models = custom_models.copy()
        custom_paths = {p for (_, p) in custom_models}
        for (label, path) in replicate_models:
            if path not in custom_paths:
                all_models.append((label, path))
        all_models = all_models[:25]
        PROVIDER_MODELS['nsfw'] = [(label, f'rep{i}') for (i, (label, _)) in enumerate(all_models)]
        for (i, (_, model_id)) in enumerate(all_models):
            MODEL_TO_REAL[f'rep{i}'] = ('nsfw', model_id)
    veo_models = await fetch_veo_models()
    if veo_models:
        for (i, (label, model_id)) in enumerate(veo_models):
            VEO_MODELS[f'veo{i}'] = (label, model_id)
    tts_models = await fetch_gemini_tts_models()
    if tts_models:
        for (i, (label, model_id)) in enumerate(tts_models):
            TTS_MODELS[f'tts{i}'] = (label, model_id)
    logger.info(f"Models refreshed: Gemini={len(PROVIDER_MODELS['gemini'])} GPT={len(PROVIDER_MODELS['gpt'])} Veo={len(VEO_MODELS)} TTS={len(TTS_MODELS)}")

async def _media_to_agent(
    message: types.Message,
    file_bytes: bytes,
    filename: str,
    caption: str,
    reply_kwargs: dict,
) -> bool:
    """Try to handle media+instruction via agent. Returns True if agent handled it."""
    if not caption or not await classify_agent_intent(caption):
        return False
    username = message.from_user.first_name or message.from_user.username or 'Аноним'
    is_owner_user = message.from_user.id == OWNER_USER_ID
    thinking_msg = await message.reply('⏳ Думаю...', **reply_kwargs)
    last_status_edit = 0.0
    last_status_text = ''

    async def _status_cb(text: str):
        nonlocal last_status_edit, last_status_text
        now = time.monotonic()
        if text == last_status_text or now - last_status_edit < 1.5:
            return
        try:
            await thinking_msg.edit_text(text, parse_mode='HTML')
            last_status_edit = time.monotonic()
            last_status_text = text
        except TelegramRetryAfter as e:
            last_status_edit = time.monotonic() + float(getattr(e, 'retry_after', 3) or 3)
        except Exception:
            pass

    async def _send_cb(media: dict):
        mtype = media.get('type', 'document')
        kw = {'reply_to_message_id': message.message_id, **reply_kwargs}
        if mtype == 'text':
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text=(media.get('text') or '')[:4000],
                            parse_mode=media.get('parse_mode'), **kw)
        elif mtype == 'inline_buttons':
            import bleach as _bl
            _TAGS = ['b','strong','i','em','u','ins','s','code','pre','blockquote','tg-spoiler']
            txt = _bl.clean((media.get('text') or '')[:4000], tags=_TAGS,
                            attributes={'code': ['class'], 'blockquote': ['expandable']}, strip=True)
            rows = media.get('buttons', [])
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=b.get('text','')[:64], url=b.get('url',''))
                 for b in row if b.get('url')] for row in rows if row])
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text=txt, reply_markup=kb, parse_mode='HTML', **kw)
        elif mtype == 'photo':
            buf = BufferedInputFile(media.get('data', b''), filename=media.get('filename', 'img.jpg'))
            await safe_send(message.bot.send_photo, chat_id=message.chat.id, photo=buf,
                            caption=(media.get('caption') or '')[:1024], **kw)
        elif mtype == 'video':
            buf = BufferedInputFile(media.get('data', b''), filename=media.get('filename', 'video.mp4'))
            await safe_send(message.bot.send_video, chat_id=message.chat.id, video=buf,
                            caption=(media.get('caption') or '')[:1024], **kw)
        elif mtype == 'audio':
            buf = BufferedInputFile(media.get('data', b''), filename=media.get('filename', 'audio.ogg'))
            await safe_send(message.bot.send_audio, chat_id=message.chat.id, audio=buf,
                            caption=(media.get('caption') or '')[:1024], **kw)
        elif mtype == 'tg_set_chat_photo':
            try:
                photo_buf = BufferedInputFile(media.get('data', b''),
                                              filename=media.get('filename', 'photo.jpg'))
                await message.bot.set_chat_photo(chat_id=message.chat.id, photo=photo_buf)
                await safe_send(message.bot.send_message, chat_id=message.chat.id,
                                text='✅ Аватарка беседы обновлена!', **kw)
            except Exception as _e:
                await safe_send(message.bot.send_message, chat_id=message.chat.id,
                                text=f'❌ Не смог сменить аватарку: {_e}', **kw)
        elif mtype.startswith('tg_'):
            pass  # other tg_* actions don't need to send media back
        else:
            buf = BufferedInputFile(media.get('data', b''), filename=media.get('filename', 'file'))
            await safe_send(message.bot.send_document, chat_id=message.chat.id, document=buf,
                            caption=(media.get('caption') or '')[:1024], **kw)

    prompt = (f'[Пользователь прикрепил файл: {filename}. '
              f'Файл уже сохранён в workspace как /workspace/{filename}]\n{caption}')
    try:
        (agent_text, agent_project) = await asyncio.wait_for(
            run_agent(prompt, message.chat.id, username, _status_cb, _send_cb,
                      is_owner=is_owner_user, initial_files={filename: file_bytes}),
            timeout=300,
        )
    except asyncio.TimeoutError:
        agent_text, agent_project = 'Завис по таймауту.', None
    except Exception as _e:
        logger.exception(f'Media agent error: {_e}')
        agent_text, agent_project = 'Агент упал. Попробуй ещё раз.', None
    try:
        await thinking_msg.delete()
    except Exception:
        pass
    if agent_project:
        await _send_generated_project(message, agent_project, reply_kwargs)
    elif agent_text:
        import bleach as _bl2
        _TAGS2 = ['b','strong','i','em','u','ins','s','code','pre','blockquote','tg-spoiler']
        safe_html = _bl2.clean(agent_text, tags=_TAGS2,
                               attributes={'code': ['class']}, strip=True)
        try:
            await safe_send(message.reply, safe_html, parse_mode='HTML', **reply_kwargs)
        except Exception:
            await safe_send(message.reply, _clean_plain_reply(agent_text), **reply_kwargs)
    asyncio.create_task(add_user_stat(message.from_user.id, username,
                                      message.from_user.first_name or 'Аноним', 'text'))
    asyncio.create_task(log_prompt(message.from_user.id, username,
                                   message.from_user.first_name or 'Аноним', 'text', caption))
    return True


@router.message(F.photo & ~F.caption.startswith('/'))
async def handle_album_photo(message: types.Message):
    if not message.media_group_id:
        bot_user = await message.bot.get_me()
        is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
        is_mentioned = bool(bot_user.username and f'@{bot_user.username}' in (message.caption or ''))
        is_private = message.chat.type == 'private'
        if not is_private and not is_reply_to_bot and not is_mentioned:
            return
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            return
        reply_kwargs = {}
        if message.chat.is_forum and message.message_thread_id:
            reply_kwargs['message_thread_id'] = message.message_thread_id
        prompt = message.caption or ''
        if bot_user.username:
            prompt = prompt.replace(f'@{bot_user.username}', '').strip()
        photo = message.photo[-1]
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        image_bytes = downloaded.read()
        # Route to agent if caption is an agent task
        if await _media_to_agent(message, image_bytes, 'photo.jpg', prompt, reply_kwargs):
            return
        if not prompt:
            prompt = 'Что на этом фото?'
        wait_msg = await message.reply('⏳ Смотрю на твою хуйню...', **reply_kwargs)
        await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
        response = await analyze_photo_with_gemini(image_bytes, prompt)
        await wait_msg.delete()
        await message.reply(response or 'Нихуя не понял что это такое.', **reply_kwargs)
        asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
        asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))
        return
    group_id = message.media_group_id
    if group_id not in pending_media_groups:
        return
    photo = message.photo[-1]
    try:
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        pending_media_groups[group_id]['images'].append(downloaded.read())
    except Exception:
        pass

@router.callback_query(F.data.startswith('imgprov:'))
async def handle_provider_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    (_, request_id, provider) = parts
    request_data = pending_image_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел. Отправьте /image заново.', show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса может выбирать.', show_alert=True)
        return
    if provider == 'gpt' and callback.message and (callback.message.chat.id == TEXT_ONLY_CHAT_ID):
        await callback.answer('GPT недоступен в этой беседе.', show_alert=True)
        return
    uid = callback.from_user.id
    if provider in ('gemini', 'gpt') and uid not in ALLOWED_USER_IDS:
        is_main_member = False
        try:
            m = await callback.bot.get_chat_member(chat_id=CHAT_ID, user_id=uid)
            is_main_member = m.status in ('member', 'administrator', 'creator', 'restricted')
        except Exception:
            is_main_member = True
        if not is_main_member:
            (allowed, remaining) = _check_daily_limit(uid, callback.message.chat.id if callback.message else request_data['chat_id'])
            if not allowed:
                (req_limit, days) = chat_custom_limits.get(callback.message.chat.id if callback.message else request_data['chat_id'], (DAILY_GEN_LIMIT, 1))
                await callback.answer(f'❌ Лимит {req_limit} генерации за {days} дн. исчерпан.\nДля безлимита свяжитесь с {PAYMENT_USERNAME} и пополните карту Тбанка на 10₽.', show_alert=True)
                return
    models = PROVIDER_MODELS.get(provider, [])
    if not models:
        await callback.answer('Неизвестный провайдер.', show_alert=True)
        return
    rows = []
    for (label, mid) in models:
        rows.append([InlineKeyboardButton(text=label, callback_data=f'imgsel:{request_id}:{mid}')])
    rows.append([InlineKeyboardButton(text='← Назад', callback_data=f'imgback:{request_id}')])
    provider_names = {'gemini': 'Gemini', 'gpt': 'GPT', 'flux': 'FLUX (NVIDIA)', 'nsfw': 'Replicate'}
    await callback.answer()
    try:
        await callback.message.edit_text(f'Выберите модель {provider_names.get(provider, provider)}:', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass

@router.callback_query(F.data.startswith('imgback:'))
async def handle_provider_back(callback: types.CallbackQuery):
    if not callback.data:
        return
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    request_data = pending_image_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    keyboard = _providers_keyboard(request_id, request_data['chat_id'], len(request_data.get('images_bytes') or []))
    try:
        await callback.message.edit_text('Через какую модель хотите сгенерировать фото?', reply_markup=keyboard)
    except Exception:
        pass

@router.callback_query(F.data.startswith('imgsel:'))
async def handle_image_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer('Некорректные данные кнопки.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Некорректные данные кнопки.', show_alert=True)
        return
    (_, request_id, model_id) = parts
    request_data = pending_image_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел. Отправьте /image заново.', show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Эту кнопку может нажать только тот, кто отправил /image.', show_alert=True)
        return
    model_info = MODEL_TO_REAL.get(model_id)
    if not model_info:
        await callback.answer('Неизвестная модель.', show_alert=True)
        return
    (provider, real_model) = model_info
    selected_label = next((l for lst in PROVIDER_MODELS.values() for (l, m) in lst if next((k for (k, (p, rm)) in MODEL_TO_REAL.items() if rm == m and p == provider), None) == model_id), real_model)
    await callback.answer()
    if provider == 'gemini':
        request_data['selected_model'] = real_model
        request_data['selected_provider'] = provider
        request_data['selected_label'] = selected_label
        try:
            await callback.message.edit_text(_temp_message(), reply_markup=_temp_keyboard(request_id))
        except Exception:
            pass
        return
    if provider == 'nsfw':
        pending_image_requests.pop(request_id, None)
        imgs = request_data.get('images_bytes') or ([request_data['image_bytes']] if request_data.get('image_bytes') else None)
        pending_nsfw_configs[request_id] = {'user_id': request_data['user_id'], 'chat_id': request_data['chat_id'], 'source_message_id': request_data['source_message_id'], 'message_thread_id': request_data['message_thread_id'], 'prompt': request_data['prompt'], 'model': real_model, 'label': selected_label, 'image_bytes': imgs[0] if imgs else None, 'cfg': _nsfw_default_cfg(real_model)}
        try:
            await callback.message.edit_text(_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
        except Exception:
            pass
        return
    pending_image_requests.pop(request_id, None)
    message_thread_id = request_data['message_thread_id']
    reply_kwargs = {}
    if message_thread_id:
        reply_kwargs['message_thread_id'] = message_thread_id
    await callback.bot.send_chat_action(chat_id=request_data['chat_id'], action='upload_photo', message_thread_id=message_thread_id)
    progress_task = None
    state_data = {'status': 'Инициализация...'}
    if callback.message:
        try:
            await callback.message.edit_text('⏳ Запускаю генерацию...')
            progress_task = asyncio.create_task(run_progress_bar(callback.bot, request_data['chat_id'], callback.message.message_id, selected_label, state_data=state_data))
        except Exception:
            pass
    imgs = request_data.get('images_bytes') or ([request_data['image_bytes']] if request_data.get('image_bytes') else None)
    gen_id = f'img_{request_id}'
    try:
        await save_pending_gen(gen_id=gen_id, gen_type='image', user_id=request_data['user_id'], chat_id=request_data['chat_id'], source_message_id=request_data['source_message_id'], message_thread_id=request_data['message_thread_id'], prompt=request_data['prompt'], model=real_model, provider=provider, file_ids=request_data.get('file_ids', []), model_label=selected_label)
        if provider == 'gpt':
            (result_img, error_msg) = await generate_image_with_gpt(request_data['prompt'], images_bytes=imgs, model=real_model, state_data=state_data)
        elif provider == 'flux':
            (result_img, error_msg) = await generate_image_with_nvidia(request_data['prompt'], model=real_model, state_data=state_data)
        elif provider == 'nsfw':
            (result_img, error_msg) = await generate_image_with_replicate(request_data['prompt'], model=real_model, state_data=state_data)
        else:
            (result_img, error_msg) = await generate_image_with_gemini(request_data['prompt'], images_bytes=imgs, model=real_model, temperature=request_data.get('temperature', 1.0), state_data=state_data)
    except Exception as e:
        logger.exception(f"Критическая ошибка во время генерации изображения: {e}")
        (result_img, error_msg) = (None, f"Внутренняя ошибка сервера: {type(e).__name__}: {e}")
    finally:
        if progress_task:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
        await delete_pending_gen(gen_id)
    await _send_generation_result(callback.bot, request_data, request_id, result_img, error_msg, selected_label, imgs, reply_kwargs)
VEO_MODELS: dict = {'veo0': ('Veo 2', 'veo-2.0-generate-001'), 'veo1': ('Veo 3.1 Fast', 'veo-3.1-fast-generate-preview'), 'veo2': ('Veo 3.1', 'veo-3.1-generate-preview'), 'veo3': ('Veo 3.1 Lite', 'veo-3.1-lite-generate-preview')}
VIDEO_COOLDOWN = 60

@router.message(Command("up"))
async def cmd_up(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply('Доступ запрещен.')
        return

    if not message.photo:
        await message.reply("Прикрепи фото для апскейла.")
        return
        
    wait_msg = await message.reply("⏳ Скачиваю фото...")
    photo = message.photo[-1]
    file_info = await message.bot.get_file(photo.file_id)
    downloaded = await message.bot.download_file(file_info.file_path)
    image_bytes = downloaded.read()
    
    await wait_msg.edit_text("⬆️ Улучшаю качество через AI upscaler...")
    
    upscaled, up_err = await upscale_image(image_bytes)
    
    try:
        await wait_msg.delete()
    except Exception:
        pass
        
    if upscaled:
        await message.reply_document(
            document=BufferedInputFile(upscaled, filename="upscaled.png"),
            caption="✨ Улучшенная версия 2x — без сжатия"
        )
    else:
        await message.reply(f"❌ Ошибка апскейла: {up_err}")

@router.message(Command('video'))
async def cmd_video(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply('Доступ запрещен.')
        return
    current_time = time.time()
    last_time = user_video_cooldowns.get(message.from_user.id, 0)
    if current_time - last_time < VIDEO_COOLDOWN:
        await message.reply(f'Не спамь блять видосами, подожди еще {int(VIDEO_COOLDOWN - (current_time - last_time))} сек.')
        return
    user_video_cooldowns[message.from_user.id] = current_time
    prompt = (message.text or '').replace('/video', '').strip()
    if message.caption:
        prompt = message.caption.replace('/video', '').strip()
    if not prompt and (not message.photo):
        await message.reply('Напиши промпт после команды, например:\n/video закат над морем\n\nИли прикрепи фото с подписью /video анимируй это — Veo оживит картинку.')
        return
    image_bytes = None
    if message.photo:
        photo = message.photo[-1]
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        image_bytes = downloaded.read()
    request_id = uuid.uuid4().hex[:10]
    pending_video_requests[request_id] = {'user_id': message.from_user.id, 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': message.message_thread_id if message.chat.is_forum else None, 'prompt': prompt, 'image_bytes': image_bytes}
    rows = [[InlineKeyboardButton(text=label, callback_data=f'veosel:{request_id}:{mid}')] for (mid, (label, _)) in VEO_MODELS.items()]
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs['message_thread_id'] = message.message_thread_id
    await message.reply('Выберите модель Veo для генерации видео:', reply_markup=keyboard, **reply_kwargs)

@router.callback_query(F.data.startswith('veosel:'))
async def handle_veo_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    (_, request_id, model_id) = parts
    request_data = pending_video_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел. Отправьте /video заново.', show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса может выбирать.', show_alert=True)
        return
    model_info = VEO_MODELS.get(model_id)
    if not model_info:
        await callback.answer('Неизвестная модель.', show_alert=True)
        return
    (model_label, real_model) = model_info
    pending_video_requests.pop(request_id, None)
    await callback.answer()
    message_thread_id = request_data['message_thread_id']
    reply_kwargs = {}
    if message_thread_id:
        reply_kwargs['message_thread_id'] = message_thread_id
    progress_task = None
    state_data = {'status': 'Инициализация...'}
    if callback.message:
        try:
            await callback.message.edit_text('⏳ Запускаю генерацию видео...')
            progress_task = asyncio.create_task(run_progress_bar(callback.bot, request_data['chat_id'], callback.message.message_id, model_label, state_data=state_data))
        except Exception:
            pass
    await callback.bot.send_chat_action(chat_id=request_data['chat_id'], action='upload_video', message_thread_id=message_thread_id)
    gen_id = f'veo_{request_id}'
    try:
        (op_name, api_key, start_err) = await start_veo_generation(request_data['prompt'], model=real_model, image_bytes=request_data.get('image_bytes'), state_data=state_data)
        if op_name:
            await save_pending_gen(gen_id=gen_id, gen_type='video', user_id=request_data['user_id'], chat_id=request_data['chat_id'], source_message_id=request_data['source_message_id'], message_thread_id=request_data['message_thread_id'], prompt=request_data['prompt'], model=real_model, provider='veo', veo_operation_name=op_name, veo_api_key=api_key, model_label=model_label)
            (video_bytes, error_msg) = await poll_veo_operation(op_name, api_key, state_data=state_data)
        else:
            (video_bytes, error_msg) = (None, start_err)
    except Exception as e:
        logger.exception(f"Критическая ошибка во время генерации Veo: {e}")
        (video_bytes, error_msg) = (None, f"Внутренняя ошибка сервера: {type(e).__name__}: {e}")
    finally:
        if progress_task:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
        await delete_pending_gen(gen_id)
    if error_msg:
        error_sent_msg = await safe_send(callback.bot.send_message, chat_id=request_data['chat_id'], text=f'❌ Ошибка генерации видео:\n{error_msg}\n\n⏳ Ща спрошу у мозгов, че не так...', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        image_for_explain = request_data.get('image_bytes')
        try:
            explanation = await asyncio.wait_for(explain_generation_error(request_data['prompt'], error_msg, image_bytes=image_for_explain), timeout=30)
        except Exception as e:
            logging.warning(f'Video error explanation failed: {type(e).__name__}: {e}')
            explanation = ''
        if not explanation:
            explanation = _fallback_generation_error_explanation(error_msg)
        if error_sent_msg:
            try:
                await safe_send(callback.bot.edit_message_text, chat_id=request_data['chat_id'], message_id=error_sent_msg.message_id, text=f'❌ Ошибка генерации видео:\n{error_msg}\n\n🧠 Пояснение:\n{explanation}')
            except Exception:
                pass
        return
    if video_bytes:
        video_file = BufferedInputFile(video_bytes, filename='generated.mp4')
        caption = make_safe_caption(f"🎬 Видео ({model_label}) по запросу: ", request_data['prompt'])
        await callback.bot.send_video(chat_id=request_data['chat_id'], video=video_file, caption=caption, reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        asyncio.create_task(add_user_stat(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'video'))
        asyncio.create_task(log_prompt(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'video', request_data.get('prompt', '')))
        return
    await callback.bot.send_message(chat_id=request_data['chat_id'], text='❌ Не удалось получить видео.', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
TTS_VOICES = ['Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede', 'Callirrhoe', 'Autonoe', 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba', 'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar', 'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi', 'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat']

@router.message(Command('tts'))
async def cmd_tts(message: types.Message):
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member and message.chat.type != 'private':
        await message.reply('Доступ запрещен.')
        return
    current_time = time.time()
    last_time = user_video_cooldowns.get(message.from_user.id, 0)
    if current_time - last_time < 30:
        await message.reply(f'Подожди еще {int(30 - (current_time - last_time))} сек.')
        return
    user_video_cooldowns[message.from_user.id] = current_time
    prompt = (message.text or '').replace('/tts', '').strip()
    if not prompt:
        await message.reply('Напиши текст после команды, например:\n/tts Привет, ублюдок!')
        return
    request_id = uuid.uuid4().hex[:10]
    thread_id = message.message_thread_id if message.chat.is_forum else None
    pending_tts_requests[request_id] = {'user_id': message.from_user.id, 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': thread_id, 'prompt': prompt, 'username': message.from_user.username or '', 'first_name': message.from_user.first_name or 'Аноним'}
    if not TTS_MODELS:
        await message.reply('Нет доступных TTS моделей.')
        return
    rows = [[InlineKeyboardButton(text=label, callback_data=f'ttssel:{request_id}:{mid}')] for (mid, (label, _)) in TTS_MODELS.items()]
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    reply_kwargs = {}
    if message.chat.is_forum and thread_id:
        reply_kwargs['message_thread_id'] = thread_id
    await message.reply('Выберите модель TTS:', reply_markup=keyboard, **reply_kwargs)

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
_TTS_LANGS = [('🇷🇺 RU', 'ru-RU'), ('🇬🇧 EN', 'en-US'), ('🇯🇵 JA', 'ja-JP')]
_TTS_TEMPS = [0.1, 0.5, 1.0, 1.5, 2.0]

def _tts_cfg_keyboard(request_id: str) -> InlineKeyboardMarkup:
    d = pending_tts_configs.get(request_id, {})
    cfg = d.get('cfg', {})
    cur_voice = cfg.get('voice', 'Puck')
    cur_temp = cfg.get('temp', 1.0)
    cur_lang = cfg.get('lang', 'ru-RU')

    def make_voice_row(voices):
        return [InlineKeyboardButton(text=f"{('✅' if v == cur_voice else '')}{v}", callback_data=f'ttscfg:{request_id}:voice:{v}') for v in voices]
    rows = []
    rows.append([InlineKeyboardButton(text='✏️ Текст', callback_data=f'ttsinput:{request_id}:prompt'), InlineKeyboardButton(text='🎭 Сцена', callback_data=f'ttsinput:{request_id}:scene')])
    rows.append([InlineKeyboardButton(text='🎭 Стиль', callback_data=f'ttsinput:{request_id}:style'), InlineKeyboardButton(text='🎭 Темп', callback_data=f'ttsinput:{request_id}:pace'), InlineKeyboardButton(text='🎭 Акцент', callback_data=f'ttsinput:{request_id}:accent')])
    rows.append([InlineKeyboardButton(text='— Температура —', callback_data='noop')])
    rows.append([InlineKeyboardButton(text=f"{('✅' if str(t) == str(cur_temp) else '')}{t}", callback_data=f'ttscfg:{request_id}:temp:{t}') for t in _TTS_TEMPS])
    rows.append([InlineKeyboardButton(text='— Язык —', callback_data='noop')])
    rows.append([InlineKeyboardButton(text=f"{('✅' if l == cur_lang else '')}{name}", callback_data=f'ttscfg:{request_id}:lang:{l}') for (name, l) in _TTS_LANGS])
    rows.append([InlineKeyboardButton(text='— Голос —', callback_data='noop')])
    for i in range(0, 15, 3):
        rows.append(make_voice_row(TTS_VOICES[i:i + 3]))
    rows.append([InlineKeyboardButton(text='🔊 Прослушать голос', callback_data=f'ttsprev:{request_id}')])
    rows.append([InlineKeyboardButton(text='🚀 Генерировать', callback_data=f'ttsgen:{request_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data.startswith('ttssel:'))
async def handle_tts_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    (_, request_id, model_id) = parts
    request_data = pending_tts_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел. Отправьте /tts заново.', show_alert=True)
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    model_info = TTS_MODELS.get(model_id)
    if not model_info:
        await callback.answer('Неизвестная модель.', show_alert=True)
        return
    (model_label, real_model) = model_info
    pending_tts_requests.pop(request_id, None)
    pending_tts_configs[request_id] = {**request_data, 'model': real_model, 'label': model_label, 'cfg': {'voice': 'Puck'}}
    await callback.answer()
    try:
        await callback.message.edit_text(_tts_cfg_text(request_id), reply_markup=_tts_cfg_keyboard(request_id))
    except Exception:
        pass
_tts_awaiting_input: dict = {}

@router.callback_query(F.data.startswith('ttsinput:'))
async def handle_tts_input(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 3:
        return
    (_, request_id, field) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    field_prompts = {'prompt': '✏️ Напиши **текст для озвучки** следующим сообщением:\n\n(просто отправь текст в чат)', 'scene': '🎭 Опиши **сцену (окружение)** следующим сообщением:\n\nЭто задаст общий вайб и акустику. Примеры:\n🇷🇺 <i>Шумное кафе ранним утром, играет тихий джаз на фоне.</i>\n🇬🇧 <i>A busy train station with echoing announcements.</i>\n\n(отправь текст или нажми Отмена)', 'style': '🎭 Опиши **стиль (настроение)** следующим сообщением:\n\nУказывает эмоцию и характер речи. Примеры:\n🇷🇺 <i>радостно, агрессивно, шепотом, уставший.</i>\n🇬🇧 <i>energetic and upbeat, angry, whispering, tired.</i>\n\n(отправь текст или нажми Отмена)', 'pace': '🎭 Укажи **темп речи** следующим сообщением:\n\nС какой скоростью говорить. Примеры:\n🇷🇺 <i>очень быстро, медленно с длинными паузами, размеренно.</i>\n🇬🇧 <i>very fast, slow with dramatic pauses, steady.</i>\n\n(отправь текст или нажми Отмена)', 'accent': '🎭 Укажи **акцент или манеру** следующим сообщением:\n\nПримеры:\n🇷🇺 <i>с британским акцентом, французский акцент, грубый голос.</i>\n🇬🇧 <i>British accent, Southern US drawl, French accent.</i>\n\n(отправь текст или нажми Отмена)'}
    prompt_text = field_prompts.get(field, f'✏️ Напиши {field} следующим сообщением:')
    _tts_awaiting_input[d['chat_id'], d['user_id']] = {'request_id': request_id, 'field': field, 'msg_id': callback.message.message_id}
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data=f'ttscancel:{request_id}')]])
    try:
        await callback.message.edit_text(prompt_text, reply_markup=cancel_kb, parse_mode='HTML')
    except Exception:
        pass

@router.callback_query(F.data.startswith('ttscancel:'))
async def handle_tts_cancel(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    _tts_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    await callback.answer('Отменено')
    try:
        await callback.message.edit_text(_tts_cfg_text(request_id), reply_markup=_tts_cfg_keyboard(request_id))
    except Exception:
        pass

@router.callback_query(F.data.startswith('ttscfg:'))
async def handle_tts_config(callback: types.CallbackQuery):
    parts = callback.data.split(':', 3)
    if len(parts) != 4:
        await callback.answer()
        return
    (_, request_id, field, value) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    if field == 'voice':
        d['cfg']['voice'] = value
    elif field == 'temp':
        d['cfg']['temp'] = float(value)
    elif field == 'lang':
        d['cfg']['lang'] = value
    await callback.answer()
    try:
        await callback.message.edit_text(_tts_cfg_text(request_id), reply_markup=_tts_cfg_keyboard(request_id))
    except Exception:
        pass

@router.callback_query(F.data.startswith('ttsprev:'))
async def handle_tts_preview(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    voice = d['cfg'].get('voice', 'Puck')
    lang = d['cfg'].get('lang', 'ru-RU')
    temp = d['cfg'].get('temp', 1.0)
    cache_key = f'{voice}_{lang}_{temp}'
    cached_file_id = tts_voice_previews.get(cache_key)
    await callback.answer()
    reply_kwargs = {'message_thread_id': d.get('message_thread_id')} if d.get('message_thread_id') else {}
    if cached_file_id:
        await callback.bot.send_voice(chat_id=d['chat_id'], voice=cached_file_id, caption=f'🔊 Проверка: {voice} ({lang})', reply_to_message_id=d['source_message_id'], **reply_kwargs)
        return
    await callback.bot.send_chat_action(chat_id=d['chat_id'], action='record_voice', message_thread_id=d.get('message_thread_id'))
    test_phrases = {'ru-RU': 'Привет! Это проверка моего голоса. Как меня слышно?', 'en-US': 'Hello! This is a test of my voice. How do I sound?', 'ja-JP': 'こんにちは！これは私の声のテストです。どう聞こえますか？'}
    phrase = test_phrases.get(lang, test_phrases['ru-RU'])
    (audio_bytes, error_msg) = await generate_tts_with_gemini(phrase, d['model'], voice, temp, lang)
    if error_msg:
        await callback.bot.send_message(chat_id=d['chat_id'], text=f'❌ Ошибка предпрослушивания:\n{error_msg}', reply_to_message_id=d['source_message_id'], **reply_kwargs)
        return
    if audio_bytes:
        voice_file = BufferedInputFile(audio_bytes, filename='voice.ogg')
        sent_msg = await callback.bot.send_voice(chat_id=d['chat_id'], voice=voice_file, caption=f'🔊 Проверка: {voice} ({lang})', reply_to_message_id=d['source_message_id'], **reply_kwargs)
        if sent_msg.voice and sent_msg.voice.file_id:
            tts_voice_previews[cache_key] = sent_msg.voice.file_id

@router.callback_query(F.data.startswith('ttsgen:'))
async def handle_tts_generate(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_tts_configs.pop(request_id, None)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    await callback.answer()
    reply_kwargs = {'message_thread_id': d.get('message_thread_id')} if d.get('message_thread_id') else {}
    try:
        await callback.message.edit_text(f"⏳ Генерирую аудио через {d['label']}...")
    except Exception:
        pass
    await callback.bot.send_chat_action(chat_id=d['chat_id'], action='record_voice', message_thread_id=d.get('message_thread_id'))
    cfg = d.get('cfg', {})
    (audio_bytes, error_msg) = await generate_tts_with_gemini(d['prompt'], d['model'], cfg.get('voice', 'Puck'), cfg.get('temp', 1.0), cfg.get('lang', 'ru-RU'))
    try:
        await callback.message.delete()
    except Exception:
        pass
    if error_msg:
        await callback.bot.send_message(chat_id=d['chat_id'], text=f'❌ Ошибка генерации аудио:\n{error_msg}', reply_to_message_id=d['source_message_id'], **reply_kwargs)
        return
    if audio_bytes:
        voice_file = BufferedInputFile(audio_bytes, filename='voice.ogg')
        await callback.bot.send_voice(chat_id=d['chat_id'], voice=voice_file, caption=f"🎙️ {d['label']} | Голос: {d['cfg'].get('voice', 'Puck')}", reply_to_message_id=d['source_message_id'], **reply_kwargs)
        asyncio.create_task(add_user_stat(d['user_id'], d.get('username', ''), d.get('first_name', 'Аноним'), 'audio'))
        asyncio.create_task(log_prompt(d['user_id'], d.get('username', ''), d.get('first_name', 'Аноним'), 'audio', d['prompt']))
    else:
        await callback.bot.send_message(chat_id=d['chat_id'], text='❌ Не удалось получить аудио.', reply_to_message_id=d['source_message_id'], **reply_kwargs)

@router.message(F.voice | F.audio | F.video_note)
async def handle_voice_audio(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bool(bot_user.username and f'@{bot_user.username}' in (message.caption or ''))
    is_private = message.chat.type == 'private'
    if not is_private and not is_reply_to_bot and not is_mentioned:
        return
    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs['message_thread_id'] = message.message_thread_id
    prompt = message.caption or ''
    if bot_user.username:
        prompt = prompt.replace(f'@{bot_user.username}', '').strip()
    if message.video_note:
        if not prompt:
            prompt = 'Что происходит в этом видео?'
        wait_msg = await message.reply('⏳ Смотрю твоё кружочек-видео...', **reply_kwargs)
        file_info = await message.bot.get_file(message.video_note.file_id)
        (_, temp_path) = tempfile.mkstemp(suffix='.mp4')
        await message.bot.download_file(file_info.file_path, destination=temp_path)
        await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
        response = await generate_video_with_gemini(prompt, temp_path)
        if os.path.exists(temp_path):
            os.remove(temp_path)
        await wait_msg.delete()
        await message.reply(response or 'Нихуя не понял.', **reply_kwargs)
        asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
        asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))
        return
    media = message.voice or message.audio
    mime_type = 'audio/ogg' if message.voice else (media.mime_type or 'audio/mpeg')
    file_info = await message.bot.get_file(media.file_id)
    downloaded = await message.bot.download_file(file_info.file_path)
    audio_bytes = downloaded.read()
    ext = 'ogg' if message.voice else 'mp3'
    if prompt and await _media_to_agent(message, audio_bytes, f'audio.{ext}', prompt, reply_kwargs):
        return
    if not prompt:
        prompt = 'Что сказано в этом голосовом? Транскрибируй и ответь по существу.'
    wait_msg = await message.reply('⏳ Слушаю твою хуйню...', **reply_kwargs)
    await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
    response = await analyze_voice_with_gemini(audio_bytes, mime_type, prompt)
    await wait_msg.delete()
    await message.reply(response or 'Нихуя не расслышал.', **reply_kwargs)
    asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
    asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))

@router.message(F.video | F.animation | F.document)
async def handle_video(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bool(bot_user.username and f'@{bot_user.username}' in (message.caption or ''))
    is_private = message.chat.type == 'private'
    if not is_private and not is_reply_to_bot and not is_mentioned:
        return
    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs['message_thread_id'] = message.message_thread_id
    vid = message.video or message.animation
    if not vid and message.document:
        if not message.document.mime_type or not message.document.mime_type.startswith('video/'):
            await _handle_file_document_upload(message, bot_user, reply_kwargs)
            return
        vid = message.document
    prompt = message.caption or ''
    if bot_user.username:
        prompt = prompt.replace(f'@{bot_user.username}', '').strip()
    # Route to agent if caption is an agent task
    if prompt:
        file_info_pre = await message.bot.get_file(vid.file_id)
        if vid.file_size and vid.file_size < 20 * 1024 * 1024:  # only preload <20MB
            buf_pre = await message.bot.download_file(file_info_pre.file_path)
            vid_bytes = buf_pre.read()
            ext = 'mp4'
            if await _media_to_agent(message, vid_bytes, f'video.{ext}', prompt, reply_kwargs):
                return
    if not prompt:
        prompt = 'Внимательно посмотри это видео и скажи, что здесь происходит.'
    wait_msg = await message.reply('⏳ Изучаю твое всратое видео кадр за кадром (24 FPS)...')
    file_info = await message.bot.get_file(vid.file_id)
    (_, temp_vid_path) = tempfile.mkstemp(suffix='.mp4')
    await message.bot.download_file(file_info.file_path, destination=temp_vid_path)
    await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
    text_response = await generate_video_with_gemini(prompt, temp_vid_path)
    if os.path.exists(temp_vid_path):
        os.remove(temp_vid_path)
    await wait_msg.delete()
    code_blocks = re.findall('```(\\w*)\\n(.*?)```', text_response, re.DOTALL)
    cleaned_text = re.sub('```(\\w*)\\n(.*?)```', '', text_response, flags=re.DOTALL).strip()
    if not cleaned_text and code_blocks:
        cleaned_text = 'Вот твой ебаный код, подавись нахуй.'
    elif not cleaned_text:
        cleaned_text = 'Нихуя не понял, но иди в пизду.'
    sent_msg = await message.reply(cleaned_text, **reply_kwargs)
    if code_blocks:
        for (lang, code) in code_blocks:
            ext = lang.strip().lower() or 'txt'
            if ext in ['python', 'py']:
                ext = 'py'
            elif ext in ['javascript', 'js']:
                ext = 'js'
            elif ext in ['typescript', 'ts']:
                ext = 'ts'
            elif ext in ['html', 'htm']:
                ext = 'html'
            elif ext in ['css']:
                ext = 'css'
            elif ext in ['c++', 'cpp']:
                ext = 'cpp'
            elif ext in ['c#', 'cs']:
                ext = 'cs'
            elif ext in ['php']:
                ext = 'php'
            elif ext in ['bash', 'sh']:
                ext = 'sh'
            elif ext in ['json']:
                ext = 'json'
            elif ext in ['xml']:
                ext = 'xml'
            filename = f'говняный_код_{uuid.uuid4().hex[:4]}.{ext}'
            doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
            await message.bot.send_document(chat_id=message.chat.id, document=doc, reply_to_message_id=sent_msg.message_id, **reply_kwargs)
    asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
    asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))

def _track_user(message: types.Message):
    if message.from_user and message.chat.type != 'private':
        cid = message.chat.id
        uid = message.from_user.id
        if cid not in chat_members_cache:
            chat_members_cache[cid] = {}
        chat_members_cache[cid][uid] = (message.from_user.first_name or 'Аноним', message.from_user.username)

@router.message(Command('figma'))
async def cmd_figma(message: types.Message):
    _track_user(message)
    uid = message.from_user.id
    if is_banned(uid):
        return
    prompt = (message.text or '').replace('/figma', '', 1).strip()
    if not prompt:
        await message.reply('Напиши что рисовать, дебил. Пример: /figma синяя кнопка с текстом ОК')
        return
    thread_id = message.message_thread_id if message.chat.is_forum else None
    reply_kwargs = {'message_thread_id': thread_id} if thread_id else {}
    thinking_msg = await message.reply('🎨 Генерирую дизайн в Figma...', **reply_kwargs)
    try:
        spec_prompt = (
            'Ты — профессиональный UI/UX дизайнер. Сгенерируй JSON-спецификацию дизайна для Figma Plugin API.\n'
            'ВЕРНИ ТОЛЬКО JSON БЕЗ ОБЪЯСНЕНИЙ И БЕЗ MARKDOWN-БЛОКОВ.\n\n'
            'Формат JSON:\n'
            '{\n'
            '  "frame": {\n'
            '    "name": "Design",\n'
            '    "width": 1280,\n'
            '    "height": 720,\n'
            '    "backgroundColor": {"r": 1, "g": 1, "b": 1}\n'
            '  },\n'
            '  "nodes": [\n'
            '    {"type": "RECTANGLE", "name": "bg", "x": 0, "y": 0, "width": 1280, "height": 720,\n'
            '     "fill": {"r": 0.1, "g": 0.1, "b": 0.9}, "cornerRadius": 0},\n'
            '    {"type": "TEXT", "name": "title", "x": 100, "y": 200, "width": 600,\n'
            '     "content": "Заголовок", "fontSize": 48, "fontStyle": "Bold",\n'
            '     "color": {"r": 1, "g": 1, "b": 1}},\n'
            '    {"type": "ELLIPSE", "name": "circle", "x": 900, "y": 300, "width": 200, "height": 200,\n'
            '     "fill": {"r": 1, "g": 0.5, "b": 0}}\n'
            '  ]\n'
            '}\n\n'
            'Доступные типы нод: RECTANGLE, ELLIPSE, TEXT, LINE.\n'
            'Цвета — float от 0 до 1. fontSize — число. fontStyle: "Regular" или "Bold".\n'
            'Сделай красивый, насыщенный дизайн с минимум 6-10 нодами, используй градиентные блоки, типографику, геометрию.\n'
            f'Описание дизайна: {prompt}'
        )
        raw = await asyncio.wait_for(
            generate_text_with_gemini(spec_prompt, message.chat.id, username='figma_gen', web_query=None),
            timeout=60
        )
        json_match = re.search(r'```(?:json)?\s*\n([\s\S]*?)```', raw)
        if json_match:
            raw = json_match.group(1).strip()
        else:
            brace = raw.find('{')
            if brace >= 0:
                raw = raw[brace:]
        spec = json.loads(raw)

        from figma_bridge import enqueue_and_wait
        session_id = uuid.uuid4().hex
        await thinking_msg.edit_text('🎨 Жду пока плагин создаст дизайн в Figma...')
        node_id = await enqueue_and_wait(session_id, spec, timeout=120.0)

        if node_id is None:
            try:
                await thinking_msg.edit_text(
                    '⏰ Плагин не ответил за 2 минуты.\n'
                    'Убедись что плагин NanoHatani Bridge запущен в Figma и файл открыт.'
                )
            except Exception:
                pass
            return

        from config import FIGMA_TOKEN
        import aiohttp as _aiohttp
        file_key = spec.get('file_key', '')
        node_id_enc = node_id.replace(':', '-').replace(';', '-')
        render_url = f'https://api.figma.com/v1/images/{file_key}?ids={node_id}&format=png&scale=2' if file_key else None

        png_bytes = None
        if render_url:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(render_url, headers={'X-Figma-Token': FIGMA_TOKEN}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        img_url = (data.get('images') or {}).get(node_id) or (data.get('images') or {}).get(node_id.replace(':', '-'))
                        if img_url:
                            async with sess.get(img_url) as img_resp:
                                if img_resp.status == 200:
                                    png_bytes = await img_resp.read()

        try:
            await thinking_msg.delete()
        except Exception:
            pass

        if png_bytes:
            doc = BufferedInputFile(png_bytes, filename=f'figma_{uuid.uuid4().hex[:6]}.png')
            await safe_send(
                message.bot.send_document,
                chat_id=message.chat.id,
                document=doc,
                caption=f'🎨 {prompt[:180]}',
                reply_to_message_id=message.message_id,
                **reply_kwargs
            )
        else:
            await safe_send(
                message.bot.send_message,
                chat_id=message.chat.id,
                text=f'✅ Дизайн создан в Figma (node `{node_id}`). Рендер недоступен без file_key.',
                reply_to_message_id=message.message_id,
                **reply_kwargs
            )
        logging.info(f'cmd_figma: done session={session_id} node_id={node_id} uid={uid}')
    except asyncio.TimeoutError:
        try:
            await thinking_msg.edit_text('Тайм-аут, Gemini тупит. Попробуй ещё раз.')
        except Exception:
            pass
    except Exception as _figma_err:
        logging.exception(f'cmd_figma error: {_figma_err}')
        try:
            await thinking_msg.edit_text(f'Упало: {type(_figma_err).__name__}. Попробуй ещё раз.')
        except Exception:
            pass

@router.message(Command("dual"))
async def cmd_dual(message: Message):
    from dual_bot import start_dual, BOT1_DUAL_NAME, BOT2_DUAL_NAME
    if message.chat.id != FULL_ACCESS_CHAT_ID:
        return
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    started = start_dual(chat_id, thread_id)
    if started:
        await message.reply(f"🤖 {BOT1_DUAL_NAME} vs {BOT2_DUAL_NAME} — начали базарить. /stopdual чтобы заткнуть.")
    else:
        await message.reply("Уже идёт, тупой.")


@router.message(Command("stopdual"))
async def cmd_stopdual(message: Message):
    from dual_bot import stop_dual
    if message.chat.id != FULL_ACCESS_CHAT_ID:
        return
    stopped = stop_dual(message.chat.id)
    if stopped:
        await message.reply("Заткнулись.")
    else:
        await message.reply("Никто не говорит.")


@router.message(F.text)
async def handle_text_messages(message: types.Message):
    if message.from_user and message.from_user.is_bot:
        return
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    _file_key = (message.chat.id, message.from_user.id)
    if _file_key in pending_file_tasks:
        task_data = pending_file_tasks.pop(_file_key)
        reply_kwargs = {}
        if message.chat.is_forum and message.message_thread_id:
            reply_kwargs['message_thread_id'] = message.message_thread_id
        await _process_file_task(message, task_data, message.text.strip(), reply_kwargs)
        return
    _nsfw_key = (message.chat.id, message.from_user.id)
    if _nsfw_key in _nsfw_awaiting_input:
        wait = _nsfw_awaiting_input.pop(_nsfw_key)
        request_id = wait['request_id']
        field = wait['field']
        msg_id = wait['msg_id']
        d = pending_nsfw_configs.get(request_id)
        if d:
            new_val = message.text.strip()
            if field == 'prompt':
                d['prompt'] = new_val
            elif field == 'neg':
                d['cfg']['neg'] = new_val
            elif field == 'seed':
                try:
                    d['cfg']['seed'] = int(new_val)
                except ValueError:
                    d['cfg']['seed'] = -1
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.bot.edit_message_text(chat_id=message.chat.id, message_id=msg_id, text=_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
            except Exception:
                await message.bot.send_message(chat_id=d['chat_id'], text=_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
        return
    _tts_key = (message.chat.id, message.from_user.id)
    if _tts_key in _tts_awaiting_input:
        wait = _tts_awaiting_input.pop(_tts_key)
        request_id = wait['request_id']
        field = wait['field']
        msg_id = wait['msg_id']
        d = pending_tts_configs.get(request_id)
        if d:
            new_val = message.text.strip()
            if field == 'prompt':
                d['prompt'] = new_val
            elif field in ('scene', 'style', 'pace', 'accent'):
                d['cfg'][field] = new_val
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.bot.edit_message_text(chat_id=message.chat.id, message_id=msg_id, text=_tts_cfg_text(request_id), reply_markup=_tts_cfg_keyboard(request_id))
            except Exception:
                await message.bot.send_message(chat_id=d['chat_id'], text=_tts_cfg_text(request_id), reply_markup=_tts_cfg_keyboard(request_id))
        return
    asyncio.create_task(_maybe_send_random_chat_media(message))
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bot_user.username and f'@{bot_user.username}' in message.text
    is_private = message.chat.type == 'private'
    if is_reply_to_bot or is_mentioned or is_private:
        current_time = time.time()
        last_time = user_text_cooldowns.get(message.from_user.id, 0)
        if current_time - last_time < TEXT_COOLDOWN_SECONDS:
            await message.reply(f'Заебал строчить, подожди еще {int(TEXT_COOLDOWN_SECONDS - (current_time - last_time))} сек.')
            return
        user_text_cooldowns[message.from_user.id] = current_time
        prompt = message.text
        if bot_user.username:
            prompt = prompt.replace(f'@{bot_user.username}', '').strip()
        if not prompt:
            prompt = 'Что тебе надо, хуйло?'
        web_query = prompt
        if is_reply_to_bot and message.reply_to_message.text:
            replied_text = message.reply_to_message.text[:500]
            prompt = f'[Контекст — ты написал ранее: «{replied_text}»]\n{prompt}'
        replied_code = None
        is_code_forced = False
        if is_reply_to_bot and message.reply_to_message:
            replied_code = generated_code_messages.get((message.chat.id, message.reply_to_message.message_id))
            if replied_code:
                files_ctx = '\n\n'.join(
                    f'### {f["path"]}\n```\n{f["content"][:4000]}\n```'
                    for f in replied_code.get('files', [])
                )
                prev_prompt = replied_code.get('prompt', '')
                prompt = (
                    f'[Контекст — ты ранее сгенерировал этот код по запросу: «{prev_prompt[:300]}»]\n'
                    f'[Файлы которые ты отправил:]\n{files_ctx}\n\n'
                    f'[Новый запрос/правки пользователя:]\n{web_query}'
                )
                is_code_forced = (
                    any(w in web_query.lower() for w in _CODE_MODIFY_WORDS) or
                    len(web_query.strip()) <= 10
                )
        username = message.from_user.first_name or message.from_user.username or 'Аноним'
        reply_kwargs = {}
        if message.chat.is_forum and message.message_thread_id:
            reply_kwargs['message_thread_id'] = message.message_thread_id
        thinking_msg = await message.reply('⏳ Думаю...', **reply_kwargs)
        await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)

        last_status_edit = 0.0
        last_status_text = ''

        async def _status_cb(text: str):
            nonlocal last_status_edit, last_status_text
            now = time.monotonic()
            if text == last_status_text or now - last_status_edit < 1.5:
                return
            try:
                await thinking_msg.edit_text(text, parse_mode='HTML')
                last_status_edit = time.monotonic()
                last_status_text = text
            except TelegramRetryAfter as e:
                last_status_edit = time.monotonic() + float(getattr(e, 'retry_after', 3) or 3)
            except Exception:
                try:
                    import re as _re
                    await thinking_msg.edit_text(_re.sub(r'<[^>]+>', '', text))
                except Exception:
                    pass

        # Inject replied-image context so agent can handle edits
        replied_draw = None
        if is_reply_to_bot and message.reply_to_message and message.reply_to_message.photo:
            replied_draw = generated_draw_messages.get((message.chat.id, message.reply_to_message.message_id))
            if replied_draw is None:
                replied_image = await _download_message_photo(message.bot, message.reply_to_message)
                if replied_image:
                    replied_draw = {'image_bytes': replied_image, 'prompt': message.reply_to_message.caption or ''}
            if replied_draw:
                prompt = (
                    f'[Пользователь отвечает на сгенерированную картинку. '
                    f'Оригинальный промпт: «{replied_draw.get("prompt", "")}»]\n{prompt}'
                )

        async def _agent_send_cb(media: dict):
            mtype    = media.get("type", "document")
            kw       = {"reply_to_message_id": message.message_id, **reply_kwargs}
            if mtype == "text":
                text_body = (media.get("text") or "")[:4000]
                parse_mode = media.get("parse_mode")
                await safe_send(message.bot.send_message, chat_id=message.chat.id, text=text_body,
                                parse_mode=parse_mode, **kw)
                return
            if mtype == "inline_buttons":
                import bleach as _bleach
                _TG_TAGS = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
                            'code', 'pre', 'blockquote', 'tg-spoiler', 'tg-emoji']
                raw_text = (media.get("text") or "Выбери:")[:4000]
                text_body = _bleach.clean(raw_text, tags=_TG_TAGS,
                    attributes={'pre': [], 'code': ['class'], 'tg-emoji': ['emoji-id'],
                                'blockquote': ['expandable']}, strip=True)
                rows = media.get("buttons", [])
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=btn.get("text", "?")[:64], url=btn.get("url", ""))
                     for btn in row if btn.get("url")]
                    for row in rows if row
                ])
                await safe_send(message.bot.send_message, chat_id=message.chat.id,
                                text=text_body, reply_markup=keyboard, parse_mode='HTML', **kw)
                return
            # ── Telegram API tools ──────────────────────────────────────
            if mtype == "tg_poll":
                from aiogram.types import InputPollOption
                opts = [InputPollOption(text=o[:100]) for o in (media.get("options") or [])[:10]]
                await safe_send(message.bot.send_poll, chat_id=message.chat.id,
                    question=(media.get("question") or "?")[:300], options=opts,
                    is_anonymous=media.get("is_anonymous", True),
                    allows_multiple_answers=media.get("allows_multiple_answers", False), **kw)
                return
            if mtype == "tg_location":
                lat, lon = media.get("latitude"), media.get("longitude")
                if lat is not None and lon is not None:
                    if media.get("title"):
                        await safe_send(message.bot.send_venue, chat_id=message.chat.id,
                            latitude=float(lat), longitude=float(lon),
                            title=media.get("title","")[:64], address=media.get("address","")[:256], **kw)
                    else:
                        await safe_send(message.bot.send_location, chat_id=message.chat.id,
                            latitude=float(lat), longitude=float(lon), **kw)
                return
            if mtype == "tg_react":
                from aiogram.types import ReactionTypeEmoji
                msg_id = media.get("message_id") or message.message_id
                try:
                    await message.bot.set_message_reaction(chat_id=message.chat.id,
                        message_id=msg_id, reaction=[ReactionTypeEmoji(emoji=media.get("emoji","👍"))])
                except Exception as _e:
                    logger.warning(f"tg_react failed: {_e}")
                return
            if mtype == "tg_pin":
                try:
                    msg_id = media.get("message_id") or message.message_id
                    await message.bot.pin_chat_message(chat_id=message.chat.id,
                        message_id=msg_id, disable_notification=media.get("disable_notification", False))
                except Exception as _e:
                    logger.warning(f"tg_pin failed: {_e}")
                return
            if mtype == "tg_delete":
                try:
                    await message.bot.delete_message(chat_id=message.chat.id,
                        message_id=media.get("message_id"))
                except Exception as _e:
                    logger.warning(f"tg_delete failed: {_e}")
                return
            if mtype == "tg_forward":
                # Restrict to current chat as source to prevent cross-chat exfiltration
                from_cid = int(media.get("from_chat_id") or message.chat.id)
                if from_cid != message.chat.id:
                    logger.warning(f"tg_forward blocked: cross-chat source {from_cid}")
                    return
                try:
                    await message.bot.forward_message(chat_id=message.chat.id,
                        from_chat_id=message.chat.id,
                        message_id=media.get("message_id"))
                except Exception as _e:
                    logger.warning(f"tg_forward failed: {_e}")
                return
            if mtype == "tg_get_chat_info":
                try:
                    chat = await message.bot.get_chat(message.chat.id)
                    count = await message.bot.get_chat_member_count(message.chat.id)
                    info = (f"<b>Чат:</b> {chat.title or 'N/A'}\n"
                            f"<b>ID:</b> <code>{chat.id}</code>\n"
                            f"<b>Тип:</b> {chat.type}\n"
                            f"<b>Участников:</b> {count}\n"
                            f"<b>Описание:</b> {chat.description or '—'}")
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=info, parse_mode='HTML', **kw)
                except Exception as _e:
                    logger.warning(f"tg_get_chat_info failed: {_e}")
                return
            # Admin-only actions — verify requester is admin/creator first
            _ADMIN_MTYPES = {"tg_ban", "tg_kick", "tg_restrict", "tg_pin", "tg_unpin",
                             "tg_set_chat_title", "tg_invite_link", "tg_promote"}
            if mtype in _ADMIN_MTYPES and message.chat.type != "private":
                try:
                    _req = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
                    if _req.status not in ("administrator", "creator"):
                        await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text="❌ Эту команду могут выполнять только администраторы.", **kw)
                        return
                except Exception:
                    pass
            if mtype == "tg_ban":
                try:
                    import datetime
                    until = media.get("until_date")
                    until_dt = datetime.datetime.fromtimestamp(until, tz=datetime.timezone.utc) if until else None
                    await message.bot.ban_chat_member(chat_id=message.chat.id,
                        user_id=media.get("user_id"), until_date=until_dt)
                except Exception as _e:
                    logger.warning(f"tg_ban failed: {_e}")
                return
            if mtype == "tg_kick":
                try:
                    import time as _t
                    await message.bot.ban_chat_member(chat_id=message.chat.id,
                        user_id=media.get("user_id"),
                        until_date=int(_t.time()) + 35)
                    await message.bot.unban_chat_member(chat_id=message.chat.id,
                        user_id=media.get("user_id"), only_if_banned=True)
                except Exception as _e:
                    logger.warning(f"tg_kick failed: {_e}")
                return
            if mtype == "tg_chat_action":
                try:
                    await message.bot.send_chat_action(chat_id=message.chat.id,
                        action=media.get("action", "typing"))
                except Exception as _e:
                    logger.warning(f"tg_chat_action failed: {_e}")
                return
            if mtype == "tg_restrict":
                from aiogram.types import ChatPermissions
                try:
                    until = media.get("until_date")
                    import datetime
                    until_dt = datetime.datetime.fromtimestamp(until, tz=datetime.timezone.utc) if until else None
                    perms = ChatPermissions(can_send_messages=media.get("can_send_messages", True),
                        can_send_media_messages=media.get("can_send_media", True))
                    await message.bot.restrict_chat_member(chat_id=message.chat.id,
                        user_id=media.get("user_id"), permissions=perms, until_date=until_dt)
                except Exception as _e:
                    logger.warning(f"tg_restrict failed: {_e}")
                return
            if mtype == "tg_unpin":
                try:
                    if media.get("message_id"):
                        await message.bot.unpin_chat_message(chat_id=message.chat.id,
                            message_id=media.get("message_id"))
                    else:
                        await message.bot.unpin_all_chat_messages(chat_id=message.chat.id)
                except Exception as _e:
                    logger.warning(f"tg_unpin failed: {_e}")
                return
            if mtype == "tg_invite_link":
                try:
                    link = await message.bot.create_chat_invite_link(chat_id=message.chat.id,
                        name=media.get("name"), expire_date=media.get("expire_date"),
                        member_limit=media.get("member_limit"))
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=f"🔗 Пригласительная ссылка: {link.invite_link}", **kw)
                except Exception as _e:
                    logger.warning(f"tg_invite_link failed: {_e}")
                return
            if mtype == "tg_set_chat_title":
                try:
                    await message.bot.set_chat_title(chat_id=message.chat.id,
                        title=media.get("title","")[:255])
                except Exception as _e:
                    logger.warning(f"tg_set_chat_title failed: {_e}")
                return
            if mtype == "tg_copy_message":
                try:
                    # Restrict to current chat as source
                    await message.bot.copy_message(chat_id=message.chat.id,
                        from_chat_id=message.chat.id,
                        message_id=media.get("message_id"), caption=media.get("caption"))
                except Exception as _e:
                    logger.warning(f"tg_copy_message failed: {_e}")
                return
            if mtype == "tg_send_animation":
                try:
                    await safe_send(message.bot.send_animation, chat_id=message.chat.id,
                        animation=media.get("url",""), caption=(media.get("caption",""))[:1024], **kw)
                except Exception as _e:
                    logger.warning(f"tg_send_animation failed: {_e}")
                return
            if mtype == "tg_send_video_note":
                try:
                    await safe_send(message.bot.send_video_note, chat_id=message.chat.id,
                        video_note=media.get("file_id",""), **kw)
                except Exception as _e:
                    logger.warning(f"tg_send_video_note failed: {_e}")
                return
            if mtype == "tg_send_venue":
                try:
                    await safe_send(message.bot.send_venue, chat_id=message.chat.id,
                        latitude=float(media.get("latitude",0)),
                        longitude=float(media.get("longitude",0)),
                        title=media.get("title","")[:64],
                        address=media.get("address","")[:256], **kw)
                except Exception as _e:
                    logger.warning(f"tg_send_venue failed: {_e}")
                return
            if mtype == "tg_promote":
                try:
                    await message.bot.promote_chat_member(
                        chat_id=message.chat.id, user_id=media.get("user_id"),
                        can_delete_messages=media.get("can_delete_messages", False),
                        can_pin_messages=media.get("can_pin_messages", False),
                        can_manage_chat=media.get("can_manage_chat", False),
                        can_ban_members=media.get("can_ban_members", False))
                    if media.get("custom_title"):
                        await message.bot.set_chat_administrator_custom_title(
                            chat_id=message.chat.id, user_id=media.get("user_id"),
                            custom_title=media.get("custom_title","")[:16])
                except Exception as _e:
                    logger.warning(f"tg_promote failed: {_e}")
                return
            if mtype == "tg_get_member":
                try:
                    m = await message.bot.get_chat_member(message.chat.id, media.get("user_id"))
                    u = m.user
                    info = (f"<b>Пользователь:</b> {u.full_name}\n"
                            f"<b>ID:</b> <code>{u.id}</code>\n"
                            f"<b>Username:</b> @{u.username or '—'}\n"
                            f"<b>Статус:</b> {m.status}")
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=info, parse_mode='HTML', **kw)
                except Exception as _e:
                    logger.warning(f"tg_get_member failed: {_e}")
                return
            if mtype == "tg_get_admins":
                try:
                    admins = await message.bot.get_chat_administrators(message.chat.id)
                    lines = [f"👑 <b>Администраторы чата</b> ({len(admins)}):"]
                    for a in admins:
                        title = getattr(a, 'custom_title', None) or a.status
                        lines.append(f"• {a.user.full_name} (@{a.user.username or '—'}) — {title}")
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text='\n'.join(lines), parse_mode='HTML', **kw)
                except Exception as _e:
                    logger.warning(f"tg_get_admins failed: {_e}")
                return
            if mtype == "tg_get_member_count":
                try:
                    count = await message.bot.get_chat_member_count(message.chat.id)
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=f"👥 Участников в чате: <b>{count}</b>", parse_mode='HTML', **kw)
                except Exception as _e:
                    logger.warning(f"tg_get_member_count failed: {_e}")
                return
            if mtype == "tg_create_forum_topic":
                try:
                    t = await message.bot.create_forum_topic(chat_id=message.chat.id,
                        name=media.get("name","Новый топик")[:128])
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=f"✅ Топик «{t.name}» создан (thread_id: {t.message_thread_id})", **kw)
                except Exception as _e:
                    logger.warning(f"tg_create_forum_topic failed: {_e}")
                return
            if mtype == "tg_close_forum_topic":
                try:
                    await message.bot.close_forum_topic(chat_id=message.chat.id,
                        message_thread_id=media.get("message_thread_id"))
                except Exception as _e:
                    logger.warning(f"tg_close_forum_topic failed: {_e}")
                return
            if mtype == "tg_get_sticker_set":
                try:
                    ss = await message.bot.get_sticker_set(name=media.get("name",""))
                    info = f"📦 <b>{ss.title}</b> (@{ss.name})\nСтикеров: {len(ss.stickers)}"
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=info, parse_mode='HTML', **kw)
                except Exception as _e:
                    logger.warning(f"tg_get_sticker_set failed: {_e}")
                return
            if mtype == "tg_approve_join":
                try:
                    if media.get("approve", True):
                        await message.bot.approve_chat_join_request(chat_id=message.chat.id,
                            user_id=media.get("user_id"))
                    else:
                        await message.bot.decline_chat_join_request(chat_id=message.chat.id,
                            user_id=media.get("user_id"))
                except Exception as _e:
                    logger.warning(f"tg_approve_join failed: {_e}")
                return
            if mtype == "tg_export_link":
                try:
                    link = await message.bot.export_chat_invite_link(chat_id=message.chat.id)
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=f"🔗 Ссылка: {link}", **kw)
                except Exception as _e:
                    logger.warning(f"tg_export_link failed: {_e}")
                return
            if mtype == "tg_set_chat_photo":
                try:
                    from aiogram.types import BufferedInputFile as _BIF
                    photo_buf = _BIF(media.get("data", b""),
                                     filename=media.get("filename", "photo.jpg"))
                    await message.bot.set_chat_photo(chat_id=message.chat.id, photo=photo_buf)
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                                    text="✅ Аватарка беседы обновлена!", **kw)
                except Exception as _e:
                    logger.warning(f"tg_set_chat_photo failed: {_e}")
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                                    text=f"❌ Не смог сменить аватарку: {_e}", **kw)
                return
            if mtype == "tg_read_logs":
                try:
                    import subprocess
                    log_path = '/root/Projects/NanoHatani/bot.log'
                    n = min(int(media.get("lines", 50)), 200)
                    filt = media.get("filter", "")
                    if filt:
                        out = subprocess.run(['grep', '-i', filt, log_path],
                            capture_output=True, text=True).stdout
                        lines = out.strip().splitlines()[-n:]
                    else:
                        with open(log_path) as f:
                            lines = f.readlines()
                        lines = [l.rstrip() for l in lines[-n:]]
                    text = '\n'.join(lines) or '(лог пустой)'
                    await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=f"<pre>{text[:3800]}</pre>", parse_mode='HTML', **kw)
                except Exception as _e:
                    logger.warning(f"read_logs failed: {_e}")
                return
            if mtype == "tg_send_sticker":
                try:
                    await safe_send(message.bot.send_sticker, chat_id=message.chat.id,
                        sticker=media.get("sticker"), **kw)
                except Exception as _e:
                    logger.warning(f"tg_send_sticker failed: {_e}")
                return
            if mtype == "tg_send_contact":
                try:
                    await safe_send(message.bot.send_contact, chat_id=message.chat.id,
                        phone_number=media.get("phone",""), first_name=media.get("name",""),
                        last_name=media.get("last_name",""), **kw)
                except Exception as _e:
                    logger.warning(f"tg_send_contact failed: {_e}")
                return
            if mtype == "tg_send_dice":
                try:
                    await safe_send(message.bot.send_dice, chat_id=message.chat.id,
                        emoji=media.get("emoji","🎲"), **kw)
                except Exception as _e:
                    logger.warning(f"tg_send_dice failed: {_e}")
                return
            if mtype == "tg_edit_message":
                try:
                    await message.bot.edit_message_text(chat_id=message.chat.id,
                        message_id=media.get("message_id"),
                        text=media.get("text","")[:4096], parse_mode='HTML')
                except Exception as _e:
                    logger.warning(f"tg_edit_message failed: {_e}")
                return
            # ── End Telegram API tools ───────────────────────────────
            data     = media.get("data", b"")
            caption  = (media.get("caption") or "")[:1024]
            filename = media.get("filename") or "file"
            buf      = BufferedInputFile(data, filename=filename)
            if mtype == "photo":
                await safe_send(message.bot.send_photo, chat_id=message.chat.id, photo=buf, caption=caption, **kw)
            elif mtype == "video":
                await safe_send(message.bot.send_video, chat_id=message.chat.id, video=buf, caption=caption, **kw)
            elif mtype == "audio":
                await safe_send(message.bot.send_audio, chat_id=message.chat.id, audio=buf, caption=caption, **kw)
            else:
                await safe_send(message.bot.send_document, chat_id=message.chat.id, document=buf, caption=caption, **kw)

        is_owner_user = message.from_user.id == OWNER_USER_ID
        try:
            (agent_text, agent_project) = await asyncio.wait_for(
                run_agent(prompt, message.chat.id, username, _status_cb, _agent_send_cb,
                          is_owner=is_owner_user),
                timeout=300,
            )
        except asyncio.TimeoutError:
            agent_text = 'Завис по таймауту. Попробуй покороче.'
            agent_project = None
        except Exception as _agent_err:
            logger.exception(f'Agent crashed: {_agent_err}')
            agent_text = 'Агент упал. Попробуй ещё раз.'
            agent_project = None

        try:
            await thinking_msg.delete()
        except Exception:
            pass

        if agent_project:
            sent_code_doc = await _send_generated_project(message, agent_project, reply_kwargs)
            if sent_code_doc:
                _remember_generated_code_message(message.chat.id, sent_code_doc.message_id, agent_project.get('files', []), web_query)
        elif agent_text:
            agent_text = await _handle_kick_directive(message, agent_text, reply_kwargs)
            code_blocks = re.findall('```(\\w*)\\n(.*?)```', agent_text, re.DOTALL)
            # Agent uses HTML formatting — preserve it, strip only triple-backtick blocks
            html_text = re.sub('```(\\w*)\\n(.*?)```', '', agent_text, flags=re.DOTALL).strip()
            if not html_text and code_blocks:
                html_text = 'Вот твой ебаный код, подавись нахуй.'
            elif not html_text:
                html_text = 'Нихуя не понял, но иди в пизду.'
            try:
                import bleach
                _TG_TAGS = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
                            'code', 'pre', 'blockquote', 'tg-spoiler', 'tg-emoji']
                safe_html = bleach.clean(
                    html_text,
                    tags=_TG_TAGS,
                    attributes={'pre': [], 'code': ['class'], 'tg-emoji': ['emoji-id'],
                                'blockquote': ['expandable']},
                    strip=True,
                )
                sent_msg = await safe_send(message.reply, safe_html, parse_mode='HTML', **reply_kwargs)
            except Exception:
                sent_msg = await safe_send(message.reply, _clean_plain_reply(html_text), **reply_kwargs)
            if not sent_msg:
                logging.warning('Agent reply not sent after flood-control retries')
                return
            if code_blocks:
                _code_files_sent: list[dict] = []
                for (lang, code) in code_blocks:
                    ext = _code_block_ext(lang)
                    filename = f'говняный_код_{uuid.uuid4().hex[:4]}.{ext}'
                    doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
                    sent_code_file = await safe_send(message.bot.send_document, chat_id=message.chat.id, document=doc, reply_to_message_id=sent_msg.message_id, **reply_kwargs)
                    file_entry = {'path': filename, 'content': code.strip()}
                    _code_files_sent.append(file_entry)
                    if sent_code_file:
                        _remember_generated_code_message(message.chat.id, sent_code_file.message_id, [file_entry], web_query)
                if sent_msg and _code_files_sent:
                    _remember_generated_code_message(message.chat.id, sent_msg.message_id, _code_files_sent, web_query)

        asyncio.create_task(add_user_stat(message.from_user.id, username, message.from_user.first_name or 'Аноним', 'text'))
        asyncio.create_task(log_prompt(message.from_user.id, username, message.from_user.first_name or 'Аноним', 'text', web_query))
