import asyncio
import aiohttp
import json
import base64
import tempfile
import os
import subprocess
import logging
from typing import Tuple, Optional

from config import (
    SYSTEM_PROMPT, 
    GEMINI_TEXT_TIMEOUT, 
    GEMINI_VIDEO_TIMEOUT, 
    GEMINI_IMAGE_TIMEOUT,
    OPENAI_TIMEOUT,
    NVIDIA_TIMEOUT,
    MAX_HISTORY_MESSAGES,
    MAX_VIDEO_FRAMES,
    VIDEO_FPS,
    VIDEO_FRAME_SIZE,
    MAX_API_RETRIES,
    RETRY_DELAY_SECONDS
)
from database import get_history, save_history
from keys_manager import load_keys, load_openai_key, load_openai_keys, load_nvidia_keys, load_openrouter_keys, remove_key

logger = logging.getLogger(__name__)

# ==========================================
# Генерация текста (обращение к Gemini Flash Lite)
# ==========================================
async def generate_text_with_gemini(prompt: str, chat_id: int) -> str:
    """Генерация текста через Gemini Flash Lite с историей чата"""
    keys = load_keys()
    
    if not keys:
        return "Блять, ключи закончились, иди нахуй."

    history = await get_history(chat_id)
    
    contents = []
    for msg in history:
        contents.append({"role": msg["role"], "parts": [{"text": msg["text"]}]})
        
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    for key in keys.copy():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={key}"
        
        payload = {
            "systemInstruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": contents,
            "generationConfig": {
                "temperature": 1.0,
                "thinkingConfig": {
                    "thinkingBudget": 0
                }
            }
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=GEMINI_TEXT_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            reply_text = data["candidates"][0]["content"]["parts"][0]["text"]
                            
                            # Сохраняем в историю
                            history.append({"role": "user", "text": prompt})
                            history.append({"role": "model", "text": reply_text})
                            # Храним только последние сообщения
                            if len(history) > MAX_HISTORY_MESSAGES:
                                history = history[-MAX_HISTORY_MESSAGES:]
                            await save_history(chat_id, history)
                                
                            return reply_text
                        except KeyError:
                            error_details = json.dumps(data, ensure_ascii=False)
                            return f"Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу. Ответ API: {error_details}"
                    elif resp.status in [429, 403, 400]:
                        resp_text = await resp.text()
                        logging.warning(f"Ошибка ключа (текст) {key[:10]}... Код: {resp.status}. Текст: {resp_text}")
                        remove_key(key)
                        continue # Пробуем следующий ключ
                    else:
                        resp_text = await resp.text()
                        logging.error(f"API Error {resp.status}: {resp_text}")
                        continue
            except Exception as e:
                logging.error(f"Сетевая ошибка (текст): {e}")
                continue # Сетевая ошибка, пробуем следующий

    return "Все ключи проебаны или сдохли, отъебись."

# ==========================================
# Анализ видео (обращение к Gemini)
# ==========================================
async def generate_video_with_gemini(prompt: str, video_path: str) -> str:
    """Анализ видео через Gemini с извлечением кадров и аудио"""
    keys = load_keys()
    if not keys:
        return "Блять, ключи закончились, иди нахуй."

    temp_dir = tempfile.mkdtemp()
    
    # Extract frames using ffmpeg
    cmd = [
        "ffmpeg", "-i", video_path, "-vf", f"fps={VIDEO_FPS},scale={VIDEO_FRAME_SIZE}:-1", 
        "-q:v", "10", os.path.join(temp_dir, "frame_%04d.jpg")
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    parts = []
    # Read frames
    frames = sorted(os.listdir(temp_dir))
    if not frames:
        os.system(f"rm -rf {temp_dir}")
        return "Не смог извлечь кадры из твоего уебищного видео. Может формат дерьмо?"
        
    # Limit frames to prevent payload from exceeding limits
    for frame in frames[:MAX_VIDEO_FRAMES]:
        with open(os.path.join(temp_dir, frame), "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": b64
                }
            })
            
    # Очищаем кадры
    for frame in frames:
        os.remove(os.path.join(temp_dir, frame))
        
    # Пытаемся извлечь аудио
    audio_path = os.path.join(temp_dir, "audio.wav")
    subprocess.run(["ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-y", audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
        with open(audio_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": "audio/wav",
                    "data": audio_b64
                }
            })
            
    os.system(f"rm -rf {temp_dir}")
            
    parts.append({"text": prompt if prompt else "Что происходит на этом видео? (учитывай и визуальный ряд, и звук, если он есть)"})
    
    for key in keys.copy():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={key}"
        
        payload = {
            "systemInstruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": [
                {
                    "parts": parts
                }
            ],
            "generationConfig": {
                "temperature": 1.0,
                "thinkingConfig": {
                    "thinkingBudget": 0
                }
            }
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=GEMINI_VIDEO_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            return data["candidates"][0]["content"]["parts"][0]["text"]
                        except KeyError:
                            return "Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу."
                    elif resp.status in [429, 403, 400]:
                        resp_text = await resp.text()
                        logging.warning(f"Ошибка ключа (видео) {key[:10]}... Код: {resp.status}. Текст: {resp_text}")
                        # Don't delete key on 400, because 400 might be just payload too large. 
                        # Delete on 429/403.
                        if resp.status != 400:
                            remove_key(key)
                        continue
                    else:
                        resp_text = await resp.text()
                        logging.error(f"API Error {resp.status}: {resp_text}")
                        continue
            except Exception as e:
                logging.error(f"Сетевая ошибка (видео): {e}")
                continue

    return "Все ключи проебаны или сдохли, отъебись."

# ==========================================
# Генерация изображения (обращение к Gemini)
# ==========================================
async def generate_image_with_gemini(prompt: str, image_bytes: Optional[bytes] = None, model: str = "gemini-3.1-flash-image-preview", images_bytes: list = None, temperature: float = 1.0) -> Tuple[Optional[bytes], Optional[str]]:
    keys = load_keys()
    
    if not keys:
        return None, "Нет доступных API ключей."

    all_images = images_bytes if images_bytes else ([image_bytes] if image_bytes else [])

    for key in keys.copy():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        
        parts = []
        for img in all_images:
            if img:
                parts.append({
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": base64.b64encode(img).decode('utf-8')
                    }
                })

        if len(all_images) > 1:
            multi_ref = (
                f"The {len(all_images)} photos above are ALL different photos of the SAME subject/person. "
                "Treat every photo as a reference of the exact same individual — same face, same identity. "
                "Do NOT blend different people. "
            )
            effective_prompt = multi_ref + (prompt if prompt else "Generate a high-quality image of this person.")
        else:
            effective_prompt = prompt if prompt else "A highly detailed beautiful picture"

        parts.append({"text": effective_prompt})
        
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": temperature},
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=GEMINI_IMAGE_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            # Достаем base64 из ответа
                            img_b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
                            img_bytes = base64.b64decode(img_b64)
                            return img_bytes, None
                        except KeyError:
                            error_details = json.dumps(data, ensure_ascii=False)
                            return None, f"Нейросеть не вернула изображение (возможно, запрос заблокирован цензурой).\n\n> Ответ API:\n> {error_details}"
                    elif resp.status in [429, 403, 400]:
                        resp_text = await resp.text()
                        logging.warning(f"Ошибка ключа (фото) {key[:10]}... Код: {resp.status}. Текст: {resp_text}")
                        remove_key(key)
                        continue
                    elif resp.status in [500, 502, 503, 504]:
                        resp_text = await resp.text()
                        logging.warning(f"Gemini временно недоступен (фото) {key[:10]}... Код: {resp.status}. Пробую следующий ключ.")
                        continue
                    else:
                        resp_text = await resp.text()
                        return None, f"Неизвестная ошибка API: {resp.status} - {resp_text}"
            except Exception as e:
                logging.error(f"Сетевая ошибка: {e}")
                continue # Сетевая ошибка, пробуем следующий

    return None, "Все API ключи исчерпали лимит или недействительны."

async def parse_openai_image_response(resp) -> Tuple[Optional[bytes], Optional[str]]:
    """Парсинг ответа от OpenAI API"""
    resp_text = await resp.text()

    if resp.status != 200:
        try:
            err_data = json.loads(resp_text)
            err_msg = err_data.get("error", {}).get("message", resp_text)
        except Exception:
            err_msg = resp_text
        return None, f"Ошибка OpenAI API ({resp.status}): {err_msg}"

    try:
        data = json.loads(resp_text)
        image_item = data["data"][0]

        if image_item.get("b64_json"):
            return base64.b64decode(image_item["b64_json"]), None

        image_url = image_item.get("url")
        if image_url:
            async with aiohttp.ClientSession() as dl_session:
                async with dl_session.get(image_url, timeout=120) as img_resp:
                    if img_resp.status == 200:
                        return await img_resp.read(), None
                    return None, f"OpenAI вернул URL, но скачивание не удалось ({img_resp.status})."

        return None, f"OpenAI не вернул изображение в ответе.\n\n> Ответ API:\n> {json.dumps(data, ensure_ascii=False)}"
    except Exception as e:
        return None, f"Не удалось разобрать ответ OpenAI: {e}\n\n> Сырой ответ API:\n> {resp_text}"

async def generate_image_with_gpt(prompt: str, image_bytes: Optional[bytes] = None, model: str = "gpt-image-2", images_bytes: list = None) -> Tuple[Optional[bytes], Optional[str]]:
    """Генерация изображения через OpenAI GPT"""
    all_ref_images = images_bytes if images_bytes else ([image_bytes] if image_bytes else [])

    api_keys = load_openai_keys()
    prompt_text = prompt if prompt else "A highly detailed beautiful picture"
    request_timeout = aiohttp.ClientTimeout(total=180)
    last_error = None

    for api_key in api_keys:
        headers = {"Authorization": f"Bearer {api_key}"}
        async with aiohttp.ClientSession() as session:
            try:
                if all_ref_images and not model.startswith("dall-e"):
                    form = aiohttp.FormData()
                    form.add_field("model", model)
                    form.add_field("prompt", prompt_text)
                    # gpt-image-2 поддерживает несколько референсов через image[]
                    for img in all_ref_images:
                        form.add_field("image[]", img, filename="input.jpg", content_type="image/jpeg")
                    async with session.post("https://api.openai.com/v1/images/edits", data=form, headers=headers, timeout=request_timeout) as resp:
                        result, error = await parse_openai_image_response(resp)
                else:
                    if model == "dall-e-3":
                        payload = {"model": model, "prompt": prompt_text, "n": 1, "size": "1024x1024"}
                    else:
                        payload = {"model": model, "prompt": prompt_text}
                    async with session.post("https://api.openai.com/v1/images/generations", json=payload, headers={**headers, "Content-Type": "application/json"}, timeout=request_timeout) as resp:
                        result, error = await parse_openai_image_response(resp)

                if result:
                    return result, None

                last_error = error
                if error and "(401)" in error:
                    logging.warning(f"OpenAI 401 на ключе {api_key[:12]}..., пробую следующий.")
                    continue
                return None, error

            except asyncio.TimeoutError:
                logger.error(f"Таймаут запроса OpenAI: ожидание превысило {OPENAI_TIMEOUT} секунд.")
                return None, f"Таймаут OpenAI: модель не ответила за {OPENAI_TIMEOUT} секунд. Попробуйте еще раз или выберите Gemini."
            except aiohttp.ClientError as e:
                logger.error(f"Сетевая ошибка OpenAI: {type(e).__name__}: {e}")
                return None, f"Сетевая ошибка OpenAI: {type(e).__name__}: {e}"
            except Exception as e:
                logger.exception("Неожиданная ошибка OpenAI")
                return None, f"Ошибка OpenAI: {type(e).__name__}: {e}"

    logging.info("Все OpenAI ключи упали, пробую через OpenRouter (openai/gpt-5.4-image-2)...")
    or_result, or_error = await generate_image_with_openrouter(prompt, model="openai/gpt-5.4-image-2")
    if or_result:
        return or_result, None
    logging.warning(f"OpenRouter тоже не помог: {or_error}")

    return None, or_error or last_error or "GPT недоступен: нет рабочих ключей."

def is_openai_verification_error(error_msg: str) -> bool:
    if not error_msg:
        return False

    lowered = error_msg.lower()
    return (
        "organization must be verified" in lowered
        or "must be verified" in lowered
        or "verify organization" in lowered
    )

async def generate_image_with_openrouter(prompt: str, model: str = "google/gemini-3.1-flash-image-preview"):
    api_keys = load_openrouter_keys()
    if not api_keys:
        return None, "Нет ключей OpenRouter. Добавьте sk-or-... ключи в r.txt."

    prompt_text = prompt if prompt else "A highly detailed beautiful picture"
    url = "https://openrouter.ai/api/v1/chat/completions"
    modalities = ["image"] if "flux" in model or "seedream" in model or "riverflow" in model else ["image", "text"]
    payload = {
        "model": model,
        "modalities": modalities,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt_text}]}],
    }
    request_timeout = aiohttp.ClientTimeout(total=300)
    last_error = None

    for api_key in api_keys:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers, timeout=request_timeout) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        raw_text = raw.decode("utf-8", errors="replace")

                        try:
                            import re as _re
                            b64_match = _re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', raw_text)
                            if b64_match:
                                return base64.b64decode(b64_match.group(1)), None
                        except Exception:
                            pass

                        try:
                            d = json.loads(raw_text)
                            msg = d.get("choices", [{}])[0].get("message", {})
                            for src in [msg.get("images", []), msg.get("content", []) or []]:
                                for part in src:
                                    if isinstance(part, dict) and part.get("type") == "image_url":
                                        img_url = part.get("image_url", {}).get("url", "")
                                        if img_url.startswith("data:"):
                                            return base64.b64decode(img_url.split(",", 1)[1]), None
                                        if img_url.startswith("http"):
                                            async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=60)) as img_resp:
                                                if img_resp.status == 200:
                                                    return await img_resp.read(), None
                        except Exception:
                            pass

                        return None, "OpenRouter не вернул изображение в ответе."

                    err_text = await resp.text()
                    last_error = f"Ошибка OpenRouter ({resp.status}): {err_text[:200]}"
                    if resp.status in [401, 403]:
                        logging.warning(f"OpenRouter {resp.status} на ключе {api_key[:12]}..., пробую следующий.")
                        continue
            except asyncio.TimeoutError:
                last_error = "Таймаут OpenRouter"
                continue
            except Exception as e:
                last_error = str(e)
                continue
    return None, last_error


_UPSCALE_CLIENT_ID = "b4f2e8a1c6d9f3b0e7a2c5d8f1b4e7a0"
_UPSCALE_BASE = "https://image-upscaling.net"

async def _upscale_imageupscaling(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    cookies = {"client_id": _UPSCALE_CLIENT_ID}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(cookies=cookies) as session:
        form = aiohttp.FormData()
        form.add_field("scale", "2")
        form.add_field("model", "plus")
        form.add_field("image", image_bytes, filename="image.jpg", content_type="image/jpeg")

        try:
            async with session.post(f"{_UPSCALE_BASE}/upscaling_upload", data=form, timeout=timeout) as resp:
                if resp.status != 200:
                    return None, await resp.text()
                original_filename = (await resp.text()).strip()
        except Exception as e:
            return None, str(e)

        dl_timeout = aiohttp.ClientTimeout(total=15)
        for _ in range(20):
            await asyncio.sleep(4)
            try:
                async with session.get(f"{_UPSCALE_BASE}/upscaling_get_status_v2", timeout=dl_timeout) as resp:
                    if resp.status != 200:
                        continue
                    items = await resp.json()
                    for item in items:
                        if item.get("original_filename") == original_filename and item.get("completed"):
                            async with session.get(item["image_url"], timeout=dl_timeout) as dl:
                                if dl.status == 200:
                                    return await dl.read(), None
            except Exception:
                continue

    return None, "Upscale timeout"

async def _upscale_picwish(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        from picwish import PicWish
        pw = PicWish()
        result = await asyncio.wait_for(pw.enhance(image_bytes), timeout=60)
        data = await asyncio.wait_for(result.get_bytes(), timeout=30)
        return data, None
    except asyncio.TimeoutError:
        return None, "PicWish timeout"
    except Exception as e:
        return None, str(e)

async def upscale_image(image_bytes: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    result, err = await _upscale_imageupscaling(image_bytes)
    if result:
        return result, None

    logging.warning(f"image-upscaling.net failed ({err}), trying PicWish")
    result, err2 = await _upscale_picwish(image_bytes)
    if result:
        return result, None

    return None, f"Все апскейлеры недоступны. upscaling.net: {err} | picwish: {err2}"

def is_openai_timeout_error(error_msg: str) -> bool:
    if not error_msg:
        return False

    lowered = error_msg.lower()
    return "таймаут openai" in lowered or "timeout" in lowered

async def explain_generation_error(prompt: str, error_msg: str, image_bytes: bytes = None) -> str:
    keys = load_keys()
    if not keys:
        return ""
    key = keys[0]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={key}"

    parts = []
    if image_bytes:
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}})
    parts.append({"text": (
        f"Пользователь пытался сгенерировать {'видео' if image_bytes else 'картинку'}.\n"
        f"Промпт: {prompt or '<без текста>'}\n"
        f"Ошибка API: {error_msg[:400]}\n"
        f"{'На изображении выше — фото которое он прикрепил.' if image_bytes else ''}\n"
        "Объясни ОЧЕНЬ коротко и агрессивно (1-2 предложения) почему получил бан. "
        "Если причина в фото — укажи что именно на нём нарушает правила. "
        "Если в промпте — скажи на что триггернуло."
    )})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.5}
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            logging.error(f"Ошибка объяснения ошибки генерации: {e}")
    return ""

async def analyze_image_for_veo(image_bytes: bytes, user_prompt: str = "") -> str:
    keys = load_keys()
    if not keys:
        return user_prompt or ""
    key = keys[0]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={key}"
    lang_hint = "на русском" if user_prompt and any('\u0400' <= c <= '\u04ff' for c in user_prompt) else "in English"
    ask = (
        f"Опиши это изображение подробно {lang_hint} для генерации видео через Veo: "
        f"что изображено, кто/что главный объект, их внешность, поза, выражение, фон, освещение, стиль. "
        f"Дай описание в 2-3 предложения — только описание, без пояснений."
    )
    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}},
            {"text": ask}
        ]}],
        "generationConfig": {"temperature": 0.2}
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    description = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    logging.info(f"Gemini описал фото для Veo: {description[:80]}...")
                    return description
        except Exception as e:
            logging.warning(f"Ошибка анализа фото для Veo: {e}")
    return user_prompt or ""

async def start_veo_generation(prompt: str, model: str = "veo-2.0-generate-001", image_bytes: bytes = None) -> tuple:
    keys = load_keys()
    if not keys:
        return None, None, "Нет Gemini ключей для генерации видео."

    prompt_text = prompt if prompt else "A beautiful cinematic scene"

    for key in keys:
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        instance = {"prompt": prompt_text}
        if image_bytes:
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(image_bytes).decode("utf-8"),
                "mimeType": "image/jpeg",
            }
        payload = {
            "instances": [instance],
            "parameters": {
                "aspectRatio": "16:9",
                "durationSeconds": 8,
                "personGeneration": "allow_adult" if image_bytes else "allow_all",
            }
        }
        base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(f"{base_url}:predictLongRunning", json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logging.warning(f"Veo start {resp.status} ключ {key[:12]}: {err[:100]}")
                        if resp.status in [429, 403]:
                            continue
                        return None, None, f"Ошибка Veo ({resp.status}): {err[:200]}"
                    op_data = await resp.json()
                    op_name = op_data.get("name", "")
                    if op_name:
                        return op_name, key, None
                    return None, None, f"Veo не вернул имя операции: {op_data}"
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                return None, None, f"Ошибка Veo start: {type(e).__name__}: {e}"

    return None, None, "Все Gemini ключи исчерпаны."

async def poll_veo_operation(operation_name: str, api_key: str) -> tuple:
    poll_url = f"https://generativelanguage.googleapis.com/v1beta/{operation_name}?key={api_key}"
    async with aiohttp.ClientSession() as session:
        for _ in range(60):
            await asyncio.sleep(5)
            try:
                async with session.get(poll_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    poll_data = await resp.json()
                    if not poll_data.get("done"):
                        continue
                    if "error" in poll_data:
                        return None, f"Ошибка Veo: {poll_data['error'].get('message', str(poll_data['error']))}"
                    response_obj = poll_data.get("response", {})
                    video_response = response_obj.get("generateVideoResponse", response_obj)
                    samples = video_response.get("generatedSamples", [])
                    if not samples:
                        reasons = video_response.get("raiMediaFilteredReasons", [])
                        if reasons:
                            return None, f"Veo заблокировал видео фильтром безопасности:\n{reasons[0][:300]}"
                        return None, f"Veo не вернул видео: {json.dumps(response_obj, ensure_ascii=False)[:400]}"
                    video = samples[0].get("video", {})
                    encoded = video.get("encodedVideo", "")
                    if encoded:
                        return base64.b64decode(encoded), None
                    uri = video.get("uri", "")
                    if uri:
                        dl_headers = {"x-goog-api-key": api_key}
                        async with session.get(uri, headers=dl_headers, timeout=aiohttp.ClientTimeout(total=120)) as dl:
                            if dl.status == 200:
                                return await dl.read(), None
                            return None, f"Ошибка скачивания ({dl.status}): {(await dl.text())[:200]}"
                    return None, f"Veo: нет видеоданных в ответе."
            except Exception:
                continue
    return None, "Veo: операция не завершилась за 5 минут."

async def generate_video_with_veo(prompt: str, model: str = "veo-2.0-generate-001", image_bytes: bytes = None) -> tuple:
    op_name, api_key, error = await start_veo_generation(prompt, model, image_bytes)
    if error:
        return None, error
    return await poll_veo_operation(op_name, api_key)

async def translate_to_english(prompt: str) -> str:
    """Перевод промпта на английский для NVIDIA"""
    import re
    if not re.search(r'[а-яёА-ЯЁ]', prompt):
        return prompt

    keys = load_keys()
    if not keys:
        return prompt

    key = keys[0]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={key}"
    payload = {
        "contents": [{"parts": [{"text": f"Translate this image generation prompt to English for an AI image generator. Return ONLY the translated prompt, no explanations:\n{prompt}"}]}],
        "generationConfig": {
            "temperature": 0.1,
            "thinkingConfig": {"thinkingBudget": 0}
        }
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    translated = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    logging.info(f"Промпт переведён: '{prompt}' → '{translated}'")
                    return translated
        except Exception as e:
            logging.error(f"Ошибка перевода промпта: {e}")

    return prompt

async def generate_image_with_nvidia(prompt: str, model: str = "black-forest-labs/flux.1-schnell") -> Tuple[Optional[bytes], Optional[str]]:
    """Генерация изображения через NVIDIA NIM"""
    api_keys = load_nvidia_keys()

    if not api_keys:
        return None, "Нет ключей NVIDIA NIM. Добавьте nvapi-... ключи в r.txt."

    prompt_text = await translate_to_english(prompt) if prompt else "A highly detailed beautiful picture"
    url = f"https://ai.api.nvidia.com/v1/genai/{model}"

    if "schnell" in model:
        steps, cfg_scale = 4, 0
    elif "klein" in model:
        steps, cfg_scale = 8, 2.0
    else:
        steps, cfg_scale = 30, 3.5

    payload = {
        "prompt": prompt_text,
        "width": 1024,
        "height": 1024,
        "steps": steps,
        "seed": 0,
        "cfg_scale": cfg_scale,
    }
    request_timeout = aiohttp.ClientTimeout(total=NVIDIA_TIMEOUT)
    last_error = None

    for api_key in api_keys:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers, timeout=request_timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        artifacts = data.get("artifacts", [])
                        if artifacts and artifacts[0].get("base64"):
                            return base64.b64decode(artifacts[0]["base64"]), None
                        return None, f"NVIDIA не вернул изображение. Ответ: {json.dumps(data, ensure_ascii=False)[:300]}"
                    resp_text = await resp.text()
                    last_error = f"Ошибка NVIDIA NIM ({resp.status}): {resp_text[:300]}"
                    logging.warning(f"NVIDIA NIM {resp.status} на ключе {api_key[:12]}..., пробую следующий.")
                    continue
            except asyncio.TimeoutError:
                return None, f"Таймаут NVIDIA NIM: модель не ответила за {NVIDIA_TIMEOUT} секунд."
            except aiohttp.ClientError as e:
                return None, f"Сетевая ошибка NVIDIA NIM: {type(e).__name__}: {e}"
            except Exception as e:
                logger.exception("Неожиданная ошибка NVIDIA NIM")
                return None, f"Ошибка NVIDIA NIM: {type(e).__name__}: {e}"

    return None, last_error


_CODE_SYSTEM_PROMPT = (
    "You are an elite senior software engineer. You write COMPLETE, PRODUCTION-READY code.\n"
    "Rules:\n"
    "- ALWAYS write the FULL implementation — never truncate, never use '...', '# TODO', or '# rest of code'\n"
    "- No placeholders whatsoever — every function must be fully implemented\n"
    "- Proper error handling throughout\n"
    "- Clean architecture, meaningful variable names\n"
    "- For web: modern responsive design, Tailwind or detailed CSS, complex JS, SVG icons where appropriate\n"
    "- For scripts: handle edge cases, proper argument parsing if needed\n"
    "- HTML: always include <meta charset='UTF-8'> in <head>\n"
    "- Code must be runnable from first line to last with zero modifications\n"
    "- Return code in a single markdown code block. Add brief explanation only if asked."
)


async def generate_code_with_gemini(prompt: str) -> str:
    keys = load_keys()
    if not keys:
        return "Ключи сдохли."

    for model_name in ["gemini-3.1-pro-preview", "gemini-3.1-flash-preview", "gemini-3.1-flash-lite-preview"]:
        for key in keys[:3]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
            payload = {
                "systemInstruction": {"parts": [{"text": _CODE_SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 8192,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(
                        url, json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=120)
                    ) as resp:
                        if resp.status == 404:
                            break
                        if resp.status == 200:
                            data = await resp.json()
                            return data["candidates"][0]["content"]["parts"][0]["text"]
                        if resp.status in [429, 403]:
                            remove_key(key)
                            continue
                except Exception:
                    continue

    return "Все модели недоступны."


_models_cache: dict = {}
_MODELS_CACHE_TTL = 3600


def _pretty_model_name(model_id: str) -> str:
    name = model_id.replace("-preview", "").replace("-generate", "").replace("-001", "")
    parts = name.split("-")
    out = []
    for p in parts:
        if p in ("pro", "flash", "lite", "fast", "ultra", "image", "mini"):
            out.append(p.capitalize())
        elif p in ("veo", "gpt", "dall", "e"):
            out.append(p.upper())
        elif p.replace(".", "").isdigit():
            out.append(p)
        else:
            out.append(p.capitalize())
    return " ".join(out)


async def fetch_gemini_image_models() -> list:
    cache_key = "gemini_image"
    now = __import__("time").time()
    if cache_key in _models_cache and now - _models_cache[cache_key]["ts"] < _MODELS_CACHE_TTL:
        return _models_cache[cache_key]["data"]

    keys = load_keys()
    if not keys:
        return []

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={keys[0]}&pageSize=200"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = []
                    for m in data.get("models", []):
                        model_id = m["name"].replace("models/", "")
                        methods = m.get("supportedGenerationMethods", [])
                        if "image" in model_id.lower() and "generateContent" in methods:
                            result.append((_pretty_model_name(model_id), model_id))
                    _models_cache[cache_key] = {"ts": now, "data": result}
                    return result
        except Exception as e:
            logging.warning(f"fetch_gemini_image_models: {e}")
    return []


async def fetch_openai_image_models() -> list:
    cache_key = "openai_image"
    now = __import__("time").time()
    if cache_key in _models_cache and now - _models_cache[cache_key]["ts"] < _MODELS_CACHE_TTL:
        return _models_cache[cache_key]["data"]

    api_keys = load_openai_keys()
    if not api_keys:
        return []

    headers = {"Authorization": f"Bearer {api_keys[0]}"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                "https://api.openai.com/v1/models", headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    skip = {"dall-e-2", "gpt-image-1-mini"}
                    result = []
                    for m in sorted(data.get("data", []), key=lambda x: x["id"], reverse=True):
                        mid = m["id"]
                        if any(p in mid for p in ("gpt-image", "dall-e", "chatgpt-image")):
                            if mid not in skip:
                                result.append((_pretty_model_name(mid), mid))
                    _models_cache[cache_key] = {"ts": now, "data": result}
                    return result
        except Exception as e:
            logging.warning(f"fetch_openai_image_models: {e}")
    return []


async def fetch_veo_models() -> list:
    cache_key = "veo"
    now = __import__("time").time()
    if cache_key in _models_cache and now - _models_cache[cache_key]["ts"] < _MODELS_CACHE_TTL:
        return _models_cache[cache_key]["data"]

    keys = load_keys()
    if not keys:
        return []

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={keys[0]}&pageSize=200"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = []
                    for m in data.get("models", []):
                        model_id = m["name"].replace("models/", "")
                        if "veo" in model_id.lower():
                            result.append((_pretty_model_name(model_id), model_id))
                    _models_cache[cache_key] = {"ts": now, "data": result}
                    return result
        except Exception as e:
            logging.warning(f"fetch_veo_models: {e}")
    return []


async def generate_image_prompt(
    prompt: str,
    images_bytes: list,
    prev_prompts: list = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    keys = load_keys()
    if not keys:
        return None, None, "Нет ключей"

    key = keys[0]
    prev_prompts = prev_prompts or []

    sys_text = (
        "You are a world-class AI image generation prompt engineer specializing in Midjourney, DALL-E, Flux, and Stable Diffusion. "
        "You analyze reference photo(s) and the user's rough idea, then craft the perfect detailed generation prompt. "
        "Include: subject description, art style, lighting, composition, mood, quality tags (masterpiece, 8k, highly detailed, etc.). "
        "Respond ONLY in this exact format — nothing else:\n"
        "ENGLISH: <your detailed English prompt>\n"
        "RUSSIAN: <Russian translation>"
    )

    photo_count = len([b for b in images_bytes if b])
    prev_note = ""
    if prev_prompts:
        prev_note = " Previously generated (make a DIFFERENT one): " + " | ".join(f'"{p}"' for p in prev_prompts[-3:])

    user_text = (
        f"{'Two' if photo_count > 1 else 'One'} reference photo{'s are' if photo_count > 1 else ' is'} attached. "
        f"User's idea: \"{prompt}\".{prev_note} "
        f"Generate the optimal image generation prompt."
    )

    parts = []
    for img in images_bytes:
        if img:
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(img).decode()}})
    parts.append({"text": user_text})

    for model_name in ["gemini-3.1-pro-preview", "gemini-3.1-flash-preview", "gemini-3.1-flash-lite-preview"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
        payload = {
            "systemInstruction": {"parts": [{"text": sys_text}]},
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.95},
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=35)
                ) as resp:
                    if resp.status == 404:
                        continue
                    if resp.status == 200:
                        data = await resp.json()
                        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                        english, russian = "", ""
                        for line in raw.split("\n"):
                            if line.upper().startswith("ENGLISH:"):
                                english = line[8:].strip()
                            elif line.upper().startswith("RUSSIAN:"):
                                russian = line[8:].strip()
                        if not english:
                            english = raw.split("\n")[0][:300]
                        return english, russian, None
                    err = await resp.text()
                    return None, None, f"API {resp.status}: {err[:150]}"
            except Exception as e:
                return None, None, str(e)

    return None, None, "Все модели недоступны"
