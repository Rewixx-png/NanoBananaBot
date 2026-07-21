"""Image, video, TTS, QR, chart, and translation tools."""

import asyncio
import io
import logging
import os
import re
import tempfile
from typing import Callable
from urllib.parse import urlparse

import aiohttp

from services.gemini_image import generate_image_with_gemini
from services.openai_service import generate_image_with_gpt
from services.audio_service import generate_tts_with_gemini
from keys import load_keys, load_openai_keys

from config import AGENT_VIDEO_DOWNLOAD_TIMEOUT, TELEGRAM_MEDIA_MAX_BYTES
from .search import _fc_search, _fc_scrape, _search_image_urls, _download_bytes, _image_is_relevant

logger = logging.getLogger(__name__)

_VDL_TIMEOUT = AGENT_VIDEO_DOWNLOAD_TIMEOUT
_TG_MAX_BYTES = TELEGRAM_MEDIA_MAX_BYTES


async def _tool_search_image(
    query: str,
    description: str,
    send_cb: Callable,
    status_cb: Callable,
) -> str:
    """Autonomous image search: formulate query → find URLs → evaluate → retry."""
    keys = await load_keys()
    search_desc = description or query
    tried_queries: list[str] = []
    max_rounds = 3

    async def _st(text: str):
        try:
            await status_cb(text)
        except Exception:
            pass

    for rnd in range(max_rounds):
        # Refine query if previous rounds failed
        if rnd == 0:
            current_query = query
        else:
            # Ask Gemini to suggest a better image search query
            if keys:
                payload = {
                    "contents": [{"parts": [{"text":
                        f"Generate a better Google Images search query to find: '{search_desc}'. "
                        f"Previous failed queries: {tried_queries}. "
                        "Return ONLY the query string, no explanations."
                    }]}],
                    "generationConfig": {"temperature": 0.5, "maxOutputTokens": 30},
                }
                url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.post(
                            url, json=payload,
                            headers={"Content-Type": "application/json", "x-goog-api-key": keys[0]},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                current_query = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                            else:
                                current_query = f"{query} hd high quality"
                except Exception:
                    current_query = f"{query} hd high quality"
            else:
                current_query = f"{query} hd"

        tried_queries.append(current_query)
        await _st(f"🔍 Ищу картинку: «{current_query}» (попытка {rnd + 1}/{max_rounds})")

        image_urls = await _search_image_urls(current_query)

        await _st(f"📥 Нашёл {len(image_urls)} кандидатов, проверяю...")

        for img_url in image_urls[:8]:
            img_bytes = await _download_bytes(img_url, max_bytes=10 * 1024 * 1024)
            if not img_bytes or len(img_bytes) < 1000:
                continue
            relevant = await _image_is_relevant(img_bytes, search_desc)
            if relevant:
                await _st("✅ Нашёл подходящую картинку, отправляю...")
                ext = img_url.split("?")[0].rsplit(".", 1)[-1].lower()
                fname = f"image.{ext}" if ext in ("jpg", "jpeg", "png", "webp") else "image.jpg"
                await send_cb({
                    "type": "photo", "data": img_bytes,
                    "caption": f"🖼 {search_desc[:900]}", "filename": fname,
                })
                safe_url = img_url.replace('\n', '').replace('\r', '').replace('\t', '')[:200]
                return f"[ОТПРАВЛЕНО] Картинка найдена и отправлена. Источник: {safe_url} (запрос: '{current_query}')"

        await _st(f"⚠️ Ни одна картинка не подошла, формулирую лучший запрос...")

    return f"[НЕ НАЙДЕНО] Не удалось найти подходящую картинку после {max_rounds} попыток. Пробовал: {tried_queries}. Скажи пользователю честно что не нашёл."


# ── Other tool implementations ───────────────────────────────────

async def _tool_generate_image(prompt: str, send_cb: Callable, provider: str = "gemini", model: str = "") -> str:
    """Generate image. Tries requested provider/model, falls back to Gemini with notification."""
    img_bytes: bytes | None = None
    note = ""

    _GPT_MODELS = {"gpt-image-2", "gpt-image-1.5", "dall-e-3", "dall-e-2"}
    _GPT_PROVIDERS = {"openai", "gpt", "gpt4", "dalle", "dall-e"}

    use_gpt = provider.lower() in _GPT_PROVIDERS or model in _GPT_MODELS or model.startswith("openai/")

    if use_gpt:
        gpt_model = model if model else "gpt-image-2"
        img_bytes, err = await generate_image_with_gpt(prompt, model=gpt_model)
        used_provider = f"OpenAI ({gpt_model})"
        if not img_bytes:
            note = f"\n⚠️ OpenAI недоступен ({(err or '').split(':')[0].strip()[:80]}), сгенерировал через Gemini."
            img_bytes, err = await generate_image_with_gemini(prompt)
            used_provider = "Gemini"
    else:
        img_bytes, err = await generate_image_with_gemini(prompt)
        used_provider = "Gemini"

    if not img_bytes:
        return f"Image generation failed: {err or 'no data'}"

    caption = f"🎨 {prompt[:900]}{note}"
    await send_cb({"type": "photo", "data": img_bytes, "caption": caption[:1024], "filename": "image.jpg"})
    return f"[ОТПРАВЛЕНО] Картинка через {used_provider}.{note}"


async def _tool_list_image_models() -> str:
    """Fetch available image-generation models from OpenAI API."""
    keys = load_openai_keys()
    if not keys:
        return "OpenAI ключи не настроены."
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {keys[0]}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return f"OpenAI API error: HTTP {resp.status}"
                data = await resp.json()
        models = [m["id"] for m in data.get("data", [])
                  if any(k in m["id"] for k in ("dall-e", "gpt-image", "image"))]
        models.sort()
        if not models:
            return "Нет доступных image-моделей на этом ключе."
        return "Доступные image-модели OpenAI:\n" + "\n".join(f"• {m}" for m in models)
    except Exception as e:
        return f"Ошибка при запросе моделей: {e}"


async def _tool_download_image(url: str, caption: str, send_cb: Callable) -> str:
    data = await _download_bytes(url)
    if not data:
        return "Failed to download image."
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    fname = f"image.{ext}" if ext in ("jpg", "jpeg", "png", "webp", "gif") else "image.jpg"
    await send_cb({"type": "photo", "data": data,
                   "caption": caption[:1024] or url[:200], "filename": fname})
    return "Image downloaded and sent."


_COOKIE_FILES = {
    "youtube.com": "/root/cookies/youtube.txt",
    "youtu.be":    "/root/cookies/youtube.txt",
    "tiktok.com":  "/root/cookies/tiktok.txt",
    "instagram.com": "/root/cookies/instagram.txt",
    "x.com":       "/root/cookies/x.txt",
    "twitter.com": "/root/cookies/x.txt",
    "reddit.com":  "/root/cookies/reddit.txt",
}

def _cookies_for_url(url: str) -> list[str]:
    """Return --cookies flag list for the given URL domain, if cookie file exists."""
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return []
    for domain, path in _COOKIE_FILES.items():
        if (hostname == domain or hostname.endswith("." + domain)) and os.path.exists(path):
            return ["--cookies", path]
    return []


async def _tool_download_video(url: str, caption: str, send_cb: Callable) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "video.%(ext)s")
        cmd = [
            "yt-dlp", "--no-playlist", "--no-warnings",
            "-f", "bestvideo[height<=720][filesize<45M]+bestaudio/best[height<=720]/best[height<=480]",
            "--merge-output-format", "mp4", "-o", out,
            *_cookies_for_url(url),  # auto-inject cookies from host, never from sandbox
            url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_VDL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return "Video download timed out (2 min)."
        if proc.returncode != 0:
            return f"yt-dlp error: {stderr.decode()[:300]}"
        files = [f for f in os.listdir(tmpdir) if f.startswith("video.")]
        if not files:
            return "yt-dlp: no output file."
        fpath = os.path.join(tmpdir, files[0])
        size  = os.path.getsize(fpath)
        if size > _TG_MAX_BYTES:
            return f"Video too large ({size // 1024 // 1024} MB > 48 MB)."
        with open(fpath, "rb") as f:
            data = f.read()
        await send_cb({"type": "video", "data": data,
                       "caption": caption[:1024] or "📹 Видео", "filename": files[0]})
        return f"Video ({size // 1024 // 1024} MB) sent."


async def _find_video_urls(query: str, creator: str = "") -> list[str]:
    """Extract YouTube / TikTok / VK URLs from Firecrawl search results.
    If creator is given, tries channel-specific search first."""
    search_queries = []
    if creator:
        slug = re.sub(r'[^a-zA-Z0-9_]', '', creator.replace(' ', '').replace('.', ''))
        search_queries.append(f"{query} site:tiktok.com/@{slug}")
        search_queries.append(f"{query} site:youtube.com @{creator}")
    search_queries.append(f"{query} site:youtube.com OR site:tiktok.com OR site:vk.com")

    patterns = [
        r'https?://(?:www\.)?youtube\.com/watch\?[^\s\)"\'<>]+',
        r'https?://youtu\.be/[^\s\)"\'<>]+',
        r'https?://(?:www\.)?tiktok\.com/@[^\s\)"\'<>]+/video/[^\s\)"\'<>]+',
        r'https?://vm\.tiktok\.com/[^\s\)"\'<>]+',
        r'https?://vk\.com/video[^\s\)"\'<>]+',
    ]

    seen: set[str] = set()
    clean: list[str] = []

    for q in search_queries[:2]:
        results = await _fc_search(q)
        for pat in patterns:
            for u in re.findall(pat, results):
                u = u.rstrip('.,;)"\'>]')
                if u not in seen:
                    seen.add(u)
                    # If creator specified — prioritise URLs that contain creator slug
                    if creator:
                        slug_lower = re.sub(r'[^a-z0-9]', '', creator.lower())
                        if slug_lower in u.lower():
                            clean.insert(0, u)
                        else:
                            clean.append(u)
                    else:
                        clean.append(u)
        if clean:
            break

    return clean[:10]


async def _verify_video_creator(url: str, creator: str) -> tuple[bool, str]:
    """Check if the video URL actually belongs to the requested creator.
    Returns (matches, found_creator_name)."""
    # Fast check: creator slug in URL
    slug = re.sub(r'[^a-z0-9]', '', creator.lower())
    if slug and slug in url.lower():
        extracted = re.search(r'tiktok\.com/@([^/\s?]+)', url)
        name = extracted.group(1) if extracted else creator
        return True, name

    # Slow check: scrape page for channel/author info
    page = await _fc_scrape(url)
    page_lower = page.lower()
    slug_lower = re.sub(r'[^a-z0-9]', '', creator.lower())

    # Look for author mentions in page content
    author_match = (
        re.search(r'@([a-zA-Z0-9_.]{2,32})', page) or
        re.search(r'(?:channel|author|by|creator)[:\s]+([a-zA-Z0-9_. -]{2,40})', page, re.IGNORECASE)
    )
    raw_name = author_match.group(1).strip() if author_match else ""
    found_name = re.sub(r'[^\w\s.@\-]', '', raw_name)[:80]
    found_slug = re.sub(r'[^a-z0-9]', '', found_name.lower())

    if slug_lower and (slug_lower in page_lower or found_slug == slug_lower):
        return True, found_name
    return False, found_name


async def _tool_search_video(
    query: str, description: str, creator: str, send_cb: Callable, status_cb: Callable
) -> str:
    """Autonomous video search with optional creator verification."""
    keys = await load_keys()
    max_rounds = 3
    tried_queries: list[str] = []

    async def _st(t: str):
        try: await status_cb(t)
        except Exception as e: logger.debug(f"status_cb suppressed: {e}")

    for rnd in range(max_rounds):
        if rnd == 0:
            current_query = query
        else:
            if keys:
                creator_hint = f" specifically from creator '{creator}'" if creator else ""
                payload = {
                    "contents": [{"parts": [{"text":
                        f"Give a better YouTube/TikTok search query to find: '{description or query}'{creator_hint}. "
                        f"Previous failed: {tried_queries}. Return ONLY the query string."
                    }]}],
                    "generationConfig": {"temperature": 0.5, "maxOutputTokens": 30},
                }
                url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.post(
                            url, json=payload,
                            headers={"Content-Type": "application/json", "x-goog-api-key": keys[0]},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                current_query = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                            else:
                                current_query = f"{query} {creator} official".strip()
                except Exception:
                    current_query = f"{query} {creator}".strip()
            else:
                current_query = f"{query} {creator}".strip()

        tried_queries.append(current_query)
        await _st(f"🔎 Ищу видео: «{current_query}» (попытка {rnd + 1}/{max_rounds})")

        video_urls = await _find_video_urls(current_query, creator=creator)
        if not video_urls:
            await _st("⚠️ Ссылок не нашёл, меняю запрос...")
            continue

        await _st(f"📥 Нашёл {len(video_urls)} ссылок, проверяю автора и скачиваю...")
        for vid_url in video_urls[:6]:
            # Verify creator if specified
            if creator:
                matches, found_name = await _verify_video_creator(vid_url, creator)
                if not matches:
                    logger.debug(f"Creator mismatch: wanted={creator!r} found={found_name!r} url={vid_url[:60]}")
                    await _st(f"⚠️ Видео не от {creator} (автор: {found_name or '?'}), пропускаю...")
                    continue
                await _st(f"✅ Автор подтверждён: {found_name}, скачиваю...")

            result = await _tool_download_video(vid_url, description or query, send_cb)
            if "sent" in result.lower() or "mb)" in result.lower():
                safe_url = vid_url.replace('\n', '').replace('\r', '').replace('\t', '')[:200]
                safe_creator = re.sub(r'[^\w\s.@\-]', '', creator)[:80]
                creator_info = f", автор: {safe_creator}" if safe_creator else ""
                return f"[ОТПРАВЛЕНО] Видео скачано и отправлено{creator_info}. Источник: {safe_url}"
            logger.debug(f"search_video dl failed: {vid_url!r} → {result[:80]}")

        await _st("⚠️ Ни одно видео не скачалось, ищу лучше...")

    return f"[НЕ НАЙДЕНО] Не удалось найти и скачать видео после {max_rounds} попыток. Пробовал: {tried_queries}. Скажи об этом пользователю честно."


async def _tool_tts(text: str, voice: str, lang: str, send_cb: Callable) -> str:
    audio, err = await generate_tts_with_gemini(
        text, model="gemini-3.5-flash-tts",
        voice_name=voice or "Kore", language_code=lang or "ru-RU",
    )
    if err or not audio:
        return f"TTS failed: {err or 'no audio'}"
    await send_cb({"type": "audio", "data": audio,
                   "caption": f"🎙 {text[:200]}", "filename": "speech.ogg"})
    return "Audio sent."




async def _tool_qr_code(text: str, caption: str, send_cb: Callable) -> str:
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        await send_cb({"type": "photo", "data": buf.getvalue(),
                       "caption": caption[:1024] or f"QR: {text[:200]}", "filename": "qr.png"})
        return "QR code sent."
    except Exception as e:
        return f"QR failed: {e}"


async def _tool_create_chart(
    chart_type: str, title: str, labels: list, values: list,
    xlabel: str, ylabel: str, send_cb: Callable,
) -> str:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.set_title(title or "Chart", fontsize=14, pad=12)
        ct = (chart_type or "bar").lower()
        if ct == "bar":
            ax.bar(range(len(values)), values, color="#4C9BE8")
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right")
        elif ct == "line":
            ax.plot(range(len(values)), values, marker="o", color="#4C9BE8", linewidth=2)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right")
        elif ct == "pie":
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
            ax.axis("equal")
        elif ct == "scatter":
            ax.scatter(range(len(values)), values, color="#4C9BE8", s=80)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right")
        else:
            ax.bar(range(len(values)), values)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right")
        if xlabel: ax.set_xlabel(xlabel)
        if ylabel: ax.set_ylabel(ylabel)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="PNG", dpi=150)
        plt.close(fig)
        await send_cb({"type": "photo", "data": buf.getvalue(),
                       "caption": f"📊 {title}", "filename": "chart.png"})
        return "Chart sent."
    except Exception as e:
        return f"Chart failed: {e}"


async def _tool_translate(text: str, target_lang: str) -> str:
    keys = await load_keys()
    if not keys:
        return "No Gemini keys."
    payload = {
        "contents": [{"parts": [{"text":
            f"Translate to {target_lang}. Return ONLY the translation:\n\n{text[:3000]}"
        }]}],
        "generationConfig": {"temperature": 0.1},
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload,
                             headers={"Content-Type": "application/json", "x-goog-api-key": keys[0]},
                             timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return f"Translate API error: {resp.status}"
    except Exception as e:
        return f"Translate failed: {e}"
