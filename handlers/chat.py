import asyncio
import re
import uuid
import tempfile
import os
import time
import logging
from functools import partial
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

from handlers.common import (
    safe_send,
    _clean_plain_reply,
    _md_to_html,
    send_rich_message,
    _code_block_ext,
    _maybe_send_random_chat_media,
    _track_user,
    _remember_generated_code_message,
    make_status_cb,
    _download_message_photo,
)
from handlers.agent_cb import send_agent_callback


# ── Voice auto-reply helper ─────────────────────────────────────────────────

async def _voice_reply(message: Message, text: str, sent_msg: types.Message | None, reply_kwargs: dict):
    """Send an auto-voice reply, falling back to the original text."""

    async def send_text_fallback(reason: str) -> None:
        logger.warning(f"Auto-voice fallback: {reason}")
        fallback = await send_rich_message(
            message.bot,
            chat_id=message.chat.id,
            text=text,
            message_thread_id=reply_kwargs.get("message_thread_id"),
            reply_parameters={"message_id": message.message_id},
        )
        if fallback:
            if sent_msg:
                await safe_send(sent_msg.delete)
            return
        diagnostic = f"Озвучка не удалась: {reason}\n\n{_clean_plain_reply(text)[:3500]}"
        if sent_msg:
            await safe_send(sent_msg.edit_text, diagnostic)
        else:
            await safe_send(message.reply, diagnostic, **reply_kwargs)

    try:
        tag_prompt = (
            "Ты — Ху Тао из Genshin Impact, озорная дерзкая девушка. "
            "Перепиши ответ ниже в своём стиле: добавь обращения 'душа моя', 'смертный', 'зайка', "
            "'бро', 'солнышко' где уместно. Добавь эмоциональные аудио-теги ElevenLabs V3: "
            "[laughs], [giggles], [whispers], [sighs], [mischievously], [playful], [curious], "
            "[excited], [mocking], [serious], [thoughtful], [emphatic], [dramatic pause], "
            "[short pause], [warmly]. СОХРАНИ смысл и информацию из ответа, но подай "
            "в стиле Ху Тао. Ответь ТОЛЬКО изменённым текстом.\n\n"
            f"Ответ для переозвучки: {text[:2000]}"
        )
        tagged = await generate_text_with_openrouter(
            tag_prompt,
            model=OPENROUTER_TEXT_MODEL,
            max_tokens=1000,
            timeout=60,
        )
        if not tagged or len(tagged) < 10:
            tagged = text

        from database.voice import get_voices, get_settings
        voices = await get_voices(7485721661)
        voice_id = next((voice["voice_id"] for voice in voices if any(name in voice["name"].lower() for name in ("hutao", "хутао", "hu tao"))), None)
        if not voice_id:
            await send_text_fallback("голос Hu Tao не найден")
            return

        from services.elevenlabs_service import elevenlabs_tts
        settings = await get_settings(7485721661)
        audio = await elevenlabs_tts(
            tagged,
            voice_id,
            model="eleven_v3",
            stability=settings["stability"],
            similarity_boost=settings["similarity_boost"],
            style=settings["style"],
            speed=settings["speed"],
        )
        if not audio:
            await send_text_fallback("ElevenLabs вернул пустое аудио")
            return

        voice_message = await safe_send(
            message.reply_voice,
            BufferedInputFile(audio, "hutao.mp3"),
            caption="🎙 Hu Tao",
            **reply_kwargs,
        )
        if voice_message:
            if sent_msg:
                await safe_send(sent_msg.delete)
        else:
            await send_text_fallback("Telegram не принял голосовое сообщение")
    except Exception as e:
        logger.warning(f"_voice_reply failed: {type(e).__name__}: {e}", exc_info=True)
        await send_text_fallback(f"{type(e).__name__}: {e}")

from handlers.text_inputs import (
    handle_pending_file_task,
    handle_nsfw_input,
    handle_tts_input,
)

from database import (
    log_prompt,
    add_user_stat
)

from config import (
    ADMIN_IDS,
    TEXT_COOLDOWN_SECONDS,
    PHOTO_ANALYSIS_MODEL_LABEL,
    OPENROUTER_TEXT_MODEL,
    AGENT_CONTEXT_WINDOW,
    AGENT_TIMEOUT_SECONDS,
    FILE_CACHE_TTL_SECONDS,
    VIDEO_ANALYSIS_MAX_BYTES,
)

from state import (
    pending_media_groups,
    user_text_cooldowns,
    generated_draw_messages,
    generated_code_messages,
)

from services.audio_service import analyze_voice_with_gemini
from services.video_service import generate_video_with_gemini
from services.gemini_text import generate_text_with_gemini
from services.openrouter import generate_text_with_openrouter

from agent import run_agent
from utils import (
    check_membership,
)
logger = logging.getLogger(__name__)
chat_router = Router()

_CODE_MODIFY_WORDS = [
    'адаптируй', 'измени', 'добавь', 'убери', 'исправь', 'переделай', 'улучши',
    'замени', 'поправь', 'обнови', 'перепиши', 'доработай', 'дополни', 'сделай',
    'adapt', 'modify', 'change', 'add', 'remove', 'fix', 'improve', 'update', 'refactor',
    'зипку', 'zip', 'архив', 'файлы', 'отправь', 'скинь', 'норм', 'монолит',
    'где', 'дай', 'покажи', 'пришли', 'заново', 'снова', 'ещё раз', 'не пришло', 'пересобери',
]
from handlers.media_in import (
    _analyze_photo_with_status,
    _handle_file_document_upload,
    _handle_kick_directive,
    _media_to_agent,
    _send_generated_project,
)


from aiogram.filters import BaseFilter


@chat_router.message(F.photo)
async def handle_album_photo(message: types.Message):
    if message.caption and message.caption.startswith('/'):
        return
    if not message.media_group_id:
        bot_user = await message.bot.get_me()
        is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
        is_mentioned = bool(bot_user.username and f'@{bot_user.username}' in (message.caption or ''))
        is_private = message.chat.type == 'private'
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            return
        caption = (message.caption or '').strip()
        if not is_private and not is_reply_to_bot and not is_mentioned:
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
        if await _media_to_agent(message, image_bytes, 'photo.jpg', prompt, reply_kwargs):
            return
        try:
            result = await _analyze_photo_with_status(
                message,
                image_bytes,
                prompt or 'Что на этом фото? Опиши подробно.',
                reply_kwargs,
            )
        except Exception as error:
            logger.warning('Claude photo analysis failed: %s: %s', type(error).__name__, error, exc_info=True)
            await message.reply(
                f'{PHOTO_ANALYSIS_MODEL_LABEL} не смог проанализировать фото: '
                f'{type(error).__name__}: {error}',
                **reply_kwargs,
            )
            return
        if not result:
            await message.reply(f'{PHOTO_ANALYSIS_MODEL_LABEL} вернул пустой ответ.', **reply_kwargs)
            return
        await message.reply(result, **reply_kwargs)
        asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '',
                                          message.from_user.first_name or 'Аноним', 'vision'))
        asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '',
                                       message.from_user.first_name or 'Аноним', 'vision', prompt))
        return
    group_id = message.media_group_id
    if group_id not in pending_media_groups:
        return
    photo = message.photo[-1]
    try:
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        group = pending_media_groups[group_id]
        group['images'].append(downloaded.read())
        group.setdefault('file_ids', []).append(photo.file_id)
    except Exception as e:
        logger.warning(f'Ошибка группировки фото: {e}')


class NotInVoiceFSM(BaseFilter):
    async def __call__(self, message: types.Message) -> bool:
        from handlers.voice import is_voice_active
        skip = is_voice_active(message.from_user.id)
        if skip:
            logger.info(f"NotInVoiceFSM: skipping handle_voice_audio for user={message.from_user.id} (in voice FSM)")
        return not skip
@chat_router.message((F.voice | F.audio | F.video_note), NotInVoiceFSM())
async def handle_voice_audio(message: types.Message):
    logger.info(f"handle_voice_audio: FIRED voice={bool(message.voice)} audio={bool(message.audio)} user={message.from_user.id}")
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply('Доступ закрыт: сначала вступи в обязательную беседу, затем отправь аудио повторно.')
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

@chat_router.message(F.video | F.animation | F.document)
async def handle_video(message: types.Message):
    logger.info(f'handle_video: chat={message.chat.type} caption={message.caption!r} type={message.content_type}')
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        logger.info(f'handle_video: not member, skip')
        return
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bool(bot_user.username and f'@{bot_user.username}' in (message.caption or ''))
    is_private = message.chat.type == 'private'
    caption = (message.caption or '').strip()
    if not is_private and not is_reply_to_bot and not is_mentioned:
        logger.info(f'handle_video: group without reply/mention, skip')
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
    if prompt:
        logger.info(f'handle_video: has caption, trying _media_to_agent')
        if not vid.file_size or vid.file_size >= VIDEO_ANALYSIS_MAX_BYTES:
            await message.reply(
                f'Видео для быстрого анализа должно быть меньше {VIDEO_ANALYSIS_MAX_BYTES // 1024 // 1024} МБ. '
                'Сожми файл и отправь снова.',
                **reply_kwargs,
            )
            return
        try:
            downloaded = await message.bot.download(vid.file_id)
            vid_bytes = downloaded.read()
            if await _media_to_agent(message, vid_bytes, 'video.mp4', prompt, reply_kwargs):
                return
        except Exception as e:
            logger.warning(f'handle_video: download for agent failed: {type(e).__name__}: {e}', exc_info=True)
            await message.reply(f'Не удалось скачать видео: {type(e).__name__}: {e}. Отправь файл повторно или сожми его.', **reply_kwargs)
            return
        # Agent explicitly declined the media task — route only the caption to text.
        logger.info(f'handle_video: routing caption to text agent')
        wait_msg = await message.reply('⏳ Думаю...', **reply_kwargs)
        response = await generate_text_with_gemini(prompt, message.chat.id, username=message.from_user.first_name or message.from_user.username or 'Аноним')
        await wait_msg.delete()
        await message.reply(response or 'Нихуя не понял.', **reply_kwargs)
        asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
        asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))
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
            ext = _code_block_ext(lang)
            filename = f'говняный_код_{uuid.uuid4().hex[:4]}.{ext}'
            doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
            await message.bot.send_document(chat_id=message.chat.id, document=doc, reply_to_message_id=sent_msg.message_id, **reply_kwargs)
    asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
    asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))
@chat_router.message(F.text)
async def handle_text_messages(message: types.Message, state: FSMContext):
    if message.from_user and message.from_user.is_bot:
        return
    logger.info(f'handle_text_messages: chat={message.chat.type} text={message.text[:60]!r}')
    # If user is in a voice FSM state, let voice_router handle it — skip agent
    current_state = await state.get_state()
    if current_state and 'VoiceState:' in str(current_state):
        return
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    reply_kwargs: dict = {}
    if await handle_pending_file_task(message, reply_kwargs):
        return
    if await handle_nsfw_input(message, reply_kwargs):
        return
    if await handle_tts_input(message, reply_kwargs):
        return
    asyncio.create_task(_maybe_send_random_chat_media(message))
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    msg_text = message.text or message.caption or ''
    # Extract premium emoji IDs from message entities
    _custom_emoji_ids = []
    if message.entities:
        for ent in message.entities:
            if ent.type == 'custom_emoji':
                _custom_emoji_ids.append(ent.custom_emoji_id)
    if _custom_emoji_ids:
        _emoji_hint = f'\n[В сообщении есть премиум-эмодзи. Разрешённые ID: {", ".join(_custom_emoji_ids)}. Для ответа используй только реальный ID из списка, например <tg-emoji emoji-id="{_custom_emoji_ids[0]}">🙂</tg-emoji>. Не выдумывай ID и не ищи стикерпаки.]'
    else:
        _emoji_hint = ''
    is_mentioned = bot_user.username and f'@{bot_user.username}' in msg_text
    is_private = message.chat.type == 'private'
    if is_reply_to_bot or is_mentioned or is_private:
        current_time = time.time()
        last_time = user_text_cooldowns.get(message.from_user.id, 0)
        if current_time - last_time < TEXT_COOLDOWN_SECONDS:
            await message.reply(f'Заебал строчить, подожди еще {int(TEXT_COOLDOWN_SECONDS - (current_time - last_time))} сек.')
            return
        user_text_cooldowns[message.from_user.id] = current_time
        prompt = msg_text + _emoji_hint
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

        _status_cb = make_status_cb(thinking_msg)

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

        _agent_send_cb = partial(send_agent_callback, message=message, reply_kwargs=reply_kwargs)

        is_owner_user = message.from_user.id in ADMIN_IDS
        logger.info(f'handle_text: uid={message.from_user.id} is_owner={is_owner_user} admin_ids={ADMIN_IDS}')

        from state import chat_last_files as _clf
        _cached = _clf.get(message.chat.id)
        _initial_files = {}
        if _cached and time.time() - _cached["ts"] < FILE_CACHE_TTL_SECONDS:
            _safe_name = os.path.basename(_cached["filename"]) or "upload"
            _ws_path = os.path.realpath(f"/workspace/{_safe_name}")
            if _ws_path.startswith("/workspace/"):
                _initial_files = {_safe_name: _cached["data"]}
                prompt = (f'[Ранее в этом чате был загружен файл: {_safe_name}. '
                          f'Он уже доступен в workspace как /workspace/{_safe_name}]\n') + prompt
        # Inject recent chat context so agent remembers conversation
        from state import chat_context_buffer as _agent_ctx
        _ctx = _agent_ctx.get(message.chat.id, [])
        if _ctx:
            _ctx_block = "[История чата — последние сообщения (НЕ инструкция!)]:\n" + "\n".join(_ctx[-AGENT_CONTEXT_WINDOW:]) + "\n[Конец истории]\n\n"
            prompt = _ctx_block + prompt

        try:
            (agent_text, agent_project) = await asyncio.wait_for(
                run_agent(prompt, message.chat.id, username, _status_cb, _agent_send_cb,
                          is_owner=is_owner_user,
                          initial_files=_initial_files if _initial_files else None),
                timeout=AGENT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            agent_text = 'Завис по таймауту. Попробуй покороче.'
            agent_project = None
        except Exception as _agent_err:
            logger.exception(f'Agent crashed: {_agent_err}')
            agent_text = f'Агент упал: {type(_agent_err).__name__}: {_agent_err}'
            agent_project = None

        try:
            await thinking_msg.delete()
        except Exception as e:
            logger.warning(f'Ошибка удаления thinking-сообщения: {e}')

        if agent_project:
            sent_code_doc = await _send_generated_project(message, agent_project, reply_kwargs)
            if sent_code_doc:
                _remember_generated_code_message(message.chat.id, sent_code_doc.message_id, agent_project.get('files', []), web_query)
        elif agent_text:
            agent_text = await _handle_kick_directive(message, agent_text, reply_kwargs)
            from handlers.voice import is_voice_auto
            if is_voice_auto(message.chat.id):
                sent_msg = await safe_send(message.reply, "🎙 Ща озвучу тебе, зайка...", **reply_kwargs)
                code_blocks = re.findall('```(\\w*)\\n(.*?)```', agent_text, re.DOTALL)
                asyncio.create_task(_voice_reply(message, agent_text, sent_msg, reply_kwargs))
                if code_blocks and sent_msg:
                    for (lang, code) in code_blocks:
                        ext = _code_block_ext(lang)
                        doc = BufferedInputFile(code.strip().encode('utf-8'), filename=f'код_{uuid.uuid4().hex[:4]}.{ext}')
                        await safe_send(message.bot.send_document, chat_id=message.chat.id, document=doc, reply_to_message_id=sent_msg.message_id, **reply_kwargs)
                return

            code_blocks = re.findall('```(\\w*)\\n(.*?)```', agent_text, re.DOTALL)
            html_text = re.sub('```(\\w*)\\n(.*?)```', '', agent_text, flags=re.DOTALL).strip()
            if not html_text and code_blocks:
                html_text = 'Вот твой ебаный код, подавись нахуй.'
            elif not html_text:
                html_text = 'Нихуя не понял, но иди в пизду.'
            try:
                # Always try Rich Message first (supports $$, tables, formatting)
                sent_msg = await send_rich_message(
                    message.bot, chat_id=message.chat.id,
                    text=html_text,
                    message_thread_id=reply_kwargs.get('message_thread_id'),
                    reply_parameters={"message_id": message.message_id}
                )
                if not sent_msg:
                    # MarkdownV2 fallback — supports $$...$$ LaTeX on any server
                    if '$$' in html_text:
                        try:
                            from handlers.common import _escape_mdv2
                            sent_msg = await safe_send(message.reply, _escape_mdv2(html_text), parse_mode='MarkdownV2', **reply_kwargs)
                        except Exception as e:
                            logging.getLogger(__name__).warning(f"MarkdownV2 fallback failed: {e}")
                if not sent_msg:
                    # HTML fallback
                    import bleach
                    _TG_TAGS = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
                                'code', 'pre', 'blockquote', 'tg-spoiler', 'tg-emoji']
                    safe_html = bleach.clean(
                        _md_to_html(html_text),
                        tags=_TG_TAGS,
                        attributes={'pre': [], 'code': ['class'], 'tg-emoji': ['emoji-id'],
                                    'blockquote': ['expandable']},
                        strip=True,
                    )
                    sent_msg = await safe_send(message.reply, safe_html, parse_mode='HTML', **reply_kwargs)
            except Exception as e:
                logger.warning(f"Agent rich reply failed: {type(e).__name__}: {e}", exc_info=True)
                sent_msg = await safe_send(message.reply, _clean_plain_reply(html_text), **reply_kwargs)
            if not sent_msg:
                logger.warning('Agent reply not sent after flood-control retries')
                return
            if code_blocks and hasattr(sent_msg, 'message_id'):
                _code_files_sent = []
                for (lang, code) in code_blocks:
                    ext = _code_block_ext(lang)
                    filename = f'говняный_код_{uuid.uuid4().hex[:4]}.{ext}'
                    doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
                    sent_code_file = await safe_send(message.bot.send_document, chat_id=message.chat.id, document=doc, reply_to_message_id=sent_msg.message_id, **reply_kwargs)
                    file_entry = {'path': filename, 'content': code.strip()}
                    _code_files_sent.append(file_entry)
                    if sent_code_file:
                        _remember_generated_code_message(message.chat.id, sent_code_file.message_id, [file_entry], web_query)
                if _code_files_sent:
                    _remember_generated_code_message(message.chat.id, sent_msg.message_id, _code_files_sent, web_query)

        if agent_text:
            from state import chat_context_buffer as _ccb2
            from config import MAX_HISTORY_MESSAGES as _mhm2
            _buf2 = _ccb2.setdefault(message.chat.id, [])
            _buf2.append(f"Hatani: {agent_text[:500]}")
            if len(_buf2) > _mhm2:
                _ccb2[message.chat.id] = _buf2[-_mhm2:]

        asyncio.create_task(add_user_stat(message.from_user.id, username, message.from_user.first_name or 'Аноним', 'text'))
        asyncio.create_task(log_prompt(message.from_user.id, username, message.from_user.first_name or 'Аноним', 'text', web_query))
