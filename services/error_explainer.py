import base64
import logging
import aiohttp

from keys import load_keys

logger = logging.getLogger(__name__)

async def explain_generation_error(prompt: str, error_msg: str, image_bytes: bytes=None) -> str:
    keys = await load_keys()
    if not keys:
        return ''
    key = keys[0]
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}'
    parts = []
    if image_bytes:
        parts.append({'inlineData': {'mimeType': 'image/jpeg', 'data': base64.b64encode(image_bytes).decode()}})
    parts.append({'text': f"Пользователь пытался сгенерировать {('видео' if image_bytes else 'картинку')}.\nПромпт: {prompt or '<без текста>'}\nОшибка API: {error_msg[:400]}\n{('На изображении выше — фото которое он прикрепил.' if image_bytes else '')}\nОбъясни ОЧЕНЬ коротко и агрессивно (1-2 предложения) почему получил бан. Если причина в фото — укажи что именно на нём нарушает правила. Если в промпте — скажи на что триггернуло."})
    payload = {'contents': [{'parts': parts}], 'generationConfig': {'temperature': 0.5}}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['candidates'][0]['content']['parts'][0]['text'].strip()
        except Exception as e:
            logging.error(f'Ошибка объяснения ошибки генерации: {e}')
    return ''

