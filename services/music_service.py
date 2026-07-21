"""Lyria music generation — text-to-music via Gemini Interactions API."""

import asyncio as _asyncio
import base64
import logging
from typing import Tuple, Optional
from keys import load_keys

from shared_types import gemini_post

logger = logging.getLogger(__name__)

# Model IDs (via Interactions API)
LYRIA_CLIP = "lyria-3-clip-preview"      # 30s clips, MP3
LYRIA_PRO  = "lyria-3-pro-preview"        # Full songs, MP3/WAV
_BLOCK_CHECK_MODEL = LYRIA_CLIP

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
                    ratings = fb.get("safetyRatings", [])
                    cats = [r["category"].rsplit("_", 1)[-1] for r in ratings if r.get("blocked")]
                    cat_info = f" ({', '.join(cats)})" if cats else ""
                    if reason == "PROHIBITED_CONTENT":
                        diag = await _diagnose_block(prompt)
                        if diag:
                            cat_info += diag
                    errors.append(f"{mk}: BLOCKED ({reason}{cat_info})")
                    logger.warning(f"music: {mk}: BLOCKED ({reason}{cat_info})")
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


async def _diagnose_block(prompt: str) -> str:
    """Quick-check each segment of a blocked prompt to find the offender.

    Returns a human-readable annotation like '\\n  ↳ Строка 5: «в стиле Моргенштерна»'
    or empty string if nothing specific is found."""
    lines = [l.strip() for l in prompt.split("\n") if l.strip()]
    if len(lines) <= 1:
        return ""
    segments = [(i, line) for i, line in enumerate(lines) if len(line) > 10]
    if not segments:
        return ""

    async def _check_one(idx: int, text: str) -> tuple[int, str, bool]:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1},
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }
        try:
            data, _, _ = await gemini_post(
                f"models/{_BLOCK_CHECK_MODEL}:generateContent",
                payload,
                timeout=30,
            )
        except Exception:
            return idx, text, False
        if data:
            fb = data.get("promptFeedback", {})
            blocked = bool(fb.get("blockReason") or not data.get("candidates"))
            return idx, text, blocked
        return idx, text, False

    results = await _asyncio.gather(*(_check_one(i, t) for i, t in segments))
    blocked = [(idx, text) for idx, text, is_blocked in results if is_blocked]
    if not blocked:
        return ""
    if len(blocked) == 1:
        idx, text = blocked[0]
        snippet = text if len(text) <= 80 else text[:77] + "..."
        return f"\n  ↳ Строка {idx+1}: «{snippet}»"
    # Quote each blocked line, up to 5; summarize the rest
    quoted = []
    for idx, text in blocked[:5]:
        snippet = text if len(text) <= 80 else text[:77] + "..."
        quoted.append(f"  ↳ Строка {idx+1}: «{snippet}»")
    result = "\n".join(quoted)
    if len(blocked) > 5:
        result += f"\n  + ещё {len(blocked) - 5} строк"
    return f"\n{result}"
