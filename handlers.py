import asyncio
import re
import uuid
import tempfile
import os
import time

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from state import pending_image_requests, user_image_cooldowns, user_text_cooldowns
from database import save_history
from utils import check_membership
from ai_services import generate_image_with_gpt, generate_image_with_gemini, is_openai_verification_error, is_openai_timeout_error, generate_video_with_gemini, generate_text_with_gemini

router = Router()

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type == "private":
        is_member = await check_membership(message.bot, message.from_user.id)
        if not is_member:
            await message.answer("Доступ запрещен. Вы не состоите в обязательной беседе.")
            return
        
        await message.answer(
            "Привет! Я бот для генерации изображений через Nano Banana 2 (Gemini 3.1 Flash Image Preview).\n\n"
            "Доступ разрешен!\n"
            "Использование: `/image ваш промпт`\n"
            "После команды я дам выбор модели: Gemini или GPT (gpt-image-2).\n"
            "Также можно отправить фото с подписью `/image ваш промпт`.\n\n"
            "Если тегнешь меня или ответишь на моё сообщение текстом — я тебе по-плохому отвечу через Flash Lite 🤬\n"
            "А если попросишь код, я скину его файлом, чтоб ты подавился."
        )

@router.message(Command("clear"))
async def cmd_clear(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id)
    if not is_member:
        return
        
    await save_history(message.chat.id, [])
    await message.reply("Окей, я забыл всю хуйню, которую мы тут обсуждали. Начинаем с чистого листа.")

@router.message(Command("image"))
async def cmd_image(message: types.Message):
    # Проверка на подписку (работает и в группе, и в ЛС)
    is_member = await check_membership(message.bot, message.from_user.id)
    if not is_member:
        await message.reply("Доступ запрещен. Вы не состоите в обязательной беседе.")
        return

    # Анти-спам проверка (15 секунд)
    current_time = time.time()
    last_time = user_image_cooldowns.get(message.from_user.id, 0)
    if current_time - last_time < 15:
        await message.reply(f"Не спамь блять картинками, подожди еще {int(15 - (current_time - last_time))} сек.")
        return
    user_image_cooldowns[message.from_user.id] = current_time

    prompt = message.text.replace("/image", "").strip() if message.text else ""
    if message.caption:
        prompt = message.caption.replace("/image", "").strip()

    if not prompt and not message.photo:
        await message.reply("Пожалуйста, напишите промпт после команды, например: `/image красивый закат`")
        return

    image_bytes = None
    # Если есть фото
    if message.photo:
        photo = message.photo[-1] # Берем самое большое разрешение
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        image_bytes = downloaded_file.read()

    request_id = uuid.uuid4().hex[:10]
    pending_image_requests[request_id] = {
        "user_id": message.from_user.id,
        "chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "message_thread_id": message.message_thread_id if message.chat.is_forum else None,
        "prompt": prompt,
        "image_bytes": image_bytes,
    }

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Gemini", callback_data=f"imgsel:{request_id}:gemini"),
                InlineKeyboardButton(text="GPT", callback_data=f"imgsel:{request_id}:gpt"),
            ]
        ]
    )

    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs["message_thread_id"] = message.message_thread_id

    await message.reply(
        "Через какую модель хотите сгенерировать фото?",
        reply_markup=keyboard,
        **reply_kwargs
    )

@router.callback_query(F.data.startswith("imgsel:"))
async def handle_image_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, request_id, model_name = parts
    request_data = pending_image_requests.get(request_id)

    if not request_data:
        await callback.answer("Запрос устарел. Отправьте /image заново.", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return

    if callback.from_user.id != request_data["user_id"]:
        await callback.answer("Эту кнопку может нажать только тот, кто отправил /image.", show_alert=True)
        return

    pending_image_requests.pop(request_id, None)

    await callback.answer()

    if callback.message:
        try:
            await callback.message.edit_text("⏳ Ваше фото готовится...")
            
            async def delete_msg(chat_id, msg_id):
                await asyncio.sleep(5)
                try:
                    await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
                    
            asyncio.create_task(delete_msg(callback.message.chat.id, callback.message.message_id))
        except Exception:
            pass

    message_thread_id = request_data["message_thread_id"]

    reply_kwargs = {}
    if message_thread_id:
        reply_kwargs["message_thread_id"] = message_thread_id

    await callback.bot.send_chat_action(
        chat_id=request_data["chat_id"],
        action="upload_photo",
        message_thread_id=message_thread_id
    )

    selected_model = model_name
    if model_name == "gpt":
        result_img, error_msg = await generate_image_with_gpt(request_data["prompt"], request_data["image_bytes"])
        if error_msg and is_openai_verification_error(error_msg):
            await callback.bot.send_message(
                chat_id=request_data["chat_id"],
                text="⚠️ GPT (gpt-image-2) сейчас недоступен: организация OpenAI не верифицирована. Переключаюсь на Gemini.",
                reply_to_message_id=request_data["source_message_id"],
                **reply_kwargs
            )
            selected_model = "gemini"
            result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], request_data["image_bytes"])
        elif error_msg and is_openai_timeout_error(error_msg):
            await callback.bot.send_message(
                chat_id=request_data["chat_id"],
                text="⚠️ GPT (gpt-image-2) не ответил вовремя. Переключаюсь на Gemini.",
                reply_to_message_id=request_data["source_message_id"],
                **reply_kwargs
            )
            selected_model = "gemini"
            result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], request_data["image_bytes"])
    else:
        result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], request_data["image_bytes"])

    if error_msg:
        error_sent_msg = await callback.bot.send_message(
            chat_id=request_data["chat_id"],
            text=f"❌ Ошибка:\n{error_msg}\n\n⏳ Ща спрошу у мозгов, че не так...",
            reply_to_message_id=request_data["source_message_id"],
            **reply_kwargs
        )
        
        # Просим Gemini объяснить ошибку
        explanation_prompt = f"Пользователь пытался сгенерировать картинку.\nЕго промпт: {request_data['prompt'] or '<без текста, только фото>'}\nОтвет API с ошибкой: {error_msg}\nОбъясни ОЧЕНЬ коротко и агрессивно (максимум 2-3 предложения), почему произошла ошибка? Если это бан по фильтру безопасности или копирайту, объясни на что именно триггернуло."
        
        explanation = await generate_text_with_gemini(explanation_prompt, request_data["chat_id"])
        
        if "Ебать, гугл зацензурил эту хуйню" in explanation:
            explanation = "Пиздец, твой изначальный промпт настолько больной и запрещенный, что Гугл забанил (PROHIBITED_CONTENT) даже мою попытку проанализировать эту ошибку! Ты че там генерируешь, извращенец ебаный?"
            
        # Убираем блоки кода если есть
        cleaned_text = re.sub(r'```(\w*)\n(.*?)```', '', explanation, flags=re.DOTALL).strip()
        if not cleaned_text:
            cleaned_text = explanation
            
        await callback.bot.edit_message_text(
            chat_id=request_data["chat_id"],
            message_id=error_sent_msg.message_id,
            text=f"❌ Ошибка:\n{error_msg}\n\n🧠 Пояснение:\n{cleaned_text}"
        )
        return

    if result_img:
        photo_file = BufferedInputFile(result_img, filename="generated.jpg")
        model_label = "GPT" if selected_model == "gpt" else "Gemini"
        caption = (
            f"🎨 Ваш результат ({model_label}) по запросу: {request_data['prompt']}"
            if request_data["prompt"]
            else f"🎨 Ваш результат ({model_label}) готов."
        )
        await callback.bot.send_photo(
            chat_id=request_data["chat_id"],
            photo=photo_file,
            caption=caption,
            reply_to_message_id=request_data["source_message_id"],
            **reply_kwargs
        )
        return

    await callback.bot.send_message(
        chat_id=request_data["chat_id"],
        text="❌ Не удалось получить изображение.",
        reply_to_message_id=request_data["source_message_id"],
        **reply_kwargs
    )

# Хэндлер на видео сообщения
@router.message(F.video | F.animation | F.document)
async def handle_video(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id)
    if not is_member:
        return
        
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bot_user.username and message.caption and f"@{bot_user.username}" in message.caption
    is_private = message.chat.type == "private"
    
    # Только если обратились к боту
    if not (is_reply_to_bot or is_mentioned or is_private):
        return
        
    vid = message.video or message.animation
    if not vid and message.document:
        if not message.document.mime_type or not message.document.mime_type.startswith('video/'):
            return
        vid = message.document
        
    prompt = message.caption or ""
    if bot_user.username:
         prompt = prompt.replace(f"@{bot_user.username}", "").strip()
    if not prompt:
         prompt = "Внимательно посмотри это видео и скажи, что здесь происходит."
         
    wait_msg = await message.reply("⏳ Изучаю твое всратое видео кадр за кадром (24 FPS)...")
    
    file_info = await message.bot.get_file(vid.file_id)
    _, temp_vid_path = tempfile.mkstemp(suffix=".mp4")
    await message.bot.download_file(file_info.file_path, destination=temp_vid_path)
    
    # Анализируем видео
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing", message_thread_id=message.message_thread_id if message.chat.is_forum else None)
    text_response = await generate_video_with_gemini(prompt, temp_vid_path)
    
    if os.path.exists(temp_vid_path):
        os.remove(temp_vid_path)
    
    await wait_msg.delete()
    
    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs["message_thread_id"] = message.message_thread_id
        
    # Вырезаем блоки кода из ответа, как и в текстах (на случай если он решит написать скрипт)
    code_blocks = re.findall(r'```(\w*)\n(.*?)```', text_response, re.DOTALL)
    cleaned_text = re.sub(r'```(\w*)\n(.*?)```', '', text_response, flags=re.DOTALL).strip()
    
    if not cleaned_text and code_blocks:
        cleaned_text = "Вот твой ебаный код, подавись нахуй."
    elif not cleaned_text:
        cleaned_text = "Нихуя не понял, но иди в пизду."

    sent_msg = await message.reply(cleaned_text, **reply_kwargs)

    if code_blocks:
        for lang, code in code_blocks:
            ext = lang.strip().lower() or 'txt'
            if ext in ['python', 'py']: ext = 'py'
            elif ext in ['javascript', 'js']: ext = 'js'
            elif ext in ['typescript', 'ts']: ext = 'ts'
            elif ext in ['html', 'htm']: ext = 'html'
            elif ext in ['css']: ext = 'css'
            elif ext in ['c++', 'cpp']: ext = 'cpp'
            elif ext in ['c#', 'cs']: ext = 'cs'
            elif ext in ['php']: ext = 'php'
            elif ext in ['bash', 'sh']: ext = 'sh'
            elif ext in ['json']: ext = 'json'
            elif ext in ['xml']: ext = 'xml'
            
            filename = f"говняный_код_{uuid.uuid4().hex[:4]}.{ext}"
            doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
            await message.bot.send_document(
                chat_id=message.chat.id,
                document=doc,
                reply_to_message_id=sent_msg.message_id,
                **reply_kwargs
            )

# Хэндлер на текстовые сообщения (реплаи и теги)
@router.message(F.text)
async def handle_text_messages(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id)
    if not is_member:
        return

    # Проверяем, упомянули ли бота или ответили ли на его сообщение
    bot_user = await message.bot.get_me()
    
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bot_user.username and f"@{bot_user.username}" in message.text

    # Также отвечаем, если это ЛС (private chat)
    is_private = message.chat.type == "private"

    if is_reply_to_bot or is_mentioned or is_private:
        # Анти-спам проверка (5 секунд)
        current_time = time.time()
        last_time = user_text_cooldowns.get(message.from_user.id, 0)
        if current_time - last_time < 5:
            await message.reply(f"Заебал строчить, подожди еще {int(5 - (current_time - last_time))} сек.")
            return
        user_text_cooldowns[message.from_user.id] = current_time

        # Убираем упоминание бота из текста, если оно там есть
        prompt = message.text
        if bot_user.username:
            prompt = prompt.replace(f"@{bot_user.username}", "").strip()
        
        if not prompt:
            prompt = "Что тебе надо, хуйло?"

        reply_kwargs = {}
        if message.chat.is_forum and message.message_thread_id:
            reply_kwargs["message_thread_id"] = message.message_thread_id

        # Отправляем запрос к Gemini Lite
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing", message_thread_id=message.message_thread_id if message.chat.is_forum else None)
        text_response = await generate_text_with_gemini(prompt, message.chat.id)

        # Вырезаем блоки кода из ответа (```язык\nкод```)
        code_blocks = re.findall(r'```(\w*)\n(.*?)```', text_response, re.DOTALL)
        
        # Убираем код из текста, оставляя только слова
        cleaned_text = re.sub(r'```(\w*)\n(.*?)```', '', text_response, flags=re.DOTALL).strip()
        
        if not cleaned_text and code_blocks:
            cleaned_text = "Вот твой ебаный код, подавись нахуй."
        elif not cleaned_text:
            cleaned_text = "Нихуя не понял, но иди в пизду."

        # Сначала отправляем текст (гневный ответ)
        sent_msg = await message.reply(cleaned_text, **reply_kwargs)

        # Если был код, прикрепляем его как файлы в ответ на это же сообщение
        if code_blocks:
            for lang, code in code_blocks:
                ext = lang.strip().lower() or 'txt'
                # Маппинг частых форматов, чтобы было красиво
                if ext in ['python', 'py']: ext = 'py'
                elif ext in ['javascript', 'js']: ext = 'js'
                elif ext in ['typescript', 'ts']: ext = 'ts'
                elif ext in ['html', 'htm']: ext = 'html'
                elif ext in ['css']: ext = 'css'
                elif ext in ['c++', 'cpp']: ext = 'cpp'
                elif ext in ['c#', 'cs']: ext = 'cs'
                elif ext in ['php']: ext = 'php'
                elif ext in ['bash', 'sh']: ext = 'sh'
                elif ext in ['json']: ext = 'json'
                elif ext in ['xml']: ext = 'xml'
                
                filename = f"говняный_код_{uuid.uuid4().hex[:4]}.{ext}"
                
                # Создаем файл в памяти
                doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
                
                await message.bot.send_document(
                    chat_id=message.chat.id,
                    document=doc,
                    reply_to_message_id=sent_msg.message_id,
                    **reply_kwargs
                )
