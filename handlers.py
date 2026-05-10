import asyncio
import re
import uuid
import tempfile
import os
import time

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from state import pending_image_requests, pending_video_requests, pending_media_groups, user_image_cooldowns, user_text_cooldowns, user_video_cooldowns, full_access_image_cooldowns, paid_unlimited_until, pending_prompt_requests, pending_nsfw_configs, chat_members_cache
from database import save_history, save_pending_gen, delete_pending_gen
from ai_services import start_veo_generation, poll_veo_operation
from utils import check_membership, is_banned
from ai_services import generate_image_with_gpt, generate_image_with_gemini, generate_image_with_nvidia, generate_image_with_openrouter, generate_video_with_veo, explain_generation_error, is_openai_verification_error, is_openai_timeout_error, generate_video_with_gemini, generate_text_with_gemini, upscale_image, generate_image_prompt, generate_code_with_gemini, fetch_gemini_image_models, fetch_openai_image_models, fetch_veo_models, generate_image_with_replicate
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
        "Можно прикрепить до 10 фото альбомом — все будут использованы как референсы.\n"
        "• Gemini: принимает все фото (до 3000 технически)\n"
        "• GPT: принимает до 16 референсных фото\n"
        "• FLUX: референсные фото не поддерживает\n"
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

import random as _random

_ALL_PHRASES = [
    "Эй вы, уроды, все сюда нахуй! 👇",
    "Хуиданте сюда все, живо! 🔔",
    "Ау, дебилы, слышите? Все сюда! 📢",
    "Все ко мне, быстро, я сказал! 🗣️",
    "Ну-ка все собрались, чего расползлись! 👊",
    "Стоять всем! Сюда смотреть! 👁️",
    "Эй ты, и ты, и ты тоже — все на месте! ⚡",
]

_all_cooldowns: dict = {}

@router.message(Command("all"))
async def cmd_all(message: types.Message):
    if message.chat.type == "private":
        await message.reply("В личке некого созывать, дурик.")
        return

    uid = message.from_user.id
    now = time.time()
    if now - _all_cooldowns.get((message.chat.id, uid), 0) < 300:
        remaining = int(300 - (now - _all_cooldowns.get((message.chat.id, uid), 0)))
        await message.reply(f"Не спамь созывом, подожди ещё {remaining} сек.")
        return
    _all_cooldowns[(message.chat.id, uid)] = now

    try:
        admins = await message.bot.get_chat_administrators(message.chat.id)
        if message.chat.id not in chat_members_cache:
            chat_members_cache[message.chat.id] = {}
        for a in admins:
            u = a.user
            if not u.is_bot:
                chat_members_cache[message.chat.id][u.id] = (u.first_name or "Аноним", u.username)
    except Exception:
        pass

    members = chat_members_cache.get(message.chat.id, {})
    if not members:
        await message.reply("Никого не знаю ещё.")
        return

    bot_user = await message.bot.get_me()
    mentions = []
    for user_id, (first_name, username) in members.items():
        if user_id == bot_user.id or user_id == uid:
            continue
        if username:
            mentions.append(f"@{username}")
        else:
            mentions.append(f'<a href="tg://user?id={user_id}">{first_name}</a>')

    if not mentions:
        await message.reply("Не на кого тегать, все и так тут.")
        return

    phrase = _random.choice(_ALL_PHRASES)
    chunks = []
    chunk = phrase + "\n"
    for m in mentions:
        if len(chunk) + len(m) + 1 > 4000:
            chunks.append(chunk)
            chunk = ""
        chunk += m + " "
    if chunk.strip():
        chunks.append(chunk)

    for ch in chunks:
        await message.answer(ch, parse_mode="HTML")


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
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply("Доступ запрещен. Вы не состоите в обязательной беседе.")
        return

    current_time = time.time()
    uid = message.from_user.id

    if message.chat.id == FULL_ACCESS_CHAT_ID and uid not in ALLOWED_USER_IDS:
        is_main_member = False
        try:
            m = await message.bot.get_chat_member(chat_id=CHAT_ID, user_id=uid)
            is_main_member = m.status in ("member", "administrator", "creator")
        except Exception:
            pass

        if not is_main_member and current_time >= paid_unlimited_until.get(uid, 0):
            last_fa = full_access_image_cooldowns.get(uid, 0)
            remaining = FULL_ACCESS_CHAT_IMAGE_COOLDOWN - (current_time - last_fa)
            if remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                time_str = f"{mins} мин {secs} сек" if mins > 0 else f"{secs} сек"
                await message.reply(
                    f"Не спамь блять картинками, подожди ещё {time_str}."
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
        if message.media_group_id:
            pending_media_groups[message.media_group_id] = {
                "images": images_bytes,
                "file_ids": file_ids,
                "request_id": None,
            }

        photo = message.photo[-1]
        file_ids.append(photo.file_id)
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        images_bytes.append(downloaded_file.read())

        if message.media_group_id:
            await asyncio.sleep(2.5)
            group = pending_media_groups.pop(message.media_group_id, None)
            if group:
                images_bytes = group["images"]
                file_ids = group.get("file_ids", file_ids)

    request_id = uuid.uuid4().hex[:10]
    thread_id = message.message_thread_id if message.chat.is_forum else None

    pending_image_requests[request_id] = {
        "user_id": message.from_user.id,
        "chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "message_thread_id": thread_id,
        "prompt": prompt,
        "image_bytes": images_bytes[0] if len(images_bytes) == 1 else None,
        "images_bytes": images_bytes if len(images_bytes) > 1 else None,
        "file_ids": file_ids,
    }

    reply_kwargs = {}
    if message.chat.is_forum and thread_id:
        reply_kwargs["message_thread_id"] = thread_id

    if images_bytes and prompt:
        pending_prompt_requests[request_id] = {
            "user_id": message.from_user.id,
            "chat_id": message.chat.id,
            "source_message_id": message.message_id,
            "message_thread_id": thread_id,
            "prompt": prompt,
            "images_bytes": images_bytes,
            "file_ids": file_ids,
            "prev_prompts": [],
            "current_ai_prompt": None,
        }
        photo_word = "фотки" if len(images_bytes) > 1 else "фотку"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Использовать шаблон промта", callback_data=f"pask:{request_id}")],
            [InlineKeyboardButton(text="⚡ Генерировать с моим промтом", callback_data=f"pbase:{request_id}")],
        ])
        await message.reply(
            f"Слышь, {photo_word} вижу. Твой промт — ну такое. "
            f"Могу через Gemini Pro сделать нормальный промт по {'этим' if len(images_bytes) > 1 else 'этому'} {'фоткам' if len(images_bytes) > 1 else 'фото'} "
            f"и твоей идее. Жмякай кнопку или генерируй со своим мусором.",
            reply_markup=keyboard,
            **reply_kwargs
        )
        return

    await message.reply(
        "Через какую модель хотите сгенерировать фото?",
        reply_markup=_providers_keyboard(request_id, message.chat.id, len(images_bytes)),
        **reply_kwargs
    )

_TEMP_OPTIONS = [
    (0.1, "🎯 Точный",     "строго следует промпту, почти без вариаций"),
    (0.5, "⚖️ Умеренный",  "баланс точности и разнообразия"),
    (1.0, "✨ Стандарт",   "стандартная генерация (по умолчанию)"),
    (1.5, "🎨 Творческий", "больше вариативности и интерпретации"),
    (2.0, "🌀 Безумный",   "максимальная непредсказуемость"),
]

def _temp_message() -> str:
    lines = [
        "🌡️ Выберите температуру генерации:\n",
        "Температура влияет на то, насколько точно ИИ следует промпту.",
        "Диапазон: 0.1 (точно) — 2.0 (творческий хаос)\n",
    ]
    for val, label, desc in _TEMP_OPTIONS:
        lines.append(f"{label} — {desc}")
    return "\n".join(lines)

def _temp_keyboard(request_id: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{label} ({val})",
        callback_data=f"ptmp:{request_id}:{i}"
    )] for i, (val, label, _) in enumerate(_TEMP_OPTIONS)]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _providers_keyboard(request_id: str, chat_id: int, photo_count: int = 0) -> InlineKeyboardMarkup:
    providers = ["gemini", "flux", "nsfw"] if chat_id == TEXT_ONLY_CHAT_ID else ["gemini", "gpt", "flux", "nsfw"]
    labels = {"gemini": "Gemini", "gpt": "GPT", "flux": "FLUX", "nsfw": "NSFW 🔞"}
    photo_label = f" 📎{photo_count} фото" if photo_count > 1 else ""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=labels[p] + (photo_label if p not in ("flux", "nsfw") else ""),
            callback_data=f"imgprov:{request_id}:{p}"
        ) for p in providers
    ]])


@router.callback_query(F.data.startswith("ptmp:"))
async def handle_temp_select(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    _, request_id, idx_str = parts
    req = pending_image_requests.get(request_id)
    if not req:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != req["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return

    idx = int(idx_str)
    temp_val, temp_label, _ = _TEMP_OPTIONS[idx]
    req["temperature"] = temp_val
    await callback.answer(f"Температура: {temp_label} ({temp_val})")

    if req.get("selected_model"):
        pending_image_requests.pop(request_id, None)
        real_model = req["selected_model"]
        selected_label = req.get("selected_label", real_model)
        message_thread_id = req.get("message_thread_id")
        reply_kwargs = {"message_thread_id": message_thread_id} if message_thread_id else {}
        await callback.bot.send_chat_action(chat_id=req["chat_id"], action="upload_photo", message_thread_id=message_thread_id)
        progress_task = None
        try:
            await callback.message.edit_text(f"🌡️ {temp_label} ({temp_val})\n⏳ Запускаю генерацию...")
            progress_task = asyncio.create_task(run_progress_bar(callback.bot, req["chat_id"], callback.message.message_id, selected_label))
        except Exception:
            pass
        imgs = req.get("images_bytes") or ([req["image_bytes"]] if req.get("image_bytes") else None)
        gen_id = f"img_{request_id}"
        await save_pending_gen(gen_id=gen_id, gen_type="image", user_id=req["user_id"], chat_id=req["chat_id"],
            source_message_id=req["source_message_id"], message_thread_id=message_thread_id,
            prompt=req["prompt"], model=real_model, provider="gemini", file_ids=req.get("file_ids", []), model_label=selected_label)
        result_img, error_msg = await generate_image_with_gemini(req["prompt"], images_bytes=imgs, model=real_model, temperature=temp_val)
        if progress_task:
            progress_task.cancel()
            try: await progress_task
            except asyncio.CancelledError: pass
        await delete_pending_gen(gen_id)
        await _send_generation_result(callback.bot, req, request_id, result_img, error_msg, selected_label, imgs, reply_kwargs)
        return

    imgs = req.get("images_bytes") or ([req["image_bytes"]] if req.get("image_bytes") else [])
    try:
        await callback.message.edit_text(
            f"🌡️ {temp_label} ({temp_val}) — выбрано\n\nЧерез какую модель генерировать?",
            reply_markup=_providers_keyboard(request_id, req["chat_id"], len(imgs))
        )
    except Exception:
        pass


async def _send_generation_result(bot, request_data, request_id, result_img, error_msg, model_label, imgs, reply_kwargs):
    if error_msg:
        err_msg = await bot.send_message(
            chat_id=request_data["chat_id"],
            text=f"❌ Ошибка:\n{error_msg}\n\n⏳ Ща спрошу у мозгов, че не так...",
            reply_to_message_id=request_data["source_message_id"], **reply_kwargs
        )
        first_image = (imgs[0] if imgs else None)
        explanation = await explain_generation_error(request_data["prompt"] or "", error_msg, image_bytes=first_image)
        if not explanation or "Ебать, гугл зацензурил" in explanation:
            explanation = "Пиздец, твой промпт или фото настолько больное, что Гугл забанил даже попытку объяснить!"
        try:
            await bot.edit_message_text(chat_id=request_data["chat_id"], message_id=err_msg.message_id,
                text=f"❌ Ошибка:\n{error_msg}\n\n🧠 Пояснение:\n{explanation}")
        except Exception:
            pass
        return
    if result_img:
        caption = (f"🎨 Ваш результат ({model_label}) по запросу: {request_data['prompt']}"
                   if request_data["prompt"] else f"🎨 Ваш результат ({model_label}) готов.")
        await bot.send_photo(chat_id=request_data["chat_id"],
            photo=BufferedInputFile(result_img, filename="generated.jpg"),
            caption=caption, reply_to_message_id=request_data["source_message_id"], **reply_kwargs)
        upscale_msg = await bot.send_message(chat_id=request_data["chat_id"],
            text="⬆️ Улучшаю качество через AI upscaler...", **reply_kwargs)
        upscaled, up_err = await upscale_image(result_img)
        try:
            await bot.delete_message(chat_id=request_data["chat_id"], message_id=upscale_msg.message_id)
        except Exception:
            pass
        if upscaled:
            await bot.send_document(chat_id=request_data["chat_id"],
                document=BufferedInputFile(upscaled, filename="upscaled.png"),
                caption=f"✨ Улучшенная версия ({model_label}) 2x — без сжатия",
                reply_to_message_id=request_data["source_message_id"], **reply_kwargs)
        return
    await bot.send_message(chat_id=request_data["chat_id"], text="❌ Не удалось получить изображение.",
        reply_to_message_id=request_data["source_message_id"], **reply_kwargs)


_NSFW_STEPS = [20, 25, 28, 35, 50]
_NSFW_CFG   = [5.0, 6.5, 7.0, 8.5, 10.0]
_NSFW_SIZES = ["512x768", "768x1024", "896x1152", "1024x1024", "1024x1536"]
_NSFW_DEFAULT_NEG = "lowres, bad anatomy, bad hands, text, error, missing fingers, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, blurry"

def _nsfw_default_cfg(model: str) -> dict:
    if "flux" in model:
        return {"steps": 28, "cfg": 3.5, "size": "1024x1024", "neg": ""}
    return {"steps": 28, "cfg": 7.0, "size": "896x1152", "neg": _NSFW_DEFAULT_NEG}

def _nsfw_cfg_text(request_id: str) -> str:
    d = pending_nsfw_configs.get(request_id, {})
    cfg = d.get("cfg", {})
    prompt = d.get("prompt", "")[:80]
    neg = cfg.get("neg", "")[:60]
    label = d.get("label", "NSFW")
    neg_display = f'"{neg}"' if neg else "не задан"
    return (
        f"⚙️ {label}\n\n"
        f"📝 Промпт:\n\"{prompt}\"\n\n"
        f"🚫 Негативный промпт:\n{neg_display}\n\n"
        f"Шаги: {cfg.get('steps', 28)}  |  CFG: {cfg.get('cfg', 7.0)}  |  Размер: {cfg.get('size', '896x1152')}"
    )

def _nsfw_cfg_keyboard(request_id: str) -> InlineKeyboardMarkup:
    d = pending_nsfw_configs.get(request_id, {})
    cfg = d.get("cfg", {})
    cur_steps = cfg.get("steps", 28)
    cur_cfgv  = cfg.get("cfg", 7.0)
    cur_size  = cfg.get("size", "896x1152")

    def row(field, options, current):
        return [InlineKeyboardButton(
            text=f"{'✅' if str(o) == str(current) else ''}{o}",
            callback_data=f"nsfwcfg:{request_id}:{field}:{o}"
        ) for o in options]

    rows = [
        [
            InlineKeyboardButton(text="✏️ Изменить промпт", callback_data=f"nsfwinput:{request_id}:prompt"),
            InlineKeyboardButton(text="🚫 Негативный промпт", callback_data=f"nsfwinput:{request_id}:neg"),
        ],
        [InlineKeyboardButton(text="— Шаги —", callback_data="noop")],
        row("steps", _NSFW_STEPS, cur_steps),
        [InlineKeyboardButton(text="— CFG Scale —", callback_data="noop")],
        row("cfg", _NSFW_CFG, cur_cfgv),
        [InlineKeyboardButton(text="— Размер —", callback_data="noop")],
        [InlineKeyboardButton(
            text=f"{'✅' if s == cur_size else ''}{s}",
            callback_data=f"nsfwcfg:{request_id}:size:{s}"
        ) for s in _NSFW_SIZES[:3]],
        [InlineKeyboardButton(
            text=f"{'✅' if s == cur_size else ''}{s}",
            callback_data=f"nsfwcfg:{request_id}:size:{s}"
        ) for s in _NSFW_SIZES[3:]],
        [InlineKeyboardButton(text="🚀 Генерировать", callback_data=f"nsfwgen:{request_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


_nsfw_input_wait: dict = {}


@router.callback_query(F.data.startswith("nsfwinput:"))
async def handle_nsfw_input(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    _, request_id, field = parts
    d = pending_nsfw_configs.get(request_id)
    if not d:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != d["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return

    await callback.answer()
    field_name = "промпт" if field == "prompt" else "негативный промпт"
    hint = await callback.bot.send_message(
        chat_id=d["chat_id"],
        text=f"✏️ Напиши {field_name} ответом на это сообщение:",
        reply_markup=types.ForceReply(selective=True),
    )
    _nsfw_input_wait[hint.message_id] = {"request_id": request_id, "field": field, "user_id": d["user_id"]}


@router.message(F.reply_to_message & F.text)
async def handle_nsfw_text_input(message: types.Message):
    reply_to_id = message.reply_to_message.message_id if message.reply_to_message else None
    if reply_to_id not in _nsfw_input_wait:
        return
    wait = _nsfw_input_wait.pop(reply_to_id)
    if message.from_user.id != wait["user_id"]:
        return

    request_id = wait["request_id"]
    field = wait["field"]
    d = pending_nsfw_configs.get(request_id)
    if not d:
        return

    new_val = message.text.strip()
    if field == "prompt":
        d["prompt"] = new_val
    else:
        d["cfg"]["neg"] = new_val

    try:
        await message.reply_to_message.delete()
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass

    cfg_msg = await message.bot.send_message(
        chat_id=d["chat_id"],
        text=_nsfw_cfg_text(request_id),
        reply_markup=_nsfw_cfg_keyboard(request_id),
    )
    d["cfg_msg_id"] = cfg_msg.message_id


@router.callback_query(F.data == "noop")
async def handle_noop(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("nsfwcfg:"))
async def handle_nsfw_config(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    _, request_id, field, value = parts
    d = pending_nsfw_configs.get(request_id)
    if not d:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != d["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return

    if field == "steps":
        d["cfg"]["steps"] = int(value)
    elif field == "cfg":
        d["cfg"]["cfg"] = float(value)
    elif field == "size":
        d["cfg"]["size"] = value

    await callback.answer(f"✅ {field}: {value}")
    try:
        await callback.message.edit_text(_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
    except Exception:
        pass


@router.callback_query(F.data.startswith("nsfwgen:"))
async def handle_nsfw_generate(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 2:
        return
    _, request_id = parts
    d = pending_nsfw_configs.pop(request_id, None)
    if not d:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != d["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return

    await callback.answer()
    message_thread_id = d.get("message_thread_id")
    reply_kwargs = {"message_thread_id": message_thread_id} if message_thread_id else {}
    cfg = d.get("cfg", {})

    model = d["model"]
    label = d["label"]
    w, h = cfg.get("size", "1024x1024").split("x")

    neg = cfg.get("neg", "")
    from ai_services import _REPLICATE_MODELS
    if model in _REPLICATE_MODELS:
        if "flux" in model:
            _REPLICATE_MODELS[model]["input"] = lambda p, _s=cfg.get("steps",28), _c=cfg.get("cfg",3.5), _w=int(w), _h=int(h): {
                "prompt": p, "width": _w, "height": _h, "steps": _s, "guidance_scale": _c,
            }
        else:
            _REPLICATE_MODELS[model]["input"] = lambda p, _s=cfg.get("steps",28), _c=cfg.get("cfg",7.0), _w=int(w), _h=int(h), _n=neg: {
                "prompt": p,
                "negative_prompt": _n if _n else _NSFW_DEFAULT_NEG,
                "width": _w, "height": _h,
                "num_inference_steps": _s, "guidance_scale": _c,
            }

    progress_task = None
    try:
        await callback.message.edit_text(f"⏳ Генерирую через {label}...")
        progress_task = asyncio.create_task(run_progress_bar(callback.bot, d["chat_id"], callback.message.message_id, label))
    except Exception:
        pass

    result_img, error_msg = await generate_image_with_replicate(d["prompt"], model=model)

    if progress_task:
        progress_task.cancel()
        try: await progress_task
        except asyncio.CancelledError: pass

    await _send_generation_result(callback.bot, d, request_id, result_img, error_msg, label, None, reply_kwargs)


def _prompt_ai_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Использовать этот промт", callback_data=f"puse:{request_id}")],
        [
            InlineKeyboardButton(text="🔄 Другой вариант", callback_data=f"pother:{request_id}"),
            InlineKeyboardButton(text="📝 Мой промт", callback_data=f"pbase:{request_id}"),
        ],
    ])


async def _run_prompt_generation(callback: types.CallbackQuery, request_id: str):
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer("Запрос устарел.", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_text("🧠 Gemini Pro анализирует фото и промт...")
    except Exception:
        pass

    eng, rus, err = await generate_image_prompt(
        data["prompt"], data["images_bytes"], data["prev_prompts"]
    )

    if not eng:
        try:
            await callback.message.edit_text(f"❌ Ошибка генерации промта: {err}")
        except Exception:
            pass
        return

    data["current_ai_prompt"] = eng
    data["prev_prompts"].append(eng)

    rus_line = f"\n\n🇷🇺 По-русски:\n{rus}" if rus else ""
    text = (
        f"🤖 AI-промт готов:\n\n"
        f"🇬🇧 English:\n<code>{eng}</code>"
        f"{rus_line}"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_prompt_ai_keyboard(request_id))
    except Exception:
        pass


@router.callback_query(F.data.startswith("pask:"))
async def handle_prompt_ask(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 2:
        return
    _, request_id = parts
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != data["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return
    await _run_prompt_generation(callback, request_id)


@router.callback_query(F.data.startswith("pother:"))
async def handle_prompt_other(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 2:
        return
    _, request_id = parts
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != data["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return
    await _run_prompt_generation(callback, request_id)


@router.callback_query(F.data.startswith("puse:"))
async def handle_prompt_use(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 2:
        return
    _, request_id = parts
    data = pending_prompt_requests.pop(request_id, None)
    if not data:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != data["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return

    chosen_prompt = data.get("current_ai_prompt") or data["prompt"]
    req = pending_image_requests.get(request_id, {})
    req["prompt"] = chosen_prompt
    pending_image_requests[request_id] = req

    await callback.answer()
    req = pending_image_requests.get(request_id, {})
    try:
        await callback.message.edit_text(
            "Через какую модель генерировать?",
            reply_markup=_providers_keyboard(request_id, req.get("chat_id", data["chat_id"]), len(data.get("images_bytes") or []))
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("pbase:"))
async def handle_prompt_base(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 2:
        return
    _, request_id = parts
    data = pending_prompt_requests.pop(request_id, None)
    if not data:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    if callback.from_user.id != data["user_id"]:
        await callback.answer("Только автор запроса.", show_alert=True)
        return

    await callback.answer()
    req = pending_image_requests.get(request_id, {})
    try:
        await callback.message.edit_text(
            "Через какую модель генерировать?",
            reply_markup=_providers_keyboard(request_id, req.get("chat_id", data["chat_id"]), len(data.get("images_bytes") or []))
        )
    except Exception:
        pass


PROVIDER_MODELS: dict = {
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
    "nsfw": [
        ("WAI Illustrious v12", "wai12"),
        ("WAI Illustrious v11", "wai11"),
        ("NSFW FLUX Dev", "nsfwflux"),
    ],
}

MODEL_TO_REAL: dict = {
    "g31flash":   ("gemini", "gemini-3.1-flash-image-preview"),
    "g20flash":   ("gemini", "gemini-2.0-flash-preview-image-generation"),
    "gpt2":       ("gpt",    "gpt-image-2"),
    "dalle3":     ("gpt",    "dall-e-3"),
    "schnell":    ("flux",   "black-forest-labs/flux.1-schnell"),
    "fluxdev":    ("flux",   "black-forest-labs/flux.1-dev"),
    "klein":      ("flux",   "black-forest-labs/flux_2-klein-4b"),
    "wai12":      ("nsfw",   "aisha-ai-official/wai-nsfw-illustrious-v12"),
    "wai11":      ("nsfw",   "aisha-ai-official/wai-nsfw-illustrious-v11"),
    "nsfwflux":   ("nsfw",   "aisha-ai-official/nsfw-flux-dev"),
}


async def refresh_models():
    gemini_models = await fetch_gemini_image_models()
    if gemini_models:
        PROVIDER_MODELS["gemini"] = [(label, f"gi{i}") for i, (label, _) in enumerate(gemini_models)]
        for i, (_, model_id) in enumerate(gemini_models):
            MODEL_TO_REAL[f"gi{i}"] = ("gemini", model_id)

    openai_models = await fetch_openai_image_models()
    if openai_models:
        PROVIDER_MODELS["gpt"] = [(label, f"oi{i}") for i, (label, _) in enumerate(openai_models)]
        for i, (_, model_id) in enumerate(openai_models):
            MODEL_TO_REAL[f"oi{i}"] = ("gpt", model_id)

    veo_models = await fetch_veo_models()
    if veo_models:
        for i, (label, model_id) in enumerate(veo_models):
            VEO_MODELS[f"veo{i}"] = (label, model_id)

    logger.info(
        f"Models refreshed: Gemini={len(PROVIDER_MODELS['gemini'])} "
        f"GPT={len(PROVIDER_MODELS['gpt'])} "
        f"Veo={len(VEO_MODELS)}"
    )


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

    selected_label = next(
        (l for lst in PROVIDER_MODELS.values() for l, m in lst
         if next((k for k, (p, rm) in MODEL_TO_REAL.items() if rm == m and p == provider), None) == model_id),
        real_model
    )

    await callback.answer()

    if provider == "gemini":
        request_data["selected_model"] = real_model
        request_data["selected_provider"] = provider
        request_data["selected_label"] = selected_label
        try:
            await callback.message.edit_text(_temp_message(), reply_markup=_temp_keyboard(request_id))
        except Exception:
            pass
        return

    if provider == "nsfw":
        pending_image_requests.pop(request_id, None)
        pending_nsfw_configs[request_id] = {
            "user_id": request_data["user_id"],
            "chat_id": request_data["chat_id"],
            "source_message_id": request_data["source_message_id"],
            "message_thread_id": request_data["message_thread_id"],
            "prompt": request_data["prompt"],
            "model": real_model,
            "label": selected_label,
            "cfg": _nsfw_default_cfg(real_model),
        }
        try:
            await callback.message.edit_text(
                _nsfw_cfg_text(request_id),
                reply_markup=_nsfw_cfg_keyboard(request_id),
            )
        except Exception:
            pass
        return

    pending_image_requests.pop(request_id, None)

    message_thread_id = request_data["message_thread_id"]
    reply_kwargs = {}
    if message_thread_id:
        reply_kwargs["message_thread_id"] = message_thread_id

    await callback.bot.send_chat_action(
        chat_id=request_data["chat_id"], action="upload_photo",
        message_thread_id=message_thread_id
    )

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
            result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], images_bytes=imgs, temperature=request_data.get("temperature", 1.0))
        elif error_msg and is_openai_timeout_error(error_msg):
            await callback.bot.send_message(
                chat_id=request_data["chat_id"],
                text="⚠️ GPT не ответил вовремя. Переключаюсь на Gemini.",
                reply_to_message_id=request_data["source_message_id"],
                **reply_kwargs
            )
            selected_label = "Gemini Flash 3.1"
            result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], images_bytes=imgs, temperature=request_data.get("temperature", 1.0))
    elif provider == "flux":
        result_img, error_msg = await generate_image_with_nvidia(request_data["prompt"], model=real_model)
    elif provider == "nsfw":
        result_img, error_msg = await generate_image_with_replicate(request_data["prompt"], model=real_model)
    else:
        result_img, error_msg = await generate_image_with_gemini(request_data["prompt"], images_bytes=imgs, model=real_model, temperature=request_data.get("temperature", 1.0))

    if progress_task:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    await delete_pending_gen(gen_id)
    await _send_generation_result(callback.bot, request_data, request_id, result_img, error_msg, selected_label, imgs, reply_kwargs)

VEO_MODELS: dict = {
    "veo0": ("Veo 2",          "veo-2.0-generate-001"),
    "veo1": ("Veo 3.1 Fast",   "veo-3.1-fast-generate-preview"),
    "veo2": ("Veo 3.1",        "veo-3.1-generate-preview"),
    "veo3": ("Veo 3.1 Lite",   "veo-3.1-lite-generate-preview"),
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
def _track_user(message: types.Message):
    if message.from_user and message.chat.type != "private":
        cid = message.chat.id
        uid = message.from_user.id
        if cid not in chat_members_cache:
            chat_members_cache[cid] = {}
        chat_members_cache[cid][uid] = (
            message.from_user.first_name or "Аноним",
            message.from_user.username,
        )


@router.message(F.text)
async def handle_text_messages(message: types.Message):
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return

    # Проверяем, упомянули ли бота или ответили ли на его сообщение
    if message.reply_to_message and message.reply_to_message.message_id in _nsfw_input_wait:
        return

    bot_user = await message.bot.get_me()
    
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bot_user.username and f"@{bot_user.username}" in message.text

    is_private = message.chat.type == "private"

    if is_reply_to_bot or is_mentioned or is_private:
        # Анти-спам проверка
        current_time = time.time()
        last_time = user_text_cooldowns.get(message.from_user.id, 0)
        if current_time - last_time < TEXT_COOLDOWN_SECONDS:
            await message.reply(f"Заебал строчить, подожди еще {int(TEXT_COOLDOWN_SECONDS - (current_time - last_time))} сек.")
            return
        user_text_cooldowns[message.from_user.id] = current_time

        prompt = message.text
        if bot_user.username:
            prompt = prompt.replace(f"@{bot_user.username}", "").strip()

        if not prompt:
            prompt = "Что тебе надо, хуйло?"

        if is_reply_to_bot and message.reply_to_message.text:
            replied_text = message.reply_to_message.text[:500]
            prompt = f"[Контекст — ты написал ранее: «{replied_text}»]\n{prompt}"

        username = message.from_user.first_name or message.from_user.username or "Аноним"

        reply_kwargs = {}
        if message.chat.is_forum and message.message_thread_id:
            reply_kwargs["message_thread_id"] = message.message_thread_id

        thinking_msg = await message.reply("⏳ Думаю...", **reply_kwargs)
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing", message_thread_id=message.message_thread_id if message.chat.is_forum else None)

        _code_kw = [
            "напиши код", "напиши скрипт", "сделай скрипт", "напиши программу",
            "напиши функцию", "создай скрипт", "создай код", "напиши бота",
            "сделай бота", "напиши сайт", "сделай сайт", "напиши приложение",
            "создай сайт", "создай приложение", "напиши парсер", "сделай парсер",
            "напиши апи", "сделай апи", "напиши api", "напиши хэндлер",
            "реализуй", "write code", "write a script", "write a bot",
            "write a site", "write a website", "write an app",
        ]
        is_code_request = any(kw in prompt.lower() for kw in _code_kw)

        if is_code_request:
            text_response = await generate_code_with_gemini(prompt)
        else:
            text_response = await generate_text_with_gemini(prompt, message.chat.id, username=username)

        try:
            await thinking_msg.delete()
        except Exception:
            pass

        code_blocks = re.findall(r'```(\w*)\n(.*?)```', text_response, re.DOTALL)
        cleaned_text = re.sub(r'```(\w*)\n(.*?)```', '', text_response, flags=re.DOTALL).strip()

        if not cleaned_text and code_blocks:
            cleaned_text = "Вот твой ебаный код, подавись нахуй."
        elif not cleaned_text:
            cleaned_text = "Нихуя не понял, но иди в пизду."

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
