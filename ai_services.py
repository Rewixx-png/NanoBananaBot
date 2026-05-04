import asyncio
import aiohttp
import json
import base64
import tempfile
import os
import subprocess
import logging

from config import SYSTEM_PROMPT
from database import get_history, save_history
from keys_manager import load_keys, load_openai_key, load_openai_keys, load_nvidia_keys, remove_key

# ==========================================
# Генерация текста (обращение к Gemini Flash Lite)
# ==========================================
async def generate_text_with_gemini(prompt: str, chat_id: int):
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
                    "thinkingBudget": -1
                }
            }
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            reply_text = data["candidates"][0]["content"]["parts"][0]["text"]
                            
                            # Сохраняем в историю
                            history.append({"role": "user", "text": prompt})
                            history.append({"role": "model", "text": reply_text})
                            # Храним только 10 последних сообщений (5 пар)
                            if len(history) > 10:
                                history = history[-10:]
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
async def generate_video_with_gemini(prompt: str, video_path: str):
    keys = load_keys()
    if not keys:
        return "Блять, ключи закончились, иди нахуй."

    temp_dir = tempfile.mkdtemp()
    
    # Extract frames using ffmpeg at 24fps, scale down to 256x256 to save payload size
    cmd = [
        "ffmpeg", "-i", video_path, "-vf", "fps=24,scale=256:-1", 
        "-q:v", "10", os.path.join(temp_dir, "frame_%04d.jpg")
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    parts = []
    # Read frames
    frames = sorted(os.listdir(temp_dir))
    if not frames:
        os.system(f"rm -rf {temp_dir}")
        return "Не смог извлечь кадры из твоего уебищного видео. Может формат дерьмо?"
        
    # Limit frames to prevent payload from exceeding limits. 
    # 24fps * 12 seconds = ~288 frames
    max_frames = 300
    for frame in frames[:max_frames]:
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
                    "thinkingBudget": -1
                }
            }
        }

        async with aiohttp.ClientSession() as session:
            try:
                # Payload could be a few MBs so we wait up to 60s
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=60) as resp:
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
async def generate_image_with_gemini(prompt: str, image_bytes: bytes = None, model: str = "gemini-3.1-flash-image-preview"):
    keys = load_keys()
    
    if not keys:
        return None, "Нет доступных API ключей."

    for key in keys.copy():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        
        # Формируем запрос
        parts = []
        if image_bytes:
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode('utf-8')
                }
            })
        
        parts.append({"text": prompt if prompt else "A highly detailed beautiful picture"})
        
        payload = {
            "contents": [
                {
                    "parts": parts
                }
            ]
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=60) as resp:
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

async def parse_openai_image_response(resp):
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

async def generate_image_with_gpt(prompt: str, image_bytes: bytes = None, model: str = "gpt-image-2"):
    api_keys = load_openai_keys()

    if not api_keys:
        return None, "Не найден OPENAI API ключ. Добавьте ключи в r.txt."

    prompt_text = prompt if prompt else "A highly detailed beautiful picture"
    request_timeout = aiohttp.ClientTimeout(total=180)
    last_error = None

    for api_key in api_keys:
        headers = {"Authorization": f"Bearer {api_key}"}
        async with aiohttp.ClientSession() as session:
            try:
                if image_bytes and model == "gpt-image-2":
                    form = aiohttp.FormData()
                    form.add_field("model", model)
                    form.add_field("prompt", prompt_text)
                    form.add_field("image", image_bytes, filename="input.jpg", content_type="image/jpeg")
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
                logging.error("Таймаут запроса OpenAI: ожидание превысило 180 секунд.")
                return None, "Таймаут OpenAI: модель не ответила за 180 секунд. Попробуйте еще раз или выберите Gemini."
            except aiohttp.ClientError as e:
                logging.error(f"Сетевая ошибка OpenAI: {type(e).__name__}: {e}")
                return None, f"Сетевая ошибка OpenAI: {type(e).__name__}: {e}"
            except Exception as e:
                logging.exception("Неожиданная ошибка OpenAI")
                return None, f"Ошибка OpenAI: {type(e).__name__}: {e}"

    return None, last_error

def is_openai_verification_error(error_msg: str) -> bool:
    if not error_msg:
        return False

    lowered = error_msg.lower()
    return (
        "organization must be verified" in lowered
        or "must be verified" in lowered
        or "verify organization" in lowered
    )

def is_openai_timeout_error(error_msg: str) -> bool:
    if not error_msg:
        return False

    lowered = error_msg.lower()
    return "таймаут openai" in lowered or "timeout" in lowered

async def translate_to_english(prompt: str) -> str:
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
            "thinkingConfig": {"thinkingBudget": -1}
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

async def generate_image_with_nvidia(prompt: str, model: str = "black-forest-labs/flux.1-schnell"):
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
    request_timeout = aiohttp.ClientTimeout(total=120)
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
                return None, "Таймаут NVIDIA NIM: модель не ответила за 120 секунд."
            except aiohttp.ClientError as e:
                return None, f"Сетевая ошибка NVIDIA NIM: {type(e).__name__}: {e}"
            except Exception as e:
                logging.exception("Неожиданная ошибка NVIDIA NIM")
                return None, f"Ошибка NVIDIA NIM: {type(e).__name__}: {e}"

    return None, last_error
