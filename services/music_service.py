"""Lyria music generation — text-to-music via Gemini Interactions API."""

import base64
import logging
from typing import Tuple, Optional
from keys import load_keys
from shared_types import gemini_post

logger = logging.getLogger(__name__)

# Model IDs (via Interactions API)
LYRIA_CLIP = "lyria-3-clip-preview"      # 30s clips, MP3
LYRIA_PRO  = "lyria-3-pro-preview"        # Full songs, MP3/WAV

MUSIC_MODELS = {
    "lyria-clip":  {"id": LYRIA_CLIP, "label": "🎵 Lyria 3 Clip",   "desc": "30-секундные клипы, MP3"},
    "lyria-pro":   {"id": LYRIA_PRO,  "label": "🎼 Lyria 3 Pro",    "desc": "Полноценные песни до 2 мин, MP3/WAV"},
}

MUSIC_MODEL_LIST = list(MUSIC_MODELS.keys())



async def generate_music(
    prompt: str,
    model_key: str = "lyria-clip",
    output_format: str = "mp3",
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Generate music via Lyria generateContent API. Falls back through all models."""
    if model_key not in MUSIC_MODELS:
        return None, None, f"Неизвестная модель: {model_key}"

    if not await load_keys():
        return None, None, "Нет доступных Gemini ключей."
    # Fallback chain: selected model → other models
    model_chain = [model_key] + [k for k in MUSIC_MODEL_LIST if k != model_key]
    errors = []

    for mk in model_chain:
        info = MUSIC_MODELS[mk]
        model_id = info["id"]

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 1.0},
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }

        data, used_key, err = await gemini_post(f"models/{model_id}:generateContent", payload, timeout=120)
        if data:
            candidates = data.get("candidates", [])
            if not candidates:
                fb = data.get("promptFeedback", {})
                reason = fb.get("blockReason", "")
                if reason:
                    errors.append(f"{mk}: BLOCKED ({reason})")
                    logger.warning(f"music: {mk}: BLOCKED ({reason})")
                else:
                    errors.append(f"{mk}: empty candidates")
                continue
            c = candidates[0]
            parts = c.get("content", {}).get("parts", [])
            audio_b64 = None
            lyrics_parts = []
            for p in parts:
                if "inlineData" in p:
                    audio_b64 = p["inlineData"].get("data", "")
                elif "text" in p:
                    lyrics_parts.append(p["text"])

            audio_bytes = base64.b64decode(audio_b64) if audio_b64 else None
            lyrics = "\n".join(lyrics_parts) or None

            if audio_bytes:
                logger.info(f"music: generated {len(audio_bytes)//1024}KB via {mk}")
                return audio_bytes, lyrics, None

            errors.append(f"{mk}: empty audio")
            logger.warning(f"music: {mk}: empty audio in response")
        else:
            errors.append(f"{mk}: {err}")
    # Deduplicate and count errors
    unique = list(dict.fromkeys(errors))  # preserve order, remove dupes
    summary = unique[0] if unique else "неизвестная ошибка"
    rest = f" + ещё {len(errors)-1}" if len(errors) > 1 else ""
    return None, None, f"Lyria: {summary}{rest}"
