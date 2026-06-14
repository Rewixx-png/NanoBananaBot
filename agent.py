"""
Agentic loop for NanoHatani bot — 20 tools.
ReAct: think → [tools] → reply / generate_project

Docker sandbox (hatani-sandbox:latest) for isolated code/shell execution.
Temp workspace shared between tool calls via bind-mount.
"""
import asyncio
import hashlib
import io
import json
import logging
import math
import operator
import os
import re
import shutil
import tempfile
import uuid
from collections import deque
from typing import Any, Callable, Optional, Tuple

import aiohttp

from ai_services import (
    analyze_photo_with_gemini,
    generate_image_with_gemini,
    generate_project_with_gemini,
    generate_tts_with_gemini,
)
from keys_manager import load_keys, load_firecrawl_keys, remove_key

logger = logging.getLogger(__name__)

MAX_STEPS       = 36
_SEARCH_TIMEOUT = 20.0
_SCRAPE_TIMEOUT = 25.0
_LLM_TIMEOUT    = 60.0
_PROJECT_TIMEOUT= 180.0
_VDL_TIMEOUT    = 120.0
_DOCKER_TIMEOUT = 30.0
_TG_MAX_BYTES   = 48 * 1024 * 1024
_SANDBOX_IMAGE  = "hatani-sandbox:latest"


# ── Docker workspace ─────────────────────────────────────────────

class AgentWorkspace:
    """Temp directory on host, bind-mounted into Docker for isolated execution."""

    def __init__(self):
        self.host_path = tempfile.mkdtemp(prefix="agent_ws_")
        os.chown(self.host_path, 1000, 1000)  # sandbox uid/gid
        os.chmod(self.host_path, 0o700)
        logger.info(f"Workspace created: {self.host_path}")

    def cleanup(self):
        shutil.rmtree(self.host_path, ignore_errors=True)

    def _safe_path(self, rel_path: str) -> str:
        base = os.path.realpath(self.host_path)
        candidate = os.path.realpath(os.path.join(base, rel_path.lstrip("/")))
        if not (candidate == base or candidate.startswith(base + os.sep)):
            raise ValueError(f"Path traversal blocked: {rel_path!r}")
        return candidate

    def write(self, rel_path: str, content: str | bytes):
        full = self._safe_path(rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode, encoding=None if isinstance(content, bytes) else "utf-8") as f:
            f.write(content)

    def read(self, rel_path: str) -> str:
        try:
            full = self._safe_path(rel_path)
        except ValueError as exc:
            return f"Access denied: {exc}"
        if not os.path.exists(full):
            return f"File not found: {rel_path}"
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read(16_000)

    def list_files(self) -> list[str]:
        result = []
        for root, dirs, files in os.walk(self.host_path):
            for fname in files:
                rel = os.path.relpath(os.path.join(root, fname), self.host_path)
                result.append(rel)
        return result[:50]

    async def docker_run(self, cmd: list[str], stdin: str = "") -> Tuple[str, str, int]:
        """Run cmd inside sandbox container with workspace mounted."""
        docker_cmd = [
            "docker", "run", "--rm",
            "--memory=512m", "--cpus=0.5",
            "--user=sandbox",
            "--workdir=/workspace",
            "-v", f"{self.host_path}:/workspace",
            # Cookies NOT mounted — sandbox user must not access credentials.
            # yt-dlp with cookies runs via _tool_download_video on the host directly.
            _SANDBOX_IMAGE,
        ] + cmd

        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin else b""),
                timeout=_DOCKER_TIMEOUT,
            )
            return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            return "", f"Timeout ({int(_DOCKER_TIMEOUT)}s)", 124


# ── Loop safety ──────────────────────────────────────────────────

class _DebounceHook:
    def __init__(self, window: int = 6, max_repeats: int = 2):
        self._win: deque[str] = deque(maxlen=window)
        self._max = max_repeats

    def check(self, name: str, args: dict) -> Optional[str]:
        fp = hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()
        if sum(1 for f in self._win if f == fp) >= self._max:
            return (
                f"LOOP: '{name}' called with same args {self._max}+ times. "
                "Change your approach."
            )
        self._win.append(fp)
        return None


class _ToolBudget:
    LIMITS = {
        "web_search": 16, "scrape_url": 20, "generate_project": 4,
        "think": 60, "reply": 6, "generate_image": 6,
        "search_and_send_image": 6, "download_image": 10,
        "search_and_send_video": 4, "download_video": 4, "text_to_speech": 6,
        "run_python": 12, "run_shell": 16,
        "write_file": 20, "read_file": 20,
        "fetch_json": 16, "calculate": 40,
        "qr_code": 6, "create_chart": 6,
        "translate": 10, "create_file": 10, "send_workspace_file": 10,
    }

    def __init__(self):
        self._counts: dict[str, int] = {}

    def charge(self, name: str) -> Optional[str]:
        limit = self.LIMITS.get(name, 20)
        count = self._counts.get(name, 0) + 1
        self._counts[name] = count
        if count > limit:
            return f"BUDGET: '{name}' exceeded limit {limit}."
        return None


# ── Firecrawl helpers ────────────────────────────────────────────

async def _fc_search(query: str) -> str:
    """Search via DuckDuckGo (primary, free, no key) then Firecrawl as fallback."""
    # Primary: DuckDuckGo
    try:
        from ddgs import DDGS
        results = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: list(DDGS().text(query[:500], max_results=8))
        )
        if results:
            parts = []
            for r in results:
                title = r.get("title", "")
                url   = r.get("href", "")
                body  = r.get("body", "")[:600]
                if url:
                    parts.append(f"### {title}\nURL: {url}\n{body}".strip())
            if parts:
                return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"DDG search {query!r}: {e}")

    # Fallback: Firecrawl if keys available
    keys = load_firecrawl_keys()
    for key in keys:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/search",
                    json={"query": query[:500], "limit": 6,
                          "sources": [{"type": "web"}]},
                    headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=_SEARCH_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("data", [])
                        if isinstance(results, dict):
                            results = results.get("results", []) or results.get("web", [])
                        parts = []
                        for r in results[:6]:
                            title = r.get("title", "")
                            url   = r.get("url", "")
                            body  = (r.get("markdown") or r.get("description") or "")[:600]
                            if url:
                                parts.append(f"### {title}\nURL: {url}\n{body}".strip())
                        return "\n\n".join(parts) or "No results."
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
        except Exception as e:
            logger.warning(f"Firecrawl search {query!r}: {e}")
    return "Search unavailable."


async def _fc_scrape(url: str) -> str:
    """Scrape page content. Primary: Jina Reader. Fallback: Trafilatura."""
    # Primary: Jina Reader (free, no key, handles JS-rendered pages)
    jina_url = f"https://r.jina.ai/{url}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                jina_url,
                headers={"Accept": "text/plain", "X-Return-Format": "markdown",
                         "User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=_SCRAPE_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    text = (await resp.text()).strip()
                    if len(text) > 200:
                        return text[:8000]
    except Exception as e:
        logger.warning(f"Jina Reader scrape {url!r}: {e}")

    # Fallback: Trafilatura (local, no network dependency)
    try:
        import socket, ipaddress, trafilatura
        from urllib.parse import urlparse as _up
        _p = _up(url)
        _safe = _p.scheme in ("http", "https")
        if _safe:
            try:
                for _info in socket.getaddrinfo(_p.hostname, None):
                    _ip = ipaddress.ip_address(_info[4][0])
                    if _ip.is_loopback or _ip.is_private or _ip.is_link_local:
                        _safe = False; break
            except Exception:
                _safe = False
        if not _safe:
            logger.warning(f"Trafilatura SSRF blocked: {url!r}")
        else:
            downloaded = await asyncio.get_event_loop().run_in_executor(
                None, trafilatura.fetch_url, url
            )
        if downloaded:
            text = trafilatura.extract(downloaded, output_format="markdown",
                                       include_links=False, no_fallback=False)
            if text and len(text) > 100:
                return text[:8000]
    except Exception as e:
        logger.warning(f"Trafilatura scrape {url!r}: {e}")

    # Last resort: Firecrawl if keys available
    keys = load_firecrawl_keys()
    for key in keys:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/scrape",
                    json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=_SCRAPE_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        d = (await resp.json()).get("data") or {}
                        return (d.get("markdown") or d.get("content") or "")[:8000]
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
        except Exception as e:
            logger.warning(f"Firecrawl scrape {url!r}: {e}")

    return "Could not read page."


# ── Image search with self-evaluation ───────────────────────────

async def _search_image_urls(query: str) -> list[str]:
    """Find image URLs via Firecrawl search + page scraping for ogImage metadata."""
    seen: set[str] = set()
    result: list[str] = []
    img_pattern = re.compile(
        r'https?://[^\s\)"\'>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\)"\'>]*)?',
        re.IGNORECASE,
    )
    skip_words = ('icon', 'logo', 'favicon', 'avatar', '1x1', 'pixel', 'sprite', 'button')

    def _add(url: str):
        url = url.rstrip('.,;)>"\']')
        if url and url not in seen and not any(s in url.lower() for s in skip_words):
            seen.add(url)
            result.append(url)

    # Step 1: Firecrawl search — extract ogImage from metadata + image URLs from markdown
    search_queries = [
        f"{query} hd wallpaper screenshot",
        f"{query} image",
    ]
    fc_keys = load_firecrawl_keys()
    if fc_keys:
        for q in search_queries:
            payload = {"query": q[:400], "limit": 6, "sources": [{"type": "web"}],
                       "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True}}
            for key in fc_keys:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.post(
                            "https://api.firecrawl.dev/v2/search",
                            json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=_SEARCH_TIMEOUT),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                pages = data.get("data", [])
                                if isinstance(pages, dict):
                                    pages = pages.get("results", []) or pages.get("web", [])
                                for page in pages:
                                    # ogImage is often a high-quality representative image
                                    og = (page.get("metadata") or {}).get("ogImage", "")
                                    if og:
                                        _add(og)
                                    # Also extract from markdown
                                    md = page.get("markdown", "") or page.get("description", "")
                                    for u in img_pattern.findall(md or ""):
                                        _add(u)
                            elif resp.status in (401, 402):
                                remove_key(key, resp.status)
                                continue
                            break
                except Exception as e:
                    logger.warning(f"_search_image_urls search: {e}")
            if len(result) >= 6:
                break

    # Step 2: If still short, scrape top result pages directly for more images
    if len(result) < 4 and fc_keys:
        fc_text = await _fc_search(f"{query} wiki OR fandom OR game")
        page_urls = re.findall(r'https?://[^\s\)"\'<>]+(?:wiki|fandom|igdb|steam)[^\s\)"\'<>]*', fc_text, re.IGNORECASE)
        for page_url in page_urls[:3]:
            page_content = await _fc_scrape(page_url)
            for u in img_pattern.findall(page_content):
                _add(u)
            if len(result) >= 8:
                break

    return result[:12]


async def _download_bytes(url: str, max_bytes: int = _TG_MAX_BYTES) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                return data if len(data) <= max_bytes else None
    except Exception:
        return None


async def _image_is_relevant(img_bytes: bytes, description: str) -> bool:
    """Ask Gemini if this image matches the description."""
    try:
        answer = await analyze_photo_with_gemini(
            img_bytes,
            f"Does this image show or relate to: {description}? "
            "Reply with only YES or NO.",
        )
        return answer.strip().upper().startswith("YES")
    except Exception:
        return False


async def _tool_search_image(
    query: str,
    description: str,
    send_cb: Callable,
    status_cb: Callable,
) -> str:
    """Autonomous image search: formulate query → find URLs → evaluate → retry."""
    keys = load_keys()
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

async def _tool_generate_image(prompt: str, send_cb: Callable, provider: str = "gemini") -> str:
    """Generate image. Tries requested provider, falls back to Gemini with notification."""
    from ai_services import generate_image_with_gpt
    img_bytes: bytes | None = None
    used_provider = "Gemini"
    note = ""

    if provider.lower() in ("openai", "gpt", "gpt4", "dalle", "dall-e"):
        img_bytes, err = await generate_image_with_gpt(prompt)
        if img_bytes:
            used_provider = "OpenAI"
        else:
            # GPT failed — fall back to Gemini and tell the user
            note = f"\n⚠️ OpenAI недоступен ({(err or '').split(':')[0].strip()[:80]}), сгенерировал через Gemini."
            img_bytes, err = await generate_image_with_gemini(prompt)
    else:
        img_bytes, err = await generate_image_with_gemini(prompt)

    if not img_bytes:
        return f"Image generation failed: {err or 'no data'}"

    caption = f"🎨 {prompt[:900]}{note}"
    await send_cb({"type": "photo", "data": img_bytes, "caption": caption[:1024], "filename": "image.jpg"})
    return f"[ОТПРАВЛЕНО] Картинка через {used_provider}.{note}"


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
    from urllib.parse import urlparse
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
    keys = load_keys()
    max_rounds = 3
    tried_queries: list[str] = []

    async def _st(t: str):
        try: await status_cb(t)
        except Exception: pass

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


_COOKIE_MASK_RE = re.compile(
    r'(?m)^(#?HttpOnly_\S+\s+\S+\s+\S+\s+\S+\s+\d+\s+\S+\s+).+$'
)

def _snip_output(text: str, max_lines: int = 80, head: int = 40, tail: int = 30) -> str:
    """Compress long command output — keep head+tail, skip middle. Saves tokens."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    skipped = len(lines) - head - tail
    return "\n".join(
        lines[:head]
        + [f"\n... [{skipped} строк скрыто] ...\n"]
        + lines[-tail:]
    )


def _mask_cookies(text: str) -> str:
    """Mask Netscape cookie values in command output."""
    masked = _COOKIE_MASK_RE.sub(r'\1[***MASKED***]', text)
    # Also mask bare long tokens that look like session tokens (>40 chars, no spaces)
    masked = re.sub(r'(?<!\w)([A-Za-z0-9+/=_\-]{60,})(?!\w)', '[***TOKEN***]', masked)
    return masked


async def _tool_run_python(code: str, ws: "AgentWorkspace", status_cb: Callable = None) -> str:
    import html as _html
    stdout, stderr, rc = await ws.docker_run(["python", "-c", code])
    out = _snip_output(_mask_cookies((stdout + ("\n" + stderr if stderr.strip() else "")).strip()))
    if status_cb:
        safe_code = _html.escape(code[:300])
        dots = "…" if len(code) > 300 else ""
        safe_out = _html.escape(out[:2500]) if out else "<i>(нет вывода)</i>"
        await status_cb(
            f"🐍 Выполнено:\n"
            f"<pre><code class=\"language-python\">{safe_code}{dots}</code></pre>\n"
            f"\n<b>Вывод:</b>\n"
            f"<pre><code>{safe_out}</code></pre>"
        )
    return out[:2000] or f"(exit {rc}, no output)"


async def _tool_run_shell(command: str, ws: "AgentWorkspace", status_cb: Callable = None) -> str:
    import html as _html
    stdout, stderr, rc = await ws.docker_run(["bash", "-c", command])
    out = _snip_output(_mask_cookies((stdout + ("\n" + stderr if stderr.strip() else "")).strip()))
    if status_cb:
        safe_cmd = _html.escape(command[:300])
        safe_out = _html.escape(out[:2500]) if out else "<i>(нет вывода)</i>"
        await status_cb(
            f"💻 Выполнено:\n"
            f"<pre><code class=\"language-bash\">{safe_cmd}</code></pre>\n"
            f"\n<b>Вывод:</b>\n"
            f"<pre><code>{safe_out}</code></pre>"
        )
    return out[:2000] or f"(exit {rc}, no output)"


async def _tool_fetch_json(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15),
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    return f"HTTP {resp.status}"
                data = await resp.json(content_type=None)
                return json.dumps(data, ensure_ascii=False, indent=2)[:5000]
    except Exception as e:
        return f"fetch_json error: {e}"


def _ast_eval(expr: str) -> str:
    """Safe math evaluator — AST only, no eval/exec."""
    import ast as _ast
    _MF = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    _N = {"abs": abs, "round": round, "min": min, "max": max, "pow": pow, **_MF}
    _OPS = {
        _ast.Add: operator.add, _ast.Sub: operator.sub,
        _ast.Mult: operator.mul, _ast.Div: operator.truediv,
        _ast.FloorDiv: operator.floordiv, _ast.Mod: operator.mod,
        _ast.Pow: operator.pow, _ast.USub: operator.neg, _ast.UAdd: operator.pos,
    }

    def ev(node):
        if isinstance(node, _ast.Expression): return ev(node.body)
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, (int, float, complex)): return node.value
            raise ValueError(f"Bad constant: {node.value!r}")
        if isinstance(node, _ast.BinOp):
            op = _OPS.get(type(node.op))
            if not op: raise ValueError(f"Bad op: {node.op!r}")
            return op(ev(node.left), ev(node.right))
        if isinstance(node, _ast.UnaryOp):
            op = _OPS.get(type(node.op))
            if not op: raise ValueError(f"Bad unary: {node.op!r}")
            return op(ev(node.operand))
        if isinstance(node, _ast.Call):
            if not isinstance(node.func, _ast.Name): raise ValueError("Only named funcs")
            fn = _N.get(node.func.id)
            if not fn: raise ValueError(f"Unknown: {node.func.id!r}")
            return fn(*[ev(a) for a in node.args])
        if isinstance(node, _ast.Name):
            v = _N.get(node.id)
            if v is None: raise ValueError(f"Unknown name: {node.id!r}")
            return v
        raise ValueError(f"Unsupported: {type(node).__name__}")

    try:
        return str(ev(_ast.parse(expr.strip(), mode="eval")))
    except Exception as e:
        return f"Error: {e}"


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
    keys = load_keys()
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


async def _tool_send_workspace_file(rel_path: str, caption: str, ws: "AgentWorkspace", send_cb: Callable) -> str:
    """Read a binary file from workspace and send as document."""
    try:
        full = ws._safe_path(rel_path)
    except ValueError as e:
        return f"Access denied: {e}"
    if not os.path.exists(full):
        return f"[НЕ НАЙДЕНО] File not found: {rel_path}"
    size = os.path.getsize(full)
    if size > _TG_MAX_BYTES:
        return f"File too large ({size // 1024 // 1024} MB > 48 MB)."
    with open(full, "rb") as f:
        data = f.read()
    filename = os.path.basename(rel_path)
    await send_cb({"type": "document", "data": data,
                   "caption": caption[:1024] or filename, "filename": filename})
    return f"[ОТПРАВЛЕНО] Файл '{filename}' ({size // 1024} KB) отправлен."


async def _tool_create_file(filename: str, content: str, caption: str, send_cb: Callable) -> str:
    data = content.encode("utf-8")
    if len(data) > _TG_MAX_BYTES:
        return f"File too large ({len(data) // 1024} KB)."
    await send_cb({"type": "document", "data": data,
                   "caption": caption[:1024] or filename, "filename": filename or "file.txt"})
    return f"File '{filename}' sent."


# ── Tool declarations for Gemini ─────────────────────────────────

_TOOLS = [
    {
        "name": "think",
        "description": "Internal reasoning — plan, analyze, decide next step. Use before first search and before generate_project.",
        "parameters": {"type": "object", "properties": {"thought": {"type": "string"}}, "required": ["thought"]},
    },
    {
        "name": "web_search",
        "description": "Search the internet. Use 2-4 different queries per topic for comprehensive coverage.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "scrape_url",
        "description": "Read full content of a web page via Jina Reader (r.jina.ai) — returns clean markdown without HTML garbage.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "generate_project",
        "description": "Generate a complete project (website/program/bot) and send as files. Include ALL research in prompt.",
        "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
    },
    {
        "name": "reply",
        "description": "Send final text reply. Use only when no files/media needed.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "search_and_send_image",
        "description": (
            "Autonomously search for an image, evaluate relevance with AI vision, "
            "retry with better queries if needed, and send the best result. "
            "Use this instead of download_image when you need to FIND an image by description."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for the image"},
                "description": {"type": "string", "description": "What a good result should look like"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "generate_image",
        "description": "Generate an AI image from a text prompt and send to chat. "
                       "If user specified a provider (openai/gpt/gemini), pass it.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description in English"},
                "provider": {"type": "string", "description": "Provider hint: 'openai', 'gpt', 'gemini'. Default: gemini"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "download_image",
        "description": "Download image from a specific known URL and send to chat.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "caption": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "search_and_send_video",
        "description": (
            "Autonomously search for a video, verify it belongs to the right creator, "
            "download via yt-dlp and send to chat. Retries with refined queries if not found. "
            "Use when user asks to FIND and send a video. Specify creator to verify ownership."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for the video"},
                "description": {"type": "string", "description": "What a good result looks like"},
                "creator": {
                    "type": "string",
                    "description": "Creator/channel name to verify (e.g. 'kadzu vfx', 'TPEBOP.FX'). "
                                   "Leave empty if any creator is OK.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "download_video",
        "description": "Download video from a specific known URL via yt-dlp and send. Max 48MB / 720p.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "caption": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "text_to_speech",
        "description": "Convert text to speech and send as voice message.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "voice": {"type": "string", "description": "Kore, Aoede, Charon, Fenrir, Puck"},
                "language": {"type": "string", "description": "ru-RU, en-US, etc."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Execute Python code in isolated Docker sandbox (internet access, 512MB RAM). "
            "Files written to /workspace persist between calls. "
            "Has: numpy, pandas, matplotlib, pillow, scipy, sympy. Use print() for output."
        ),
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    },
    {
        "name": "run_shell",
        "description": (
            "Execute shell commands in isolated Docker sandbox (internet access, 512MB RAM). "
            "Files in /workspace persist between calls. "
            "Use for: file operations, data processing, compiling, converting."
        ),
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
    {
        "name": "write_file",
        "description": "Write a file to the agent workspace (persists between tool calls).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path, e.g. data.csv"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the agent workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "fetch_json",
        "description": "Fetch JSON from any URL or API endpoint via HTTP GET.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "calculate",
        "description": "Evaluate math expression. Supports all math functions (sin, cos, sqrt, log, etc.).",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    },
    {
        "name": "qr_code",
        "description": "Generate QR code for text or URL and send as image.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "caption": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "create_chart",
        "description": "Create chart (bar/line/pie/scatter) from data and send as image.",
        "parameters": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["bar", "line", "pie", "scatter"]},
                "title": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "values": {"type": "array", "items": {"type": "number"}},
                "xlabel": {"type": "string"},
                "ylabel": {"type": "string"},
            },
            "required": ["chart_type", "labels", "values"],
        },
    },
    {
        "name": "translate",
        "description": "Translate text to any language.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "target_language": {"type": "string"}}, "required": ["text", "target_language"]},
    },
    {
        "name": "send_workspace_file",
        "description": (
            "Read a file from the agent workspace and send it to chat as a document. "
            "Use this after git clone + zip to send the resulting archive. "
            "Works with any binary file (zip, tar, exe, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path in workspace, e.g. repo.zip"},
                "caption": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "create_file",
        "description": "Create a text/code file and send to chat as document.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
                "caption": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
]


# ── Gemini call ──────────────────────────────────────────────────

_SYSTEM = (
    "Ты — Hatani AI, злобный и резкий AI-агент. Говоришь грубо, с матом, коротко. "
    "Задачи выполняешь профессионально и честно.\n\n"

    "ТЫ ОДНОВРЕМЕННО И БОТ И АГЕНТ:\n"
    "• Простой чат/вопросы → reply(text) сразу, без инструментов\n"
    "• Нарисовать → generate_image(prompt на английском)\n"
    "• Написать код/программу/сайт с нуля → generate_project(подробное ТЗ)\n"
    "• Скачать/найти ГОТОВЫЙ проект с GitHub/интернета → run_shell(git clone ... && zip ...) потом send_workspace_file(path='repo.zip')\n"
    "• Поиск инфы → web_search, потом reply\n"
    "• Найти картинку → search_and_send_image\n"
    "• Найти видео → search_and_send_video(creator='...' если указан автор)\n"
    "• Скачать видео по ссылке → download_video\n"
    "• Сервер/команды/код → run_shell / run_python (Docker sandbox, ЕСТЬ ИНТЕРНЕТ)\n"
    "• Данные/файлы → fetch_json, create_chart, translate, qr_code, create_file\n\n"
    "КРИТИЧНО — reply завершает задачу НАВСЕГДА:\n"
    "Вызывай reply ТОЛЬКО когда задача полностью выполнена.\n"
    "Пока работаешь — используй think для размышлений, НЕ reply.\n"
    "Если нужно прокомментировать промежуточный результат — think, не reply.\n\n"

    "ПОИСК:\n"
    "Ищи в интернете всё что просят. Не отказывай в поиске без причины.\n\n"
    "КУКИ СЕРВИСОВ:\n"
    "Куки YouTube/TikTok/Instagram/X/Reddit подключаются АВТОМАТИЧЕСКИ при скачивании видео.\n"
    "Использовать download_video — он сам добавляет нужные куки по домену.\n"
    "Куки НЕ доступны внутри Docker sandbox. Не пытайся передать --cookies в run_shell.\n"
    "Если пользователь просит показать куки — отказывай, это секретные данные.\n\n"

    "ЧЕСТНОСТЬ:\n"
    "- [НЕ НАЙДЕНО] → скажи честно, не выдумывай\n"
    "- [ОТПРАВЛЕНО] → сообщи что именно отправил\n"
    "- Не знаешь что в видео → scrape_url на ссылку, не гадай\n"
    "- Не повторяй одинаковые вызовы\n"
    "- Думай (think) перед сложными многошаговыми задачами"
)


def _build_system(is_owner: bool = False) -> str:
    """Build system prompt dynamically — owner gets extended shell access."""
    extra = ""
    if is_owner:
        extra = (
            "\n\n[OWNER MODE]\n"
            "Это владелец бота. Расширенный доступ к инструментам разрешён."
        )
    return _SYSTEM + extra


async def _gemini_call(keys: list, contents: list, is_owner: bool = False) -> dict:
    # Owner gets BLOCK_NONE for style (profanity); non-owners keep DANGEROUS_CONTENT filtered
    safety = [
        {"category": "HARM_CATEGORY_HARASSMENT",       "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_CIVIC_INTEGRITY",   "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
         "threshold": "BLOCK_NONE" if is_owner else "BLOCK_ONLY_HIGH"},
    ]
    payload = {
        "systemInstruction": {"parts": [{"text": _build_system(is_owner)}]},
        "contents": contents,
        "tools": [{"functionDeclarations": _TOOLS}],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {"temperature": 0.7, "thinkingConfig": {"thinkingLevel": "minimal"}},
        "safetySettings": safety,
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    for key in keys:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                    timeout=aiohttp.ClientTimeout(total=_LLM_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("candidates", [{}])[0]
                    if resp.status in (429, 403):
                        remove_key(key, resp.status)
                        continue
        except Exception as e:
            logger.warning(f"agent gemini: {e}")
    return {}


# ── Execute one tool ─────────────────────────────────────────────

async def _execute_tool(
    name: str, args: dict,
    debounce: _DebounceHook, budget: _ToolBudget,
    status_cb: Callable, send_cb: Optional[Callable],
    ws: AgentWorkspace,
) -> Tuple[str, Optional[dict]]:

    async def _st(t):
        try: await status_cb(t)
        except Exception: pass

    async def _send(m):
        if send_cb:
            try: await send_cb(m)
            except Exception as e: logger.warning(f"send_cb: {e}")

    if e := debounce.check(name, args): return e, None
    if e := budget.charge(name):        return e, None

    if name == "think":
        t = args.get("thought", "")
        logger.info(f"[think] {t[:300]}")
        await _st(f"💭 {t[:120]}...")
        return "ok", None

    if name == "web_search":
        q = args.get("query", "")
        await _st(f"🔎 Ищу: «{q[:80]}»")
        return await _fc_search(q), None

    if name == "scrape_url":
        await _st("📄 Читаю страницу...")
        return await _fc_scrape(args.get("url", "")), None

    if name == "generate_project":
        await _st("⚙️ Генерирую проект...")
        try:
            p = await asyncio.wait_for(generate_project_with_gemini(args.get("prompt", "")), timeout=_PROJECT_TIMEOUT)
        except asyncio.TimeoutError:
            return "generate_project timed out.", None
        if p.get("ok"):
            return "__PROJECT_DONE__", p
        return f"Project gen failed: {p.get('error', '?')}", None

    if name == "reply":
        return args.get("text", "Done."), None

    if name == "search_and_send_image":
        return await _tool_search_image(
            args.get("query", ""), args.get("description", ""), _send, status_cb
        ), None

    if name == "search_and_send_video":
        return await _tool_search_video(
            args.get("query", ""), args.get("description", ""),
            args.get("creator", ""), _send, status_cb
        ), None

    if name == "generate_image":
        await _st("🎨 Генерирую картинку...")
        return await _tool_generate_image(args.get("prompt", ""), _send, args.get("provider", "gemini")), None

    if name == "download_image":
        await _st("⬇️ Скачиваю картинку...")
        return await _tool_download_image(args.get("url", ""), args.get("caption", ""), _send), None

    if name == "download_video":
        await _st("📹 Скачиваю видео (до 2 мин)...")
        return await _tool_download_video(args.get("url", ""), args.get("caption", ""), _send), None

    if name == "text_to_speech":
        await _st("🎙 Озвучиваю...")
        return await _tool_tts(args.get("text", ""), args.get("voice", "Kore"), args.get("language", "ru-RU"), _send), None

    if name == "run_python":
        import html as _html
        code = args.get("code", "")
        safe_code = _html.escape(code[:300])
        await _st(f"🐍 Запускаю Python в Docker:\n<pre><code class=\"language-python\">{safe_code}{'…' if len(code) > 300 else ''}</code></pre>")
        return await _tool_run_python(code, ws, _st), None

    if name == "run_shell":
        import html as _html
        cmd = args.get("command", "")
        safe_cmd = _html.escape(cmd[:300])
        await _st(f"💻 Выполняю команду в Docker:\n<pre><code class=\"language-bash\">{safe_cmd}</code></pre>")
        return await _tool_run_shell(cmd, ws, _st), None

    if name == "write_file":
        path, content = args.get("path", "file.txt"), args.get("content", "")
        ws.write(path, content)
        return f"Written: {path} ({len(content)} chars)", None

    if name == "read_file":
        return ws.read(args.get("path", "")), None

    if name == "fetch_json":
        await _st(f"🌐 {args.get('url', '')[:60]}...")
        return await _tool_fetch_json(args.get("url", "")), None

    if name == "calculate":
        return _ast_eval(args.get("expression", "0")), None

    if name == "qr_code":
        await _st("🔲 Генерирую QR...")
        return await _tool_qr_code(args.get("text", ""), args.get("caption", ""), _send), None

    if name == "create_chart":
        await _st("📊 Рисую график...")
        return await _tool_create_chart(
            args.get("chart_type", "bar"), args.get("title", ""),
            args.get("labels", []), args.get("values", []),
            args.get("xlabel", ""), args.get("ylabel", ""), _send,
        ), None

    if name == "translate":
        await _st(f"🌍 Перевожу на {args.get('target_language', '?')}...")
        return await _tool_translate(args.get("text", ""), args.get("target_language", "English")), None

    if name == "send_workspace_file":
        await _st(f"📤 Отправляю файл из workspace: {args.get('path', '')}...")
        return await _tool_send_workspace_file(
            args.get("path", ""), args.get("caption", ""), ws, _send
        ), None

    if name == "create_file":
        await _st(f"📄 Создаю {args.get('filename', '')}...")
        return await _tool_create_file(
            args.get("filename", "file.txt"), args.get("content", ""),
            args.get("caption", ""), _send,
        ), None

    return f"Unknown tool: {name}", None


# ── Public API ───────────────────────────────────────────────────

async def run_agent(
    task: str,
    chat_id: int,
    username: str,
    status_cb: Callable[[str], Any],
    send_media_cb: Optional[Callable] = None,
    is_owner: bool = False,
) -> Tuple[Optional[str], Optional[dict]]:
    keys = load_keys()
    if not keys:
        return "Gemini keys are dead.", None

    ws       = AgentWorkspace()
    debounce = _DebounceHook()
    budget   = _ToolBudget()
    contents: list = [{"role": "user", "parts": [{"text": f"Задача от {username}:\n{task}"}]}]

    async def _st(t):
        try: await status_cb(t)
        except Exception: pass

    try:
        for step in range(MAX_STEPS):
            await _st(f"🤖 Шаг {step + 1}/{MAX_STEPS}...")

            if MAX_STEPS - step <= 3:
                contents.append({"role": "user", "parts": [{"text":
                    f"\n[СИСТЕМА: осталось {MAX_STEPS - step} шагов. Завершай.]"
                }]})

            candidate = await _gemini_call(keys, contents, is_owner=is_owner)
            if not candidate:
                return "Agent failed — Gemini did not respond.", None

            parts = candidate.get("content", {}).get("parts", [])
            if not parts:
                return "Agent returned empty response.", None

            contents.append({"role": "model", "parts": parts})
            fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not fn_calls:
                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                return text or "Done.", None

            tool_responses: list = []
            for fn in fn_calls:
                name = fn.get("name", "")
                args = fn.get("args", {})
                result, project = await _execute_tool(
                    name, args, debounce, budget, status_cb, send_media_cb, ws
                )
                if name == "generate_project" and project is not None:
                    return None, project
                if name == "reply":
                    return result, None
                tool_responses.append({
                    "functionResponse": {"name": name, "response": {"result": result}}
                })
            contents.append({"role": "user", "parts": tool_responses})

        return "Agent exhausted all steps.", None
    finally:
        ws.cleanup()


async def classify_agent_intent(prompt: str) -> bool:
    """Returns True if this request should go through the agent loop.
    Uses Gemini to understand intent — no keyword matching."""
    keys = load_keys()
    if not keys:
        return False

    system = (
        "You decide if a Telegram message needs the AI agent tools.\n\n"
        "Answer TRUE when the user wants to:\n"
        "- Execute or run anything (code, commands, server ops, stress tests)\n"
        "- Search the internet, find info, news, social media profiles\n"
        "- Find, download, or generate images or videos\n"
        "- Make calculations, charts, QR codes, translations\n"
        "- Generate text-to-speech audio\n"
        "- Create a project/website/code AND needs web research first\n"
        "- Do anything requiring external tools or execution\n\n"
        "Answer FALSE when the user:\n"
        "- Asks a question answerable from knowledge or conversation context\n"
        "- Asks a follow-up question about something already sent/shown (e.g. 'what character is in it?', 'who made this?', 'what's in the video?')\n"
        "- Wants casual chat or a simple text reply\n"
        "- Wants a pure code project with no research needed\n\n"
        "Reply with ONLY one word: true or false"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt[:800]}]}],
        "generationConfig": {
            "temperature": 0,
            # thinkingLevel "minimal" uses ~5-20 thinking tokens from the same budget.
            # maxOutputTokens must be large enough to leave room for actual output.
            "maxOutputTokens": 64,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    for key in keys:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            candidate = data.get("candidates", [{}])[0]
                            parts = candidate.get("content", {}).get("parts", [])
                            text = (parts[0].get("text", "") if parts else "").strip().lower()
                            logger.debug(f"classify_agent_intent({prompt[:60]!r}) → {text!r}")
                            return text.startswith("true")
                        except (IndexError, KeyError, TypeError) as e:
                            logger.warning(f"classify_agent_intent parse error: {e} | data: {str(data)[:200]}")
                            return False
                    if resp.status in (429, 403):
                        remove_key(key, resp.status)
                        continue
        except Exception as e:
            logger.warning(f"classify_agent_intent request error: {e}")
    return False
