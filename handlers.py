import asyncio
import re
import uuid
import tempfile
import os
import time

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from state import pending_image_requests, pending_video_requests, pending_media_groups, user_image_cooldowns, user_text_cooldowns, user_video_cooldowns, full_access_image_cooldowns, paid_unlimited_until
from database import save_history, save_pending_gen, delete_pending_gen
from ai_services import start_veo_generation, poll_veo_operation
from utils import check_membership, is_banned
from ai_services import generate_image_with_gpt, generate_image_with_gemini, generate_image_with_nvidia, generate_image_with_openrouter, generate_video_with_veo, explain_generation_error, is_openai_verification_error, is_openai_timeout_error, generate_video_with_gemini, generate_text_with_gemini, upscale_image
from config import IMAGE_COOLDOWN_SECONDS, TEXT_COOLDOWN_SECONDS, DELETE_MESSAGE_DELAY_SECONDS, TEXT_ONLY_CHAT_ID, FULL_ACCESS_CHAT_ID, FULL_ACCESS_CHAT_IMAGE_COOLDOWN, PAYMENT_PHONE, ALLOWED_USER_IDS, OWNER_USER_ID
import logging

logger = logging.getLogger(__name__)

router = Router()

async def delete_message_after_delay(bot, chat_id: int, message_id: int, delay: int = DELETE_MESSAGE_DELAY_SECONDS):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение {message_id}: {e}")

async def run_progress_bar(bot, chat_id: int, message_id: int, model_label: str):
    import random
    BAR_LEN = 10
    start = asyncio.get_event_loop().time()
    pos = 0
    while True:
        elapsed = int(asyncio.get_event_loop().time() - start)
        if elapsed < 60:
            time_str = f"00:{elapsed:02d}"
        else:
            time_str = f"{elapsed // 60}:{elapsed % 60:02d}"
        filled = pos % BAR_LEN
        bar = "■" * filled + "□" * (BAR_LEN - filled)
        text = f"⏳ Генерация...\n[{bar}]\nПрошло: {time_str}\nМодель: {model_label}"
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1, 10))
        pos += 1

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type == "private":
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            await message.answer("Доступ запрещен. Вы не состоите в обязательной беседе.")
            return
        
        await message.answer(
            "Привет! Доступ разрешён 🤬\n\n"
            "Команды:\n"
            "/image ваш промпт — генерация картинки (Gemini / GPT / FLUX)\n"
            "/video ваш промпт — генерация видео через Veo\n"
            "/clear — очистить историю диалога\n\n"
            "Можно прикрепить фото к /image или /video.\n"
            "Тегни меня или ответь на моё сообщение — отвечу по-плохому 🤬"
        )

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.reply(
        "Что умеет этот бот:\n\n"
        "🎨 ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ\n"
        "/image ваш промпт\n"
        "Выбор провайдера и модели:\n"
        "• Gemini — Flash 3.1 Image / Flash 2.0 Image\n"
        "• GPT — GPT-Image-2 / DALL-E 3\n"
        "• FLUX — Schnell (быстро) / Dev (качество) / Klein 4B\n"
        "Можно прикрепить 1 или несколько фото (альбом) — бот учтёт их при генерации.\n"
        "GPT: если прямые ключи недоступны — автоматически переключается на OpenRouter.\n\n"
        "🎬 ГЕНЕРАЦИЯ ВИДЕО\n"
        "/video ваш промпт\n"
        "Модели Veo от Google:\n"
        "• Veo 2 — стабильный, без аудио\n"
        "• Veo 3.1 Fast — быстрее, с аудио\n"
        "• Veo 3.1 — лучшее качество, с аудио\n"
        "• Veo 3.1 Lite — дешевле\n"
        "Можно прикрепить фото — Veo оживит его.\n"
        "Промпт поддерживает русский язык.\n\n"
        "🧠 ТЕКСТОВЫЕ ОТВЕТЫ\n"
        "Тегни меня или ответь на моё сообщение — отвечу через Gemini Flash Lite с токсичным характером.\n"
        "Попроси написать код — напишу профессионально и пришлю файлом.\n\n"
        "🎞 АНАЛИЗ ВИДЕО\n"
        "Отправь видео/GIF — разберу покадрово и расскажу что происходит (с аудио).\n\n"
        "💾 ПАМЯТЬ\n"
        "Запоминаю последние 10 сообщений диалога на каждый чат.\n"
        "/clear — очистить историю.\n\n"
        "🛡 ЗАЩИТА ОТ СПАМА\n"
        "• Фото: 1 запрос раз в 15 сек\n"
        "• Видео: 1 запрос раз в 60 сек\n"
        "• Текст: 1 запрос раз в 5 сек\n\n"
        "🔄 ВОССТАНОВЛЕНИЕ ПОСЛЕ ПЕРЕЗАПУСКА\n"
        "Если бот перезапустился во время генерации — после старта автоматически продолжит и пришлёт результат.\n\n"
        "📎 ПОДСКАЗКИ\n"
        "• К /image и /video можно прикреплять фото\n"
        "• Для альбома (несколько фото) к /image: Gemini принимает все, GPT использует первое\n"
        "• FLUX не поддерживает фото-референсы"
    )

@router.message(Command("clear"))
async def cmd_clear(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
        
    await save_history(message.chat.id, [])
    await message.reply("Окей, я забыл всю хуйню, которую мы тут обсуждали. Начинаем с чистого листа.")

@router.message(Command("vip"))
async def cmd_vip(message: types.Message):
    if message.from_user.id not in ALLOWED_USER_IDS and message.from_user.id != OWNER_USER_ID:
        return

    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        parts = (message.text or "").split()
        if len(parts) > 1:
            try:
                target_id = int(parts[1])
            except ValueError:
                pass

    if not target_id:
        await message.reply("Ответь на сообщение юзера или укажи /vip <user_id>")
        return

    paid_unlimited_until[target_id] = time.time() + 86400
    await message.reply(f"✅ Юзер {target_id} получил безлимит на 24 часа.")


@router.message(Command("image"))
async def cmd_image(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply("Доступ запрещен. Вы не состоите в обязательной беседе.")
        return

    current_time = time.time()
    uid = message.from_user.id

    if message.chat.id == FULL_ACCESS_CHAT_ID and uid not in ALLOWED_USER_IDS:
        if current_time >= paid_unlimited_until.get(uid, 0):
            last_fa = full_access_image_cooldowns.get(uid, 0)
            remaining = FULL_ACCESS_CHAT_IMAGE_COOLDOWN - (current_time - last_fa)
            if remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                time_str = f"{mins} мин {secs} сек" if mins > 0 else f"{secs} сек"
                await message.reply(
                    f"⏳ Лимит: 1 фото каждые 10 минут. Подожди ещё {time_str}.\n\n"
                    f"💳 Переведи 10₽ на номер {PAYMENT_PHONE} и получи доступ без лимита на 24 часа."
                )
                return
        full_access_image_cooldowns[uid] = current_time
    else:
        last_time = user_image_cooldowns.get(uid, 0)
        if current_time - last_time < IMAGE_COOLDOWN_SECONDS:
            await message.reply(f"Не спамь блять картинками, подожди еще {int(IMAGE_COOLDOWN_SECONDS - (current_time - last_time))} сек.")
            return
        user_image_cooldowns[uid] = current_time

    prompt = message.text.replace("/image", "").strip() if message.text else ""
    if message.caption:
        prompt = message.caption.replace("/image", "").strip()

    if not prompt and not message.photo:
        await message.reply("Напишите промпт после команды, например:\n/image красивый закат")
        return

    images_bytes = []
    file_ids = []
    if message.photo:
        photo = message.photo[-1]
        file_ids.append(photo.file_id)
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        images_bytes.append(downloaded_file.read())

        if message.media_group_id:
            pending_media_groups[message.media_group_id] = {
                "images": images_bytes,
                "file_ids": file_ids,
                "request_id": None,
            }
            await asyncio.sleep(1.5)
            group = pending_media_groups.pop(message.media_group_id, None)
            if group:
                images_bytes = group["images"]
                file_ids = group.get("file_ids", file_ids)

    request_id = uuid.uuid4().hex[:10]
    pending_image_requests[request_id] = {
        "user_id": message.from_user.id,
        "chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "message_thread_id": message.message_thread_id if message.chat.is_forum else None,
        "prompt": prompt,
        "image_bytes": images_bytes[0] if len(images_bytes) == 1 else None,
        "images_bytes": images_bytes if len(images_bytes) > 1 else None,
        "file_ids": file_ids,
    }

    providers = ["gemini", "flux"] if message.chat.id == TEXT_ONLY_CHAT_ID else ["gemini", "gpt", "flux"]
    labels = {"gemini": "Gemini", "gpt": "GPT", "flux": "FLUX"}
    photo_label = f" 📎{len(images_bytes)} фото" if len(images_bytes) > 1 else ""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=labels[p] + (photo_label if p != "flux" else ""), callback_data=f"imgprov:{request_id}:{p}")
            for p in providers
        ]]
    )

    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs["message_thread_id"] = message.message_thread_id

    await message.reply(
        "Через какую модель хотите сгенерировать фото?",
        reply_markup=keyboard,
        **reply_kwargs
    )

PROVIDER_MODELS = {
    "gemini": [
        ("Flash 3.1 Image", "g31flash"),
        ("Flash 2.0 Image", "g20flash"),
    ],
    "gpt": [
        ("GPT-Image-2", "gpt2"),
        ("DALL-E 3", "dalle3"),
    ],
    "flux": [
        ("FLUX Schnell (быстро)", "schnell"),
        ("FLUX Dev (качество)", "fluxdev"),
        ("FLUX Klein 4B", "klein"),
    ],
}

MODEL_TO_REAL = {
    "g31flash":   ("gemini", "gemini-3.1-flash-image-preview"),
    "g20flash":   ("gemini", "gemini-2.0-flash-preview-image-generation"),
    "gpt2":       ("gpt",    "gpt-image-2"),
    "dalle3":     ("gpt",    "dall-e-3"),
    "schnell":    ("flux",   "black-forest-labs/flux.1-schnell"),
    "fluxdev":    ("flux",   "black-forest-labs/flux.1-dev"),
    "klein":      ("flux",   "black-forest-labs/flux_2-klein-4b"),
}


@router.message(F.photo & ~F.caption.startswith("/"))
async def handle_album_photo(message: types.Message):
    if not message.media_group_id:
        return
    group_id = message.media_group_id
    if group_id not in pending_media_groups:
        return
    photo = message.photo[-1]
    try:
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        pending_media_groups[group_id]["images"].append(downloaded.read())
    except Exception:
        pass

@router.callback_query(F.data.startswith("imgprov:"))
async def handle_provider_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    _, request_id, provider = parts
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
        await callback.answer("Только автор запроса может выбирать.", show_alert=True)
        return

    if provider == "gpt" and callback.message and callback.message.chat.id == TEXT_ONLY_CHAT_ID:
        await callback.answer("GPT недоступен в этой беседе.", show_alert=True)
        return

    models = PROVIDER_MODELS.get(provider, [])
    if not models:
        await callback.answer("Неизвестный провайдер.", show_alert=True)
        return

    rows = []
    for label, mid in models:
        rows.append([InlineKeyboardButton(text=label, callback_data=f"imgsel:{request_id}:{mid}")])

    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"imgback:{request_id}")])

    provider_names = {"gemini": "Gemini", "gpt": "GPT", "flux": "FLUX (NVIDIA)"}
    await callback.answer()
    try:
        await callback.message.edit_text(
            f"Выберите модель {provider_names.get(provider, provider)}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    except Exception:
        pass

@router.callback_query(F.data.startswith("imgback:"))
async def handle_provider_back(callback: types.CallbackQuery):
    if not callback.data:
        return
    parts = callback.data.split(":")
    if len(parts) != 2:
        return
    _, request_id = parts
    request_data = pending_image_requests.get(request_id)
    if not request_data:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != request_data["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return
    await callback.answer()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Gemini", callback_data=f"imgprov:{request_id}:gemini"),
            InlineKeyboardButton(text="GPT",    callback_data=f"imgprov:{request_id}:gpt"),
            InlineKeyboardButton(text="FLUX",   callback_data=f"imgprov:{request_id}:flux"),
        ]]
    )
    try:
        await callback.message.edit_text("Через какую модель хотите сгенерировать фото?", reply_markup=keyboard)
    except Exception:
        pass

@router.callback_query(F.data.startswith("imgsel:"))
async def handle_image_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, request_id, model_id = parts
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

    model_info = MODEL_TO_REAL.get(model_id)
    if not model_info:
        await callback.answer("Неизвестная модель.", show_alert=True)
        return

    provider, real_model = model_info
    pending_image_requests.pop(request_id, None)

    await callback.answer()

    message_thread_id = request_data["message_thread_id"]
    reply_kwargs = {}
    if message_thread_id:
        reply_kwargs["message_thread_id"] = message_thread_id

    await callback.bot.send_chat_action(
        chat_id=request_data["chat_id"],
        action="upload_photo",
        message_thread_id=message_thread_id
    )

    selected_label = next((l for lst in PROVIDER_MODELS.values() for l, m in lst if next((k for k, (p, rm) in MODEL_TO_REAL.items() if rm == m and p == provider), None) == model_id), real_model)

    progress_task = None
    if callback.message:
        try:
            await callback.message.edit_text("⏳ Запускаю генерацию...")
            progress_task = asyncio.create_task(
                run_progress_bar(callback.bot, request_data["chat_id"], callback.message.message_id, selected_label)
            )
        except Exception:
            pass

    imgs = request_data.get("images_bytes") or ([request_data["image_bytes"]] if request_data.get("image_bytes") else None)

    gen_id = f"img_{request_id}"
    await save_pending_gen(
        gen_id=gen_id, gen_type="image",
        user_id=request_data["user_id"], chat_id=request_data["chat_id"],
        source_message_id=request_data["source_message_id"],
        message_thread_id=request_data["message_thread_id"],
        prompt=request_data["prompt"], model=real_model, provider=provider,
        file_ids=request_data.get("file_ids", []), model_label=selected_label,
    )

    if provider == "gpt":
        result_img, error_msg = await generate_image_with_gpt(request_data["prompt"], images_bytes=imgs, model=real_model)
        if error_msg and is_openai_verification_error(error_msg):
            await callback.bot.send_message(
                chat_id=request_data["chat_id"],
                text="⚠️ GPT сейчас недоступен: организация OpenAI не верифицирована. Переключаюсь на Gemini.",
                reply_to_message_id=request_data["source_message_id"],
                **reply_kwargs
            )
            selected_label = "Gemini Flash 3.1"
            result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], images_bytes=imgs)
        elif error_msg and is_openai_timeout_error(error_msg):
            await callback.bot.send_message(
                chat_id=request_data["chat_id"],
                text="⚠️ GPT не ответил вовремя. Переключаюсь на Gemini.",
                reply_to_message_id=request_data["source_message_id"],
                **reply_kwargs
            )
            selected_label = "Gemini Flash 3.1"
            result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], images_bytes=imgs)
    elif provider == "flux":
        result_img, error_msg = await generate_image_with_nvidia(request_data["prompt"], model=real_model)
    else:
        result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], images_bytes=imgs, model=real_model)

    if progress_task:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    await delete_pending_gen(gen_id)

    if error_msg:
        error_sent_msg = await callback.bot.send_message(
            chat_id=request_data["chat_id"],
            text=f"❌ Ошибка:\n{error_msg}\n\n⏳ Ща спрошу у мозгов, че не так...",
            reply_to_message_id=request_data["source_message_id"],
            **reply_kwargs
        )

        first_image = (imgs[0] if imgs else None)
        explanation = await explain_generation_error(
            request_data["prompt"] or "", error_msg, image_bytes=first_image
        )

        if not explanation or "Ебать, гугл зацензурил" in explanation:
            explanation = "Пиздец, твой промпт или фото настолько больное, что Гугл забанил даже попытку объяснить — иди нахуй со своей хуйнёй!"

        try:
            await callback.bot.edit_message_text(
                chat_id=request_data["chat_id"],
                message_id=error_sent_msg.message_id,
                text=f"❌ Ошибка:\n{error_msg}\n\n🧠 Пояснение:\n{explanation}"
            )
        except Exception:
            pass
        return

    if result_img:
        photo_file = BufferedInputFile(result_img, filename="generated.jpg")
        model_label = selected_label
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

        upscale_msg = await callback.bot.send_message(
            chat_id=request_data["chat_id"],
            text="⬆️ Улучшаю качество через AI upscaler...",
            **reply_kwargs
        )
        upscaled, up_err = await upscale_image(result_img)
        try:
            await callback.bot.delete_message(
                chat_id=request_data["chat_id"],
                message_id=upscale_msg.message_id,
            )
        except Exception:
            pass
        if upscaled:
            await callback.bot.send_document(
                chat_id=request_data["chat_id"],
                document=BufferedInputFile(upscaled, filename="upscaled.png"),
                caption=f"✨ Улучшенная версия ({model_label}) 2x — без сжатия",
                reply_to_message_id=request_data["source_message_id"],
                **reply_kwargs
            )
        else:
            logger.warning(f"Upscale failed: {up_err}")

        return

    await callback.bot.send_message(
        chat_id=request_data["chat_id"],
        text="❌ Не удалось получить изображение.",
        reply_to_message_id=request_data["source_message_id"],
        **reply_kwargs
    )

VEO_MODELS = {
    "veo2":    ("Veo 2 (стабильный)",        "veo-2.0-generate-001"),
    "veo31f":  ("Veo 3.1 Fast (быстро)",     "veo-3.1-fast-generate-preview"),
    "veo31":   ("Veo 3.1 (лучшее + аудио)",  "veo-3.1-generate-preview"),
    "veo31l":  ("Veo 3.1 Lite (дешево)",     "veo-3.1-lite-generate-preview"),
}

VIDEO_COOLDOWN = 60

@router.message(Command("video"))
async def cmd_video(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply("Доступ запрещен.")
        return

    current_time = time.time()
    last_time = user_video_cooldowns.get(message.from_user.id, 0)
    if current_time - last_time < VIDEO_COOLDOWN:
        await message.reply(f"Не спамь блять видосами, подожди еще {int(VIDEO_COOLDOWN - (current_time - last_time))} сек.")
        return
    user_video_cooldowns[message.from_user.id] = current_time

    prompt = (message.text or "").replace("/video", "").strip()
    if message.caption:
        prompt = message.caption.replace("/video", "").strip()

    if not prompt and not message.photo:
        await message.reply(
            "Напиши промпт после команды, например:\n"
            "/video закат над морем\n\n"
            "Или прикрепи фото с подписью /video анимируй это — Veo оживит картинку."
        )
        return

    image_bytes = None
    if message.photo:
        photo = message.photo[-1]
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        image_bytes = downloaded.read()

    request_id = uuid.uuid4().hex[:10]
    pending_video_requests[request_id] = {
        "user_id": message.from_user.id,
        "chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "message_thread_id": message.message_thread_id if message.chat.is_forum else None,
        "prompt": prompt,
        "image_bytes": image_bytes,
    }

    rows = [[InlineKeyboardButton(text=label, callback_data=f"veosel:{request_id}:{mid}")]
            for mid, (label, _) in VEO_MODELS.items()]
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs["message_thread_id"] = message.message_thread_id

    await message.reply("Выберите модель Veo для генерации видео:", reply_markup=keyboard, **reply_kwargs)

@router.callback_query(F.data.startswith("veosel:"))
async def handle_veo_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    _, request_id, model_id = parts
    request_data = pending_video_requests.get(request_id)

    if not request_data:
        await callback.answer("Запрос устарел. Отправьте /video заново.", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return

    if callback.from_user.id != request_data["user_id"]:
        await callback.answer("Только автор запроса может выбирать.", show_alert=True)
        return

    model_info = VEO_MODELS.get(model_id)
    if not model_info:
        await callback.answer("Неизвестная модель.", show_alert=True)
        return

    model_label, real_model = model_info
    pending_video_requests.pop(request_id, None)
    await callback.answer()

    message_thread_id = request_data["message_thread_id"]
    reply_kwargs = {}
    if message_thread_id:
        reply_kwargs["message_thread_id"] = message_thread_id

    progress_task = None
    if callback.message:
        try:
            await callback.message.edit_text("⏳ Запускаю генерацию видео...")
            progress_task = asyncio.create_task(
                run_progress_bar(callback.bot, request_data["chat_id"], callback.message.message_id, model_label)
            )
        except Exception:
            pass

    await callback.bot.send_chat_action(chat_id=request_data["chat_id"], action="upload_video", message_thread_id=message_thread_id)

    op_name, api_key, start_err = await start_veo_generation(
        request_data["prompt"], model=real_model, image_bytes=request_data.get("image_bytes")
    )

    gen_id = f"veo_{request_id}"
    if op_name:
        await save_pending_gen(
            gen_id=gen_id, gen_type="video",
            user_id=request_data["user_id"], chat_id=request_data["chat_id"],
            source_message_id=request_data["source_message_id"],
            message_thread_id=request_data["message_thread_id"],
            prompt=request_data["prompt"], model=real_model, provider="veo",
            veo_operation_name=op_name, veo_api_key=api_key, model_label=model_label,
        )
        video_bytes, error_msg = await poll_veo_operation(op_name, api_key)
    else:
        video_bytes, error_msg = None, start_err

    if progress_task:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    await delete_pending_gen(gen_id)

    if error_msg:
        error_sent_msg = await callback.bot.send_message(
            chat_id=request_data["chat_id"],
            text=f"❌ Ошибка генерации видео:\n{error_msg}\n\n⏳ Ща спрошу у мозгов, че не так...",
            reply_to_message_id=request_data["source_message_id"],
            **reply_kwargs
        )
        image_for_explain = request_data.get("image_bytes")
        explanation = await explain_generation_error(
            request_data["prompt"], error_msg, image_bytes=image_for_explain
        )
        if explanation:
            try:
                await callback.bot.edit_message_text(
                    chat_id=request_data["chat_id"],
                    message_id=error_sent_msg.message_id,
                    text=f"❌ Ошибка генерации видео:\n{error_msg}\n\n🧠 Пояснение:\n{explanation}"
                )
            except Exception:
                pass
        return

    if video_bytes:
        video_file = BufferedInputFile(video_bytes, filename="generated.mp4")
        caption = f"🎬 Видео ({model_label}) по запросу: {request_data['prompt']}"
        await callback.bot.send_video(
            chat_id=request_data["chat_id"],
            video=video_file,
            caption=caption,
            reply_to_message_id=request_data["source_message_id"],
            **reply_kwargs
        )
        return

    await callback.bot.send_message(
        chat_id=request_data["chat_id"],
        text="❌ Не удалось получить видео.",
        reply_to_message_id=request_data["source_message_id"],
        **reply_kwargs
    )

# Хэндлер на видео сообщения
@router.message(F.video | F.animation | F.document)
async def handle_video(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
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
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return

    # Проверяем, упомянули ли бота или ответили ли на его сообщение
    bot_user = await message.bot.get_me()
    
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bot_user.username and f"@{bot_user.username}" in message.text

    # Также отвечаем, если это ЛС (private chat)
    is_private = message.chat.type == "private"

    if is_reply_to_bot or is_mentioned or is_private:
        # Анти-спам проверка
        current_time = time.time()
        last_time = user_text_cooldowns.get(message.from_user.id, 0)
        if current_time - last_time < TEXT_COOLDOWN_SECONDS:
            await message.reply(f"Заебал строчить, подожди еще {int(TEXT_COOLDOWN_SECONDS - (current_time - last_time))} сек.")
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
