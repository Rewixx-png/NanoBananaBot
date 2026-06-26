import asyncio
import io
import logging
import os
import subprocess
import sys
import tempfile
import traceback
import aiohttp
from typing import Tuple, Optional, Any

from keys import load_keys, remove_key

logger = logging.getLogger(__name__)

# ── PIL code generation service ───────────────────────────────────────────
_PIL_CODE_SYSTEM = (
    "You are a Python PIL/Pillow image generation expert. "
    "When given a description, write complete Python code using PIL that draws it. "
    "Rules:\n"
    "- Use ONLY the provided names: Image, ImageDraw, ImageFont, ImageFilter, ImageOps, io, math, random, colorsys\n"
    "- Do NOT import anything\n"
    "- Canvas size: 512x512 minimum, up to 1024x1024\n"
    "- Store final image in variable named `result_image` (PIL Image object)\n"
    "- Be creative: use colors, shapes, gradients, details\n"
    "- Do NOT use file I/O, subprocess, os, sys, requests, or any network calls\n"
    "- Do NOT include any print() statements\n"
    "- Return ONLY raw Python code, no markdown fences, no explanations"
)



def _clean_generated_python_code(raw: str) -> str:
    text = strip_code_fences(raw or '')
    text = re.sub(r'```(?:python|py)?', '', text, flags=re.IGNORECASE).replace('```', '')
    text = re.sub(r'^\s*python\s*\n', '', text, flags=re.IGNORECASE).strip()
    lines = text.splitlines()
    starts = (
        'from PIL', 'import ', 'width =', 'height =', 'w =', 'h =', 'W =', 'H =',
        'canvas =', 'image =', 'img =', 'result_image =', 'size =', 'background =',
    )
    start_idx = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(starts) or re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*=\s*Image\.', stripped):
            start_idx = idx
            break
    cleaned = '\n'.join(lines[start_idx:]).strip()
    cleaned = re.sub(r'\n\s*Here(?: is|\'s).*$', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    safe_imports = {'io', 'math', 'random', 'colorsys'}
    cleaned_lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        normalized = re.sub(r'\s+', ' ', stripped)
        if normalized.startswith('from PIL import '):
            continue
        import_match = re.fullmatch(r'import ([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)', normalized)
        if import_match:
            modules = {part.strip() for part in import_match.group(1).split(',')}
            if modules.issubset(safe_imports):
                continue
        cleaned_lines.append(line)
    return '\n'.join(cleaned_lines).strip()



def _generated_code_error_message(code: str, error: Exception) -> str:
    line_no = getattr(error, 'lineno', None)
    message = f'{type(error).__name__}: {error}'
    if not isinstance(line_no, int) or line_no <= 0:
        return message
    lines = code.splitlines()
    start = max(0, line_no - 3)
    end = min(len(lines), line_no + 2)
    snippet = ' | '.join(f'{idx + 1}: {lines[idx].strip()[:140]}' for idx in range(start, end))
    return f'{message}. Bad code near line {line_no}: {snippet}'



def _fallback_draw_image(prompt: str) -> bytes:
    import io as _io
    import math as _math
    from PIL import Image as _Image, ImageDraw as _ImageDraw, ImageFilter as _ImageFilter
    width = 768
    height = 768
    seed = sum(ord(ch) for ch in (prompt or 'image'))
    bg1 = ((seed * 37) % 120 + 40, (seed * 53) % 120 + 40, (seed * 71) % 120 + 70)
    bg2 = ((seed * 17) % 120 + 90, (seed * 29) % 120 + 70, (seed * 43) % 120 + 40)
    img = _Image.new('RGB', (width, height), bg1)
    px = img.load()
    if px is not None:
        for y in range(height):
            t = y / max(1, height - 1)
            r = int(bg1[0] * (1 - t) + bg2[0] * t)
            g = int(bg1[1] * (1 - t) + bg2[1] * t)
            b = int(bg1[2] * (1 - t) + bg2[2] * t)
            for x in range(width):
                glow = int(22 * _math.sin((x + seed) / 80) * _math.cos((y + seed) / 95))
                px[x, y] = (max(0, min(255, r + glow)), max(0, min(255, g + glow)), max(0, min(255, b + glow)))
    draw = _ImageDraw.Draw(img)
    lowered = (prompt or '').lower()
    for i in range(34):
        x = (seed * (i + 11) * 37) % width
        y = (seed * (i + 17) * 53) % height
        radius = 2 + (seed + i * 7) % 8
        color = (220 + i % 35, 220 - i % 80, 180 + i % 55)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    if any(word in lowered for word in ('геймпад', 'gamepad', 'джойстик', 'controller')):
        shadow = (178, 258, 590, 610)
        body = (158, 228, 570, 570)
        draw.rounded_rectangle(shadow, radius=120, fill=(20, 20, 28))
        draw.rounded_rectangle(body, radius=120, fill=(36, 38, 50), outline=(230, 230, 245), width=7)
        draw.ellipse((235, 335, 315, 415), fill=(16, 17, 24), outline=(120, 130, 150), width=5)
        draw.ellipse((405, 335, 485, 415), fill=(16, 17, 24), outline=(120, 130, 150), width=5)
        draw.rectangle((206, 377, 286, 397), fill=(235, 235, 245))
        draw.rectangle((246, 337, 266, 417), fill=(235, 235, 245))
        button_colors = [(244, 81, 93), (82, 202, 118), (89, 154, 245), (245, 207, 82)]
        for idx, (cx, cy) in enumerate(((480, 345), (525, 390), (435, 390), (480, 435))):
            draw.ellipse((cx - 21, cy - 21, cx + 21, cy + 21), fill=button_colors[idx], outline=(255, 255, 255), width=3)
        draw.rounded_rectangle((318, 438, 402, 460), radius=11, fill=(110, 115, 135))
        draw.ellipse((305, 292, 345, 332), fill=(90, 100, 130))
        draw.ellipse((375, 292, 415, 332), fill=(90, 100, 130))
    elif any(word in lowered for word in ('кот', 'cat', 'кошка')):
        draw.ellipse((225, 230, 543, 575), fill=(238, 174, 92), outline=(80, 42, 22), width=6)
        draw.polygon([(255, 265), (300, 145), (360, 275)], fill=(238, 174, 92), outline=(80, 42, 22))
        draw.polygon([(410, 275), (470, 145), (515, 265)], fill=(238, 174, 92), outline=(80, 42, 22))
        draw.ellipse((300, 360, 340, 400), fill=(20, 25, 28))
        draw.ellipse((455, 360, 495, 400), fill=(20, 25, 28))
        draw.polygon([(390, 425), (365, 455), (415, 455)], fill=(80, 42, 42))
        for y in (430, 455):
            draw.line((255, y, 350, y + 10), fill=(80, 42, 22), width=4)
            draw.line((445, y + 10, 545, y), fill=(80, 42, 22), width=4)
    elif any(word in lowered for word in ('машин', 'car', 'авто')):
        draw.rounded_rectangle((165, 335, 605, 500), radius=45, fill=(220, 55, 65), outline=(255, 240, 240), width=6)
        draw.polygon([(260, 335), (335, 245), (495, 245), (560, 335)], fill=(70, 145, 210), outline=(245, 245, 255))
        draw.rectangle((345, 265, 475, 330), fill=(120, 190, 235))
        draw.ellipse((230, 470, 330, 570), fill=(25, 25, 30), outline=(235, 235, 235), width=8)
        draw.ellipse((485, 470, 585, 570), fill=(25, 25, 30), outline=(235, 235, 235), width=8)
    else:
        draw.ellipse((190, 160, 578, 548), fill=(245, 210, 95), outline=(255, 255, 245), width=8)
        draw.rounded_rectangle((245, 355, 520, 590), radius=48, fill=(65, 90, 180), outline=(235, 245, 255), width=6)
        draw.polygon([(384, 210), (445, 355), (384, 505), (323, 355)], fill=(235, 80, 105), outline=(255, 245, 245))
        draw.ellipse((334, 315, 414, 395), fill=(255, 255, 245), outline=(30, 35, 50), width=4)
        draw.ellipse((358, 339, 390, 371), fill=(30, 35, 50))
    img = img.filter(_ImageFilter.UnsharpMask(radius=2, percent=130, threshold=3))
    buf = _io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf.read()



def _execute_pil_code_to_png(code: str) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        import io as _io
        from PIL import Image as _Image, ImageDraw as _ImageDraw, ImageFont as _ImageFont, ImageFilter as _ImageFilter, ImageOps as _ImageOps
        import math as _math, random as _random, colorsys as _colorsys
        allowed_builtins = {
            'abs': abs, 'all': all, 'any': any, 'bool': bool, 'dict': dict, 'enumerate': enumerate,
            'float': float, 'int': int, 'len': len, 'list': list, 'max': max, 'min': min,
            'pow': pow, 'range': range, 'round': round, 'set': set, 'sorted': sorted, 'str': str,
            'sum': sum, 'tuple': tuple, 'zip': zip,
        }
        sandbox: dict[str, Any] = {
            '__builtins__': allowed_builtins,
            'io': _io, 'Image': _Image, 'ImageDraw': _ImageDraw, 'ImageFont': _ImageFont,
            'ImageFilter': _ImageFilter, 'ImageOps': _ImageOps, 'math': _math,
            'random': _random, 'colorsys': _colorsys,
        }
        exec(compile(code, '<generated>', 'exec'), sandbox)
        result_image: Any = sandbox.get('result_image')
        if result_image is None:
            return (None, 'Код выполнился, но `result_image` не создан.')
        if not hasattr(result_image, 'save'):
            return (None, '`result_image` не является PIL Image.')
        buf = _io.BytesIO()
        result_image.save(buf, format='PNG')
        buf.seek(0)
        return (buf.read(), None)
    except Exception as e:
        details = _generated_code_error_message(code, e)
        logging.warning(f'PIL code execution failed: {details}\nCode:\n{code[:1200]}')
        return (None, f'Ошибка выполнения кода: {details}')



async def generate_image_via_code(prompt: str, state_data: Optional[dict[str, Any]] = None, max_attempts: int = 5) -> Tuple[Optional[bytes], Optional[str], str]:
    from ai_services import _thinking_config, _TEXT_MODEL_FALLBACKS
    from services.gemini_image import review_image_with_gemini
    keys = await load_keys()
    if not keys:
        return (None, 'Нет API ключей.', '')
    attempts = max(1, min(max_attempts, 5))
    critique = ''
    last_error = ''
    for attempt in range(attempts):
        if state_data:
            state_data['status'] = f'Gemini пишет код {attempt + 1}/{attempts}'
        feedback = critique or last_error
        user_text = f'User prompt: {prompt[:1800]}\nAttempt: {attempt + 1}/{attempts}'
        if feedback:
            user_text += f'\nPrevious attempt feedback: {feedback[:1200]}\nWrite a corrected, better version from scratch.'
        payload = {
            'systemInstruction': {'parts': [{'text': _PIL_CODE_SYSTEM}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_text}]}],
            'generationConfig': {'temperature': 0.8, 'thinkingConfig': _thinking_config('gemini-3.5-flash', 'minimal')},
        }
        code = ''
        request_error = ''
        for key in keys.copy():
            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}'
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            raw = data['candidates'][0]['content']['parts'][0]['text']
                            code = _clean_generated_python_code(raw)
                            break
                        err = await resp.text()
                        request_error = f'Gemini ошибка {resp.status}: {err[:200]}'
                        if resp.status in [429, 403]:
                            remove_key(key, resp.status)
                            continue
                        if resp.status == 404:
                            break
            except Exception as e:
                request_error = f'Ошибка запроса к Gemini: {type(e).__name__}: {e}'
                continue
            if code:
                break
        if not code:
            last_error = request_error or 'Gemini не вернул код.'
            continue
        if state_data:
            state_data['status'] = f'Выполняю код {attempt + 1}/{attempts}'
        result_img, exec_error = _execute_pil_code_to_png(code)
        if exec_error or not result_img:
            last_error = exec_error or 'Код не вернул изображение.'
            continue
        if state_data:
            state_data['status'] = f'Gemini проверяет фото {attempt + 1}/{attempts}'
        ok, critique = await review_image_with_gemini(result_img, prompt)
        if ok:
            return (result_img, None, critique)
        last_error = critique or 'Проверка забраковала изображение.'
    final_error = critique or last_error or 'Не удалось получить нормальную картинку.'
    try:
        if state_data:
            state_data['status'] = 'Собираю резервную картинку'
        fallback_img = _fallback_draw_image(prompt)
        return (fallback_img, None, final_error[:200])
    except Exception as e:
        return (None, f'Проверка забраковала результат после {attempts} попыток: {final_error[:200]}; fallback: {type(e).__name__}: {e}', critique)


