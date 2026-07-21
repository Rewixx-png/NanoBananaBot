import asyncio
import base64
import logging
import aiohttp
from typing import Tuple, Optional

from keys import load_keys, remove_key
from shared_types import _models_cache, _MODELS_CACHE_TTL, _pretty_model_name, _build_text_system_prompt, _gemini_url, _gemini_headers, gemini_post, gemini_text_of


# ── Audio service implementation ──────────────────────────────────────────
async def analyze_voice_with_gemini(audio_bytes: bytes, mime_type: str, prompt: str) -> str:
    if not await load_keys():
        return 'Блять, ключи закончились, иди нахуй.'
    if not prompt:
        prompt = 'Что сказано в этом голосовом сообщении? Транскрибируй и ответь по существу.'
    system = _build_text_system_prompt(allow_web_directive=False)
    parts = [
        {'inlineData': {'mimeType': mime_type, 'data': base64.b64encode(audio_bytes).decode()}},
        {'text': prompt},
    ]
    payload = {
        'systemInstruction': {'parts': [{'text': system}]},
        'contents': [{'role': 'user', 'parts': parts}],
        'generationConfig': {'temperature': 1.0, 'thinkingConfig': {'thinkingLevel': 'minimal'}},
    }
    data, key, err = await gemini_post("models/gemini-3.5-flash:generateContent", payload, timeout=30)
    if data:
        text = gemini_text_of(data)
        if not text:
            return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
        return text
    if err:
        err_lower = err.lower()
        if any(w in err_lower for w in ('safety', 'prohibited', 'harm', 'block', 'policy', 'recitation')):
            return 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
        logging.warning(f'Ошибка аудио Gemini: {err}')
    return 'Все ключи проебаны или сдохли, отъебись.'
async def fetch_gemini_tts_models() -> list:
    cache_key = 'gemini_tts'
    now = __import__('time').time()
    if cache_key in _models_cache and now - _models_cache[cache_key]['ts'] < _MODELS_CACHE_TTL:
        return _models_cache[cache_key]['data']
    keys = await load_keys()
    if not keys:
        return []
    url = _gemini_url("models") + "?pageSize=200"
    headers = _gemini_headers(keys[0])
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = []
                    for m in data.get('models', []):
                        model_id = m['name'].replace('models/', '')
                        methods = m.get('supportedGenerationMethods', [])
                        if 'tts' in model_id.lower() and 'generateContent' in methods:
                            result.append((_pretty_model_name(model_id), model_id))
                    _models_cache[cache_key] = {'ts': now, 'data': result}
                    return result
        except Exception as e:
            logging.warning(f'fetch_gemini_tts_models: {e}')
    return []
def _split_text(text: str, max_chars: int = 800) -> list:
    import re
    sentences = re.split(r'(?<=[.!?\n])\s+', text)
    chunks = []
    current_chunk = []
    current_len = 0
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if current_len + len(s) > max_chars:
            if current_chunk:
                chunks.append(" ".join(current_chunk))
            current_chunk = [s]
            current_len = len(s)
        else:
            current_chunk.append(s)
            current_len += len(s) + 1
    if current_chunk:
        chunks.append(" ".join(current_chunk))
    return chunks
async def generate_tts_with_gemini(text: str, model: str, voice_name: str, temperature: float=1.0, language_code: str='ru-RU', scene: str='', style: str='', pace: str='', accent: str='', _is_chunk: bool=False) -> Tuple[Optional[bytes], Optional[str]]:
    import wave
    import io
    if not await load_keys():
        return (None, 'Нет ключей Gemini.')

    if '#### TRANSCRIPT' in text:
        text = text.split('#### TRANSCRIPT')[-1].strip()

    if not _is_chunk and len(text) > 800:
        chunks = _split_text(text, 800)
        tasks = []
        for chunk in chunks:
            tasks.append(generate_tts_with_gemini(
                chunk, model, voice_name, temperature, language_code,
                scene, style, pace, accent, _is_chunk=True
            ))
        results = await asyncio.gather(*tasks)
        pcm_chunks = []
        for res, err in results:
            if err:
                return (None, f"Ошибка в части текста: {err}")
            if res:
                pcm_chunks.append(res)
        if not pcm_chunks:
            return (None, "Не удалось сгенерировать части аудио.")
        pcm_data = b"".join(pcm_chunks)
        import tempfile
        import subprocess
        import os
        (fd, temp_wav) = tempfile.mkstemp(suffix='.wav')
        os.close(fd)
        with wave.open(temp_wav, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm_data)
        temp_ogg = temp_wav.replace('.wav', '.ogg')
        subprocess.run(['ffmpeg', '-i', temp_wav, '-c:a', 'libopus', '-b:a', '48k', '-y', temp_ogg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ogg_data = None
        if os.path.exists(temp_ogg):
            with open(temp_ogg, 'rb') as f:
                ogg_data = f.read()
            os.remove(temp_ogg)
        os.remove(temp_wav)
        if ogg_data:
            return (ogg_data, None)
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm_data)
        return (wav_io.getvalue(), None)

    full_text = ''
    has_advanced = scene or style or pace or accent
    if pace:
        pace_lower = pace.lower()
        if 'extremely fast' in pace_lower or 'жестко быстро' in pace_lower or 'очень очень быстро' in pace_lower:
            text = '[extremely fast] ' + text
        elif 'very fast' in pace_lower or 'очень быстро' in pace_lower:
            text = '[very fast] ' + text
        elif 'fast' in pace_lower or 'быстро' in pace_lower:
            text = '[fast] ' + text
        elif 'extremely slow' in pace_lower or 'жестко медленно' in pace_lower:
            text = '[extremely slow] ' + text
        elif 'very slow' in pace_lower or 'очень медленно' in pace_lower:
            text = '[very slow] ' + text
        elif 'slow' in pace_lower or 'медленно' in pace_lower:
            text = '[slow] ' + text
    if style:
        style_lower = style.lower()
        for tag in ['whispering', 'shouting', 'cheerful', 'excited', 'serious', 'tired', 'panicked']:
            if tag in style_lower:
                text = f'[{tag}] ' + text
                break
        if 'шепот' in style_lower or 'тихо' in style_lower:
            text = '[whispering] ' + text
        elif 'крик' in style_lower or 'громко' in style_lower:
            text = '[shouting] ' + text
        elif 'весел' in style_lower or 'радост' in style_lower:
            text = '[cheerful] ' + text
        elif 'возбужд' in style_lower or 'эмоционально' in style_lower:
            text = '[excited] ' + text
        elif 'серьезн' in style_lower:
            text = '[serious] ' + text
        elif 'устал' in style_lower:
            text = '[tired] ' + text
        elif 'паник' in style_lower or 'испуг' in style_lower:
            text = '[panicked] ' + text
    if not has_advanced:
        full_text += (
            "You are an advanced text-to-speech system. Please synthesize the transcript below into spoken audio exactly as written.\n"
            "#### TRANSCRIPT\n"
        )
    else:
        full_text += (
            "You are an advanced text-to-speech system. Please synthesize the following text into spoken audio "
            "following the performance guidelines below. Do not read the audio profile or director's notes aloud. "
            "Read only the text under the TRANSCRIPT section.\n\n"
        )
        full_text += f'# AUDIO PROFILE: {voice_name}\n'
        if scene:
            full_text += f'## THE SCENE: {scene}\n'
        if style or pace or accent:
            full_text += "### PERFORMANCE\n"
            if style:
                full_text += f'Style: {style}\n'
            if pace:
                full_text += f'Pace: {pace}\n'
            if accent:
                full_text += f'Accent: {accent}\n'
        full_text += '\n#### TRANSCRIPT\n'
    full_text += text
    last_err = 'Нет доступных ключей.'
    payload = {
        'contents': [{'parts': [{'text': full_text}]}],
        'generationConfig': {
            'temperature': temperature,
            'responseModalities': ['AUDIO'],
            'speechConfig': {
                'languageCode': language_code,
                'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voice_name}}
            }
        },
        'safetySettings': [
            {'category': 'HARM_CATEGORY_HARASSMENT',       'threshold': 'BLOCK_NONE'},
            {'category': 'HARM_CATEGORY_HATE_SPEECH',       'threshold': 'BLOCK_NONE'},
            {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_NONE'},
            {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_NONE'},
            {'category': 'HARM_CATEGORY_CIVIC_INTEGRITY',   'threshold': 'BLOCK_NONE'},
        ]
    }
    for attempt in range(2):
        data, used_key, err = await gemini_post(f"models/{model}:generateContent", payload, timeout=300)
        if data:
            candidates = data.get('candidates', [])
            if not candidates:
                if used_key:
                    remove_key(used_key, 400)
                last_err = 'Gemini заблокировал текст или вернул пустой ответ.'
                break
            candidate = candidates[0]
            finish_reason = candidate.get('finishReason', '')
            if finish_reason and finish_reason != 'STOP' and finish_reason != 'MAX_TOKENS':
                if used_key:
                    remove_key(used_key, 400)
                last_err = f'Ошибка генерации (Finish Reason: {finish_reason}).'
                break
            parts = candidate.get('content', {}).get('parts', [])
            for part in parts:
                if 'inlineData' in part:
                    b64_data = part['inlineData']['data']
                    pcm_data = base64.b64decode(b64_data)
                    if _is_chunk:
                        return (pcm_data, None)
                    import tempfile
                    import subprocess
                    import os
                    (fd, temp_wav) = tempfile.mkstemp(suffix='.wav')
                    os.close(fd)
                    with wave.open(temp_wav, 'wb') as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(24000)
                        wav_file.writeframes(pcm_data)
                    temp_ogg = temp_wav.replace('.wav', '.ogg')
                    subprocess.run(['ffmpeg', '-i', temp_wav, '-c:a', 'libopus', '-b:a', '48k', '-y', temp_ogg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    ogg_data = None
                    if os.path.exists(temp_ogg):
                        with open(temp_ogg, 'rb') as f:
                            ogg_data = f.read()
                        os.remove(temp_ogg)
                    os.remove(temp_wav)
                    if ogg_data:
                        return (ogg_data, None)
                    wav_io = io.BytesIO()
                    with wave.open(wav_io, 'wb') as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(24000)
                        wav_file.writeframes(pcm_data)
                    return (wav_io.getvalue(), None)
            last_err = 'Gemini не вернул аудио-данные.'
            break
        if err:
            last_err = f'Ошибка Gemini TTS: {err}'
            if attempt < 1:
                await asyncio.sleep(0.3)
                continue
            break
    return (None, f'Все API ключи исчерпаны или недоступны. Последняя ошибка: {last_err}')

