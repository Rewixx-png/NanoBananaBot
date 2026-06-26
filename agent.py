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
from keys import load_keys, load_firecrawl_keys, remove_key
from keys import get_live_keys as _nk_get_live, mark_cooldown as _nk_cooldown, sync_from_keyhunter as _nk_sync, init_db as _nk_init

logger = logging.getLogger(__name__)

MAX_STEPS       = 60
_SEARCH_TIMEOUT = 20.0
_SCRAPE_TIMEOUT = 25.0
_LLM_TIMEOUT    = 60.0
_PROJECT_TIMEOUT= 180.0
_VDL_TIMEOUT    = 120.0
_DOCKER_TIMEOUT = 600.0
_TG_MAX_BYTES   = 48 * 1024 * 1024
_SANDBOX_IMAGE  = "hatani-sandbox:latest"


# ── Docker workspace ─────────────────────────────────────────────

class AgentWorkspace:
    """Temp directory on host, bind-mounted into Docker for isolated execution."""

    def __init__(self, existing_path: str = ""):
        if existing_path and os.path.isdir(existing_path):
            self.host_path = existing_path
            self._persistent = True
            logger.info(f"Workspace reused: {self.host_path}")
        else:
            project_dir = os.path.dirname(os.path.abspath(__file__))
            ws_dir = os.path.join(project_dir, ".agent_workspaces")
            os.makedirs(ws_dir, exist_ok=True)
            try:
                os.chown(ws_dir, 1000, 1000)
                os.chmod(ws_dir, 0o777)
            except Exception:
                pass
            self.host_path = tempfile.mkdtemp(prefix="agent_ws_", dir=ws_dir)
            try:
                os.chown(self.host_path, 1000, 1000)
                os.chmod(self.host_path, 0o777)
            except Exception:
                pass
            self._persistent = False
            logger.info(f"Workspace created: {self.host_path}")

    def cleanup(self):
        if not self._persistent:
            shutil.rmtree(self.host_path, ignore_errors=True)

    def preload(self, files: dict[str, bytes]):
        """Pre-populate workspace with files before agent starts."""
        for name, data in files.items():
            self.write(name, data)

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

    async def docker_run(
        self, cmd: list[str], stdin: str = "",
        output_cb: Optional[Callable] = None,
    ) -> Tuple[str, str, int]:
        """Run cmd inside sandbox container with workspace mounted."""
        def _fix_workspace_permissions():
            try:
                os.chown(self.host_path, 1000, 1000)
                os.chmod(self.host_path, 0o777)
                for root, dirs, files in os.walk(self.host_path):
                    for d in dirs:
                        path = os.path.join(root, d)
                        os.chown(path, 1000, 1000)
                        os.chmod(path, 0o777)
                    for f in files:
                        path = os.path.join(root, f)
                        os.chown(path, 1000, 1000)
                        os.chmod(path, 0o777)
            except Exception as e:
                logger.warning(f"Failed to fix workspace permissions recursive: {e}")

        await asyncio.to_thread(_fix_workspace_permissions)

        docker_cmd = [
            "docker", "run", "--rm",
            "--memory=1024m", "--cpus=2",
            "--user=sandbox",
            "--workdir=/workspace",
            "-v", f"{self.host_path}:/workspace",
            _SANDBOX_IMAGE,
        ] + cmd

        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        out_lines: list[str] = []
        err_lines: list[str] = []

        async def _read_stream(stream, buf: list):
            while True:
                line = await stream.readline()
                if not line:
                    break
                buf.append(line.decode(errors="replace"))

        async def _flush_loop():
            import time as _t
            last = _t.monotonic()
            while True:
                await asyncio.sleep(2)
                if output_cb:
                    try:
                        await output_cb("".join(out_lines + err_lines))
                    except Exception:
                        pass

        read_out = asyncio.create_task(_read_stream(proc.stdout, out_lines))
        read_err = asyncio.create_task(_read_stream(proc.stderr, err_lines))
        flush_task = asyncio.create_task(_flush_loop()) if output_cb else None

        try:
            if stdin:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            await asyncio.wait_for(
                asyncio.gather(read_out, read_err, proc.wait()),
                timeout=_DOCKER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.gather(read_out, read_err, return_exceptions=True)
            if flush_task:
                flush_task.cancel()
            return "".join(out_lines), "".join(err_lines) + f"\nTimeout ({int(_DOCKER_TIMEOUT)}s)", 124
        finally:
            if flush_task:
                flush_task.cancel()

        return "".join(out_lines), "".join(err_lines), proc.returncode


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
        "think": 30, "reply": 6, "generate_image": 6,
        "search_and_send_image": 6, "download_image": 10,
        "search_and_send_video": 4, "download_video": 4, "text_to_speech": 6,
        "run_python": 12, "run_shell": 16,
        "write_file": 20, "read_file": 20,
        "fetch_json": 16, "calculate": 40,
        "qr_code": 6, "create_chart": 6,
        "translate": 10, "create_file": 10, "send_workspace_file": 10, "send_with_buttons": 5,
        "tg_send_poll": 3, "tg_send_location": 5, "tg_react": 10, "tg_pin_message": 3,
        "tg_delete_message": 10, "tg_forward_message": 5, "tg_get_chat_info": 5,
        "tg_ban_user": 3, "tg_kick_user": 3, "tg_send_chat_action": 10,
        "tg_restrict_member": 3, "tg_unpin_message": 5, "tg_create_invite_link": 3,
        "tg_set_chat_title": 2, "tg_copy_message": 10, "tg_send_sticker": 5,
        "tg_send_contact": 5, "tg_send_dice": 5, "tg_edit_message": 10,
        "fetch_tiktok_profile": 5, "fetch_with_cookies": 8,
        "tg_set_chat_photo": 2, "tg_send_animation": 5, "tg_send_video_note": 3, "tg_send_venue": 5,
        "tg_promote_member": 2, "tg_get_chat_member": 10, "tg_get_admins": 5,
        "tg_get_member_count": 10, "tg_create_forum_topic": 3, "tg_close_forum_topic": 3,
        "tg_get_sticker_set": 5, "tg_approve_join_request": 5, "tg_export_invite_link": 3,
        "read_bot_logs": 5,
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
    keys = await load_firecrawl_keys()
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
    keys = await load_firecrawl_keys()
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
    fc_keys = await load_firecrawl_keys()
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
    from ai_services import generate_image_with_gpt
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
    from keys import load_openai_keys
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
    keys = await load_keys()
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
    import html as _html, time as _t
    safe_code = _html.escape(code[:1500])
    dots = "…" if len(code) > 1500 else ""

    start_ts = _t.monotonic()

    async def _live_cb(raw: str):
        elapsed = int(_t.monotonic() - start_ts)
        m, s = divmod(elapsed, 60)
        t = f"{m}м {s}с" if m else f"{s}с"
        live = _html.escape(_snip_output(_mask_cookies(raw.strip()))[:1800]) or "<i>...</i>"
        if status_cb:
            try:
                await status_cb(
                    f"⏳ Запускаю Python · {t}\n"
                    f"<pre><code class=\"language-python\">{safe_code}{dots}</code></pre>\n"
                    f"<pre><code>{live}</code></pre>"
                )
            except Exception:
                pass

    stdout, stderr, rc = await ws.docker_run(["python", "-c", code], output_cb=_live_cb)
    out = _snip_output(_mask_cookies((stdout + ("\n" + stderr if stderr.strip() else "")).strip()))
    if status_cb:
        safe_out = _html.escape(out[:2300]) if out else "<i>(нет вывода)</i>"
        await status_cb(
            f"🐍 Выполнено:\n"
            f"<pre><code class=\"language-python\">{safe_code}{dots}</code></pre>\n"
            f"\n<b>Вывод:</b>\n"
            f"<pre><code>{safe_out}</code></pre>"
        )
    return out[:2000] or f"(exit {rc}, no output)"


async def _tool_run_shell(command: str, ws: "AgentWorkspace", status_cb: Callable = None) -> str:
    import html as _html, time as _t
    safe_cmd = _html.escape(command[:1500])
    dots = "…" if len(command) > 1500 else ""

    start_ts = _t.monotonic()

    async def _live_cb(raw: str):
        elapsed = int(_t.monotonic() - start_ts)
        m, s = divmod(elapsed, 60)
        t = f"{m}м {s}с" if m else f"{s}с"
        live = _html.escape(_snip_output(_mask_cookies(raw.strip()))[:1800]) or "<i>...</i>"
        if status_cb:
            try:
                await status_cb(
                    f"⏳ Выполняю · {t}\n"
                    f"<pre><code class=\"language-bash\">{safe_cmd}{dots}</code></pre>\n"
                    f"<pre><code>{live}</code></pre>"
                )
            except Exception:
                pass

    stdout, stderr, rc = await ws.docker_run(["bash", "-c", command], output_cb=_live_cb)
    out = _snip_output(_mask_cookies((stdout + ("\n" + stderr if stderr.strip() else "")).strip()))
    if status_cb:
        safe_out = _html.escape(out[:2300]) if out else "<i>(нет вывода)</i>"
        await status_cb(
            f"💻 Выполнено:\n"
            f"<pre><code class=\"language-bash\">{safe_cmd}{dots}</code></pre>\n"
            f"\n<b>Вывод:</b>\n"
            f"<pre><code>{safe_out}</code></pre>"
        )
    return out[:2000] or f"(exit {rc}, no output)"


async def _tool_analyze_image(
    path: str,
    question: str,
    ws: "AgentWorkspace",
) -> str:
    """Send workspace image to Gemini vision and return its response."""
    import base64, mimetypes
    safe_path = os.path.join(ws.host_path, os.path.basename(path))
    if not os.path.exists(safe_path):
        return f"Файл не найден: {path}"
    with open(safe_path, "rb") as f:
        img_bytes = f.read()
    if len(img_bytes) > 20 * 1024 * 1024:
        return "Файл слишком большой (>20MB) для анализа."
    mime = mimetypes.guess_type(safe_path)[0] or "image/jpeg"
    b64 = base64.b64encode(img_bytes).decode()
    keys = await load_keys()
    if not keys:
        return "Нет Gemini ключей."
    payload = {
        "contents": [{"parts": [
            {"text": question or "Подробно опиши что на этом изображении."},
            {"inlineData": {"mimeType": mime, "data": b64}},
        ]}],
        "generationConfig": {"temperature": 0.4, "mediaResolution": "MEDIA_RESOLUTION_HIGH"},
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    for key in keys:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        _cands = data.get("candidates", [])
                        parts = (_cands[0].get("content", {}).get("parts", []) if _cands else [])
                        return " ".join(p.get("text", "") for p in parts).strip() or "Нет ответа."
                    if resp.status in (429, 403):
                        remove_key(key, resp.status)
                        continue
        except Exception as e:
            logger.warning(f"analyze_image: {e}")
    return "Gemini не ответил."


async def _tool_analyze_audio(
    path: str,
    question: str,
    ws: "AgentWorkspace",
) -> str:
    """Send workspace audio to Gemini for quality analysis."""
    import base64, mimetypes
    safe_path = os.path.join(ws.host_path, os.path.basename(path))
    if not os.path.exists(safe_path):
        return f"Файл не найден: {path}"
    with open(safe_path, "rb") as f:
        audio_bytes = f.read()
    if len(audio_bytes) > 20 * 1024 * 1024:
        return "Файл слишком большой (>20MB) для анализа."
    ext = os.path.splitext(safe_path)[1].lower().lstrip(".")
    mime_map = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
                "flac": "audio/flac", "m4a": "audio/mp4", "aac": "audio/aac"}
    mime = mime_map.get(ext, "audio/mpeg")
    b64 = base64.b64encode(audio_bytes).decode()
    keys = await _nk_get_live()
    if not keys:
        return "Нет ключей для анализа."
    payload = {
        "contents": [{"parts": [
            {"text": question or "Оцени качество этого аудио: звучание, баланс, артефакты, клиппинг. Дай конкретную оценку."},
            {"inlineData": {"mimeType": mime, "data": b64}},
        ]}],
        "generationConfig": {"temperature": 0.4},
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    for key in keys[:5]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        parts = (data.get("candidates", [{}])[0]
                                 .get("content", {}).get("parts", []))
                        return " ".join(p.get("text", "") for p in parts).strip() or "Нет ответа."
                    if resp.status in (429, 403):
                        await _nk_cooldown(key, resp.status)
        except Exception as e:
            logger.warning(f"analyze_audio: {type(e).__name__}: {e}")
    return "Gemini не ответил."


async def _tool_playwright_browse(
    url: str,
    action: str,
    selector: str = "",
    value: str = "",
    js_code: str = "",
    ws: "AgentWorkspace" = None,
    status_cb: Callable = None,
    send_cb: Callable = None,
) -> str:
    import json as _json
    import html as _html

    params_json = _json.dumps({
        "url": url, "action": action,
        "selector": selector, "value": value, "js_code": js_code,
    })
    script = f"""
import json, sys, os
params  = {params_json!r}
url     = params["url"]
action  = params["action"]
sel     = params["selector"]
val     = params["value"]
js_code = params["js_code"]

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path="/usr/bin/chromium",
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx  = browser.new_context(viewport={{"width": 1280, "height": 900}})
    page = ctx.new_page()
    page.set_default_timeout(20000)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        print(f"goto error: {{e}}", file=sys.stderr)

    if action == "screenshot":
        page.screenshot(path="/workspace/_pw_screen.png", full_page=True)
        print("SCREENSHOT_DONE")
    elif action == "scrape":
        if sel:
            els = page.query_selector_all(sel)
            print("\\n".join(e.inner_text() for e in els[:20]))
        else:
            print(page.inner_text("body")[:6000])
    elif action == "click":
        page.click(sel)
        page.screenshot(path="/workspace/_pw_screen.png")
        print("SCREENSHOT_DONE")
    elif action == "fill":
        page.fill(sel, val)
        print(f"Filled {{sel!r}} = {{val!r}}")
    elif action == "eval":
        print(str(page.evaluate(js_code))[:3000])
    else:
        print(f"Unknown action: {{action}}")
    browser.close()
"""
    ws.write("_pw_script.py", script)
    stdout, stderr, rc = await ws.docker_run(["python", "_pw_script.py"])
    out = (stdout + ("\n" + stderr if stderr.strip() else "")).strip()

    if "SCREENSHOT_DONE" in out and send_cb:
        screen_host = os.path.join(ws.host_path, "_pw_screen.png")
        if os.path.exists(screen_host):
            await send_cb({"type": "tg_send_photo", "path": screen_host,
                           "caption": f"📸 {url[:80]}"})
        out = f"Скриншот отправлен | {url}"

    if status_cb:
        safe = _html.escape(out[:2000]) if out else "<i>(нет вывода)</i>"
        await status_cb(
            f"🌐 Playwright [{_html.escape(action)}]: <code>{_html.escape(url[:100])}</code>\n"
            f"<pre><code>{safe}</code></pre>"
        )
    return _snip_output(out)[:2000] or f"(exit {rc}, no output)"


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


async def _tool_read_bot_logs(lines: int = 100) -> str:
    """Read the last N lines of the bot's own log file from the host."""
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    if not os.path.exists(log_path):
        return "bot.log not found."
    try:
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])[-3000:] or "(empty)"
    except Exception as e:
        return f"Error reading bot.log: {e}"


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
        "description": "Use before EVERY tool call. Write 1-2 sentences: what you will do next and why. User sees this as a thinking block.",
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
        "name": "send_with_buttons",
        "description": (
            "Send a message with inline URL buttons. Use when links look ugly in plain text, "
            "or to present multiple URLs as clickable buttons. "
            "Each inner list = one row of buttons. Max 8 per row."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text (HTML formatting supported)"},
                "buttons": {
                    "type": "array",
                    "description": "Rows of buttons: [[{text, url}, ...], ...]",
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "url":  {"type": "string"},
                            },
                            "required": ["text", "url"],
                        },
                    },
                },
            },
            "required": ["text", "buttons"],
        },
    },
    # ── Telegram API tools ──────────────────────────────────────────
    {
        "name": "tg_send_poll",
        "description": "Create an interactive poll in the chat.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "2-10 answer options"},
            "is_anonymous": {"type": "boolean", "description": "Anonymous poll (default true)"},
            "allows_multiple_answers": {"type": "boolean", "description": "Multiple choice (default false)"},
        }, "required": ["question", "options"]},
    },
    {
        "name": "tg_send_location",
        "description": "Send a GPS location to the chat.",
        "parameters": {"type": "object", "properties": {
            "latitude": {"type": "number"},
            "longitude": {"type": "number"},
            "title": {"type": "string", "description": "Optional venue title"},
            "address": {"type": "string", "description": "Optional venue address"},
        }, "required": ["latitude", "longitude"]},
    },
    {
        "name": "tg_react",
        "description": "Add an emoji reaction to the last message or a specific message_id.",
        "parameters": {"type": "object", "properties": {
            "emoji": {"type": "string", "description": "Emoji reaction e.g. 👍 ❤️ 🔥 🎉 💯 😂"},
            "message_id": {"type": "integer", "description": "Target message (omit for the user's last message)"},
        }, "required": ["emoji"]},
    },
    {
        "name": "tg_pin_message",
        "description": "Pin a message in the chat (requires admin rights).",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "integer", "description": "Message to pin (omit to pin the user's message)"},
            "disable_notification": {"type": "boolean"},
        }, "required": []},
    },
    {
        "name": "tg_delete_message",
        "description": "Delete a specific message (requires admin rights or own message).",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "integer"},
        }, "required": ["message_id"]},
    },
    {
        "name": "tg_forward_message",
        "description": "Forward a message to the current chat from another chat.",
        "parameters": {"type": "object", "properties": {
            "from_chat_id": {"type": "integer", "description": "Source chat ID"},
            "message_id": {"type": "integer"},
        }, "required": ["from_chat_id", "message_id"]},
    },
    {
        "name": "tg_get_chat_info",
        "description": "Get info about the current chat: title, description, member count, admin list.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "tg_ban_user",
        "description": "Ban a user from the chat (requires admin). Provide user_id or reply to their message.",
        "parameters": {"type": "object", "properties": {
            "user_id": {"type": "integer", "description": "User to ban"},
            "reason": {"type": "string"},
            "until_date": {"type": "integer", "description": "Unix timestamp when ban expires (omit for permanent)"},
        }, "required": ["user_id"]},
    },
    {
        "name": "tg_unban_user",
        "description": "Unban a user from the chat / remove from blacklist (requires admin).",
        "parameters": {"type": "object", "properties": {
            "user_id": {"type": "integer", "description": "User to unban"},
        }, "required": ["user_id"]},
    },
    {
        "name": "tg_kick_user",
        "description": "Kick (temporary ban 60s) a user from the chat (requires admin).",
        "parameters": {"type": "object", "properties": {
            "user_id": {"type": "integer"},
            "reason": {"type": "string"},
        }, "required": ["user_id"]},
    },
    {
        "name": "tg_send_chat_action",
        "description": "Show a typing/uploading status indicator in the chat.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["typing", "upload_photo", "upload_video",
                        "upload_document", "record_voice", "find_location"]},
        }, "required": ["action"]},
    },
    {
        "name": "tg_restrict_member",
        "description": "Restrict (mute) a user in the chat (requires admin). Set can_send_messages=false to mute.",
        "parameters": {"type": "object", "properties": {
            "user_id": {"type": "integer"},
            "can_send_messages": {"type": "boolean", "description": "False = muted"},
            "can_send_media": {"type": "boolean"},
            "until_date": {"type": "integer", "description": "Unix timestamp when restriction expires"},
        }, "required": ["user_id"]},
    },
    {
        "name": "tg_unpin_message",
        "description": "Unpin a message or all messages in the chat (requires admin).",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "integer", "description": "Omit to unpin all messages"},
        }, "required": []},
    },
    {
        "name": "tg_create_invite_link",
        "description": "Create a new invite link for the chat (requires admin).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Link name"},
            "expire_date": {"type": "integer", "description": "Expiry unix timestamp"},
            "member_limit": {"type": "integer", "description": "Max number of uses"},
        }, "required": []},
    },
    {
        "name": "tg_set_chat_title",
        "description": "Change the chat title (requires admin).",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
        }, "required": ["title"]},
    },
    {
        "name": "tg_copy_message",
        "description": "Copy a message to the current chat without the 'Forwarded from' label.",
        "parameters": {"type": "object", "properties": {
            "from_chat_id": {"type": "integer", "description": "Source chat (omit = current chat)"},
            "message_id": {"type": "integer"},
            "caption": {"type": "string"},
        }, "required": ["message_id"]},
    },
    {
        "name": "tg_send_sticker",
        "description": "Send a sticker by file_id or emoji.",
        "parameters": {"type": "object", "properties": {
            "sticker": {"type": "string", "description": "Sticker file_id or emoji"},
        }, "required": ["sticker"]},
    },
    {
        "name": "tg_send_contact",
        "description": "Send a phone contact to the chat.",
        "parameters": {"type": "object", "properties": {
            "phone": {"type": "string", "description": "Phone number e.g. +79001234567"},
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
        }, "required": ["phone", "first_name"]},
    },
    {
        "name": "tg_send_dice",
        "description": "Send an animated emoji with random result (dice, dart, basketball, etc.).",
        "parameters": {"type": "object", "properties": {
            "emoji": {"type": "string", "enum": ["🎲", "🎯", "🏀", "⚽", "🎳", "🎰"]},
        }, "required": []},
    },
    {
        "name": "tg_edit_message",
        "description": "Edit a previously sent message by the bot.",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "integer"},
            "text": {"type": "string", "description": "New message text (HTML supported)"},
        }, "required": ["message_id", "text"]},
    },
    # ── More Telegram API tools ──────────────────────────────────────
    {"name": "tg_send_animation", "description": "Send a GIF animation to the chat.",
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string", "description": "URL or file_id of the GIF"},
         "caption": {"type": "string"}}, "required": ["url"]}},
    {"name": "tg_send_video_note", "description": "Send a round video note (кружок) to the chat.",
     "parameters": {"type": "object", "properties": {
         "file_id": {"type": "string", "description": "file_id of an existing video note"}},
      "required": ["file_id"]}},
    {"name": "tg_send_venue", "description": "Send a venue (location with title and address).",
     "parameters": {"type": "object", "properties": {
         "latitude": {"type": "number"}, "longitude": {"type": "number"},
         "title": {"type": "string"}, "address": {"type": "string"},
         "foursquare_id": {"type": "string"}},
      "required": ["latitude", "longitude", "title", "address"]}},
    {"name": "tg_promote_member", "description": "Promote or demote a user to/from admin (requires admin).",
     "parameters": {"type": "object", "properties": {
         "user_id": {"type": "integer"},
         "can_delete_messages": {"type": "boolean"}, "can_pin_messages": {"type": "boolean"},
         "can_manage_chat": {"type": "boolean"}, "can_ban_members": {"type": "boolean"},
         "custom_title": {"type": "string", "description": "Admin title e.g. 'Редактор'"}},
      "required": ["user_id"]}},
    {"name": "tg_get_chat_member", "description": "Get information about a specific chat member.",
     "parameters": {"type": "object", "properties": {
         "user_id": {"type": "integer"}}, "required": ["user_id"]}},
    {"name": "tg_get_admins", "description": "Get list of all chat administrators.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "tg_get_member_count", "description": "Get total number of members in the chat.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "tg_create_forum_topic", "description": "Create a new forum topic in a forum group (requires admin).",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "icon_emoji": {"type": "string", "description": "Topic emoji icon"}},
      "required": ["name"]}},
    {"name": "tg_close_forum_topic", "description": "Close a forum topic (requires admin).",
     "parameters": {"type": "object", "properties": {
         "message_thread_id": {"type": "integer"}}, "required": ["message_thread_id"]}},
    {"name": "tg_get_sticker_set", "description": "Get info about a sticker set by name.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Sticker set name e.g. 'kirieshkikirieshki'"}},
      "required": ["name"]}},
    {"name": "tg_approve_join_request", "description": "Approve or decline a chat join request.",
     "parameters": {"type": "object", "properties": {
         "user_id": {"type": "integer"},
         "approve": {"type": "boolean", "description": "True to approve, False to decline"}},
      "required": ["user_id", "approve"]}},
    {"name": "tg_export_invite_link", "description": "Get the primary invite link for the chat.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "tg_set_bot_photo",
     "description": (
         "Change the bot's own profile photo. "
         "Pass workspace path of the image. "
         "Use when user says 'смени аву бота', 'поставь боту аватарку' etc."
     ),
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Workspace path e.g. photo.jpg"}},
      "required": ["path"]}},
    {"name": "tg_set_chat_description",
     "description": "Change the chat description/bio (requires admin).",
     "parameters": {"type": "object", "properties": {
         "description": {"type": "string", "description": "New description (up to 255 chars)"}},
      "required": ["description"]}},
    {"name": "tg_set_chat_photo",
     "description": (
         "Set or change the chat avatar/photo. "
         "Pass the workspace path of the image file the user attached. "
         "Requires admin rights. Use when user says 'поставь на аву', 'смени аватарку' etc."
     ),
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Path in workspace e.g. photo.jpg"}},
      "required": ["path"]}},
    {"name": "fetch_tiktok_profile",
     "description": (
         "Fetch REAL TikTok profile info using authenticated cookies on the server. "
         "Returns actual follower count, likes, bio, videos. "
         "Use this instead of web_search when user asks for TikTok profile data. "
         "Cookies are used automatically — no need to specify them."
     ),
     "parameters": {"type": "object", "properties": {
         "username": {"type": "string", "description": "TikTok username without @ e.g. 'verb.aep'"}},
      "required": ["username"]}},
    {"name": "fetch_with_cookies",
     "description": (
         "Fetch a URL using the server's authenticated cookies for that service. "
         "Supports: youtube.com, tiktok.com, instagram.com, x.com/twitter.com, reddit.com. "
         "Returns the page content/API response. Use for getting real authenticated data."
     ),
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string", "description": "Full URL to fetch"},
         "output_format": {"type": "string", "enum": ["text", "json"], "description": "Expected output format"}},
      "required": ["url"]}},
    # ── End Telegram API tools ───────────────────────────────────────
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
        "name": "list_image_models",
        "description": "Fetch available image-generation models from OpenAI API. Call this first if user wants to pick a specific GPT/DALL-E model.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "generate_image",
        "description": (
            "Generate an AI image from a text prompt and send to chat. "
            "provider='gemini' (default) or 'openai'. "
            "For OpenAI: use list_image_models to see available models, then pass the model name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt":   {"type": "string", "description": "Detailed image description in English"},
                "model":    {"type": "string", "description": "Exact model ID from list_image_models, e.g. 'dall-e-3'"},
                "provider": {"type": "string", "description": "'openai' or 'gemini'. Default: gemini"},
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
            "Execute Python code in isolated Docker sandbox (internet access, 1024MB RAM). "
            "Files written to /workspace persist between calls. "
            "Has: numpy, pandas, matplotlib, pillow, scipy, sympy. Use print() for output."
        ),
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    },
    {
        "name": "analyze_audio",
        "description": (
            "Send an audio file from workspace to Gemini for quality analysis. "
            "ALWAYS use after creating/processing audio to verify quality before sending to user. "
            "Checks for clipping, bad balance, artifacts, and whether the result matches the request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path":     {"type": "string", "description": "Relative path in workspace, e.g. 'output.mp3'"},
                "question": {"type": "string", "description": "What to check, e.g. 'Is bass balanced? Any clipping?'"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "analyze_image",
        "description": (
            "Send an image file from workspace to Gemini vision for analysis. "
            "Use when you need to understand image content, read text from it, compare visuals, etc. "
            "Much better than pytesseract for general image understanding."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path":     {"type": "string", "description": "Relative path in workspace, e.g. 'photo.jpg'"},
                "question": {"type": "string", "description": "What to ask about the image"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_shell",
        "description": (
            "Execute shell commands in isolated Docker sandbox (internet access, 1024MB RAM). "
            "Files in /workspace persist between calls. "
            "Use for: file operations, data processing, compiling, converting."
        ),
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
    {
        "name": "playwright_browse",
        "description": (
            "Control a real Chromium browser in Docker sandbox. "
            "Actions: 'screenshot' — take full-page screenshot and send to chat; "
            "'scrape' — extract text from page or CSS selector; "
            "'click' — click element by CSS selector, then screenshot; "
            "'fill' — fill input field (selector + value); "
            "'eval' — run JavaScript and return result. "
            "Use for JS-heavy sites, SPAs, login flows, visual page checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url":      {"type": "string", "description": "Full URL to open"},
                "action":   {"type": "string", "enum": ["screenshot", "scrape", "click", "fill", "eval"]},
                "selector": {"type": "string", "description": "CSS selector (for click/fill/scrape)"},
                "value":    {"type": "string", "description": "Text to fill (for fill action)"},
                "js_code":  {"type": "string", "description": "JS expression to evaluate (for eval action)"},
            },
            "required": ["url", "action"],
        },
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
        "name": "read_bot_logs",
        "description": "Read the last N lines of bot.log from host. Returns the actual log text — READ IT and report what you see. Do NOT say 'logs requested' — analyze and summarize what's in the returned content.",
        "parameters": {"type": "object", "properties": {"lines": {"type": "integer", "description": "How many last lines to read (default 100)"}}, "required": []},
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
    "Задачи выполняешь профессионально и честно.\n"
    "ЗАПРЕЩЕНО: писать комментарии в коде (#, //, /* */, --). Никогда. Нигде. Вообще.\n\n"

    "ТЫ ОДНОВРЕМЕННО И БОТ И АГЕНТ:\n"
    "• Простой чат/вопросы → reply(text) сразу, без инструментов\n"
    "• Нарисовать → generate_image(prompt на английском)\n"
    "• Написать код/программу/сайт с нуля → generate_project(подробное ТЗ)\n"
    "• Скачать/найти ГОТОВЫЙ проект с GitHub/интернета → run_shell(git clone ... && zip ...) потом send_workspace_file(path='repo.zip')\n"
    "• Отправить ссылки красиво → send_with_buttons(text='...', buttons=[[{text:'YouTube',url:'...'},{text:'Reddit',url:'...'}]])\n"
    "• Поиск инфы → web_search, потом reply\n"
    "• Найти картинку → search_and_send_image\n"
    "• Найти видео → search_and_send_video(creator='...' если указан автор)\n"
    "• Скачать видео по ссылке → download_video\n"
    "• Сервер/команды/код → run_shell / run_python (Docker sandbox, ЕСТЬ ИНТЕРНЕТ)\n"
    "• Данные/файлы → fetch_json, create_chart, translate, qr_code, create_file\n"
    "• Логи бота → read_bot_logs(lines=100) — читает bot.log с хоста (НЕ искать лог в Docker!). "
    "После вызова tool вернёт текст логов — ПРОЧИТАЙ его и расскажи что там написано!\n\n"
    "КОНТЕЙНЕР (Docker sandbox hatani-sandbox) — что установлено:\n"
    "Системные: git curl wget ffmpeg imagemagick tesseract-ocr(rus+eng) chromium chromium-driver build-essential jq poppler-utils zip unzip p7zip-full\n"
    "Python: numpy pandas scipy sympy matplotlib seaborn scikit-learn pillow opencv pytesseract\n"
    "         requests httpx aiohttp beautifulsoup4 lxml scrapy mechanize playwright selenium nodriver\n"
    "         openpyxl xlrd python-docx pyyaml pypdf2 reportlab pydantic cryptography psutil\n"
    "         yt-dlp pydub demucs(htdemucs model cached) gitpython black pytest rich click\n"
    "RAM: 1024MB, CPU: 2 ядра, таймаут команды: 10 мин. Интернет: ЕСТЬ.\n"
    "ВАЖНО: работаешь под юзером sandbox (НЕ root). apt-get, sudo — НЕ РАБОТАЮТ. Для установки Python-библиотек ОБЯЗАТЕЛЬНО используй uv: `uv pip install --system <пакет>` вместо pip install.\n"
    "Все нужные пакеты уже установлены — не трать шаги на их установку.\n"
    "ЗАПРЕЩЕНО: glob('/**/*', recursive=True), find /, os.walk('/') и любой рекурсивный обход всей файловой системы — зависает навсегда. Работай только в /workspace.\n"
    "Demucs: ВСЕГДА используй модель `-n mdx_extra_q` (быстрее htdemucs в 3 раза на CPU) + `-j 2 --segment 7`. "
    "Пример: demucs -n mdx_extra_q --two-stems=vocals -j 2 --segment 7 -o /workspace/out /workspace/audio.wav\n"
    "Прогресс-бар demucs не отображается (использует \\r), это нормально — жди завершения.\n\n"
    "АУДИО ОБРАБОТКА:\n"
    "После создания/обработки аудиофайла ВСЕГДА вызывай analyze_audio(path='...', question='Оцени качество: баланс, клиппинг, соответствие задаче'). "
    "Если Gemini говорит что есть проблемы (слишком громкий бас, клиппинг, плохой баланс) — "
    "исправь параметры и обработай заново ПЕРЕД отправкой пользователю. "
    "Отправляй только тот результат который сам считаешь качественным.\n\n"
    "КРИТИЧНО — reply завершает задачу НАВСЕГДА:\n"
    "Вызывай reply ТОЛЬКО когда задача полностью выполнена и файл/результат уже отправлен.\n"
    "НИКОГДА не пиши «call:default_api» или «call:» в тексте — юзай НАСТОЯЩИЙ инструмент reply.\n"
    "Если хочешь ответить — вызови reply ИНСТРУМЕНТОМ, а не текстом.\n\n"
    "Пока работаешь — используй think для размышлений, НЕ reply.\n"
    "ДУМАЙ ПЕРЕД КАЖДЫМ ДЕЙСТВИЕМ:\n"
    "Перед каждой командой/инструментом вызывай think() где:\n"
    "1. Объясни что ты собираешься сделать (1-2 предложения)\n"
    "2. Почему именно так\n"
    "НЕ думай одно и то же дважды. После think — сразу действуй.\n\n"

    "ПОИСК:\n"
    "Ищи в интернете всё что просят. Не отказывай в поиске без причины.\n"
    "Если пользователь говорит 'ищи дальше', 'продолжай', 'найди больше' — "
    "используй НОВЫЕ поисковые запросы, которые ещё не пробовал. "
    "Не повторяй те же запросы что давали одинаковые результаты. "
    "Пробуй другие ключевые слова, платформы, форматы запросов.\n\n"
    "TELEGRAM API ИНСТРУМЕНТЫ (используй когда нужно):\n"
    "Отправка: tg_send_poll, tg_send_location, tg_send_venue, tg_send_sticker, "
    "tg_send_contact, tg_send_dice, tg_send_animation, tg_send_video_note\n"
    "Сообщения: tg_react, tg_pin_message, tg_unpin_message, tg_edit_message, "
    "tg_delete_message, tg_forward_message, tg_copy_message\n"
    "Кнопки: send_with_buttons\n"
    "Чат-инфо: tg_get_chat_info, tg_get_admins, tg_get_member_count, "
    "tg_get_chat_member, tg_get_sticker_set, tg_export_invite_link\n"
    "Модерация (нужен админ): tg_ban_user, tg_unban_user, tg_kick_user, tg_restrict_member, "
    "tg_promote_member, tg_create_invite_link, tg_set_chat_title, tg_set_chat_description, "
    "tg_set_chat_photo (аватарка БЕСЕДЫ/ЧАТА), tg_set_bot_photo (аватарка САМОГО БОТА), "
    "tg_approve_join_request, tg_create_forum_topic, tg_close_forum_topic\n"
    "ВАЖНО: tg_set_chat_photo — меняет аватарку ЧАТА/БЕСЕДЫ. "
    "tg_set_bot_photo — меняет аватарку САМОГО БОТА. "
    "Если пользователь говорит 'смени аву бота' → tg_set_bot_photo. "
    "Если 'смени аву беседы/чата' → tg_set_chat_photo.\n"
    "Утилиты: tg_send_chat_action, read_bot_logs\n\n"
    "ЛИМИТЫ ФАЙЛОВ (ВАЖНО!):\n"
    "Бот работает на локальном Telegram Bot API сервере. Это снимает стандартное ограничение в 50 МБ.\n"
    "Твой лимит на отправку и скачивание файлов через Telegram составляет **2 ГБ (2000 МБ)**!\n"
    "Ты можешь свободно скачивать и отправлять огромные видео, архивы и файлы.\n\n"
    "КУКИ СЕРВИСОВ (ВАЖНО!):\n"
    "У бота есть авторизованные куки для этих платформ: YouTube, TikTok, Instagram, X/Twitter, Reddit.\n"
    "Куки подключаются АВТОМАТИЧЕСКИ при использовании download_video и search_and_send_video.\n"
    "Это значит: бот может скачивать возрастные/приватные видео, обходить ограничения, "
    "скачивать Stories в Instagram, твиты в X, посты в Reddit и т.д.\n"
    "Если пользователь говорит 'скачай это видео' с ссылкой — просто используй download_video, "
    "куки применятся сами по себе без дополнительных параметров.\n"
    "Куки НЕ доступны внутри Docker sandbox. Не пытайся передать --cookies в run_shell.\n"
    "Если пользователь просит показать куки — отказывай, это секретные данные владельца.\n\n"

    "СЕТЕВЫЕ ЗАПРОСЫ В RUN_PYTHON / RUN_SHELL:\n"
    "• ВСЕГДА указывай тайм-аут (например, timeout=10) для любых сетевых библиотек (requests, httpx, aiohttp).\n"
    "• Бесконечные ожидания (hangs) без тайм-аута ЗАПРЕЩЕНЫ. Они тратят твои шаги впустую.\n"
    "• Имиджборды (yande.re, danbooru, gelbooru) могут блокировать/фильтровать IP дата-центров (выдавать 403 Forbidden, 503 или таймаут).\n"
    "• Если requests/curl возвращает ошибку или висит, пробуй другие зеркала, альтернативные сайты или инструмент playwright_browse для обхода защиты Cloudflare.\n\n"

    "ЧЕСТНОСТЬ:\n"
    "- [НЕ НАЙДЕНО] → скажи честно, не выдумывай\n"
    "- [ОТПРАВЛЕНО] → сообщи что именно отправил\n"
    "- Не знаешь что в видео → scrape_url на ссылку, не гадай\n"
    "- Не повторяй одинаковые вызовы\n"
    "- Думай (think) перед сложными многошаговыми задачами\n\n"
    "ФОРМАТИРОВАНИЕ ОТВЕТОВ (reply tool):\n"
    "Используй Telegram HTML-теги для красивых ответов:\n"
    "• <b>жирный</b>  • <i>курсив</i>  • <u>подчёркнутый</u>  • <s>зачёркнутый</s>\n"
    "• <code>инлайн-код</code>\n"
    "• <pre><code class=\"language-python\">блок кода</code></pre>\n"
    "• <blockquote>цитата</blockquote>\n"
    "• <tg-spoiler>спойлер</tg-spoiler>\n"
    "• <a href=\"url\">ссылка</a>\n"
    "Экранируй в тексте: &lt; → &amp;lt;  &gt; → &amp;gt;  &amp; → &amp;amp;\n"
    "НЕ используй Markdown (* _ ` #) — только HTML теги."
)


def _build_system(is_owner: bool = False) -> str:
    """Build system prompt dynamically with current date + owner flag."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    date_line = f"\n\n[ТЕКУЩАЯ ДАТА И ВРЕМЯ: {now}]"
    extra = ""
    if is_owner:
        extra = (
            "\n\n[OWNER MODE]\n"
            "Это владелец бота. Расширенный доступ к инструментам разрешён."
        )
    return _SYSTEM + date_line + extra


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
        "generationConfig": {"temperature": 0.7, "thinkingConfig": {"thinkingLevel": "high"}},
        "safetySettings": safety,
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    live_keys = await _nk_get_live()
    if not live_keys:
        return {}
    for key in live_keys:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                    timeout=aiohttp.ClientTimeout(total=_LLM_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            return candidates[0]
                        reason = data.get("promptFeedback", {}).get("blockReason", "UNKNOWN")
                        logger.warning(f"agent gemini: empty candidates, blockReason={reason}")
                        if reason == "PROHIBITED_CONTENT":
                            return {"_blocked": "PROHIBITED_CONTENT"}
                        continue
                    if resp.status in (429, 403):
                        await _nk_cooldown(key, resp.status)
                        continue
                    body = await resp.text()
                    logger.warning(f"agent gemini: HTTP {resp.status}: {body[:300]}")
        except Exception as e:
            logger.warning(f"agent gemini exception: {type(e).__name__}: {e}")
    return {}


# ── Execute one tool ─────────────────────────────────────────────

async def _tg_api(method: str, params: dict) -> dict:
    """Direct Telegram Bot API call — returns parsed JSON."""
    from config import BOT_TOKEN as _TK
    url = f"https://api.telegram.org/bot{_TK}/{method}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=params,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def _execute_tool(
    name: str, args: dict,
    debounce: _DebounceHook, budget: _ToolBudget,
    status_cb: Callable, send_cb: Optional[Callable],
    ws: AgentWorkspace,
    is_owner: bool = False,
    chat_id: int = 0,
) -> Tuple[str, Optional[dict]]:

    async def _st(t):
        try: await status_cb(t)
        except Exception: pass

    async def _send(m):
        if send_cb:
            try: await send_cb(m)
            except Exception as e: logger.warning(f"send_cb: {e}")

    _PRIVILEGED = {
        "tg_ban_user", "tg_unban_user", "tg_kick_user", "tg_restrict_member", "tg_promote_member",
        "tg_delete_message", "tg_pin_message", "tg_unpin_message",
        "tg_set_chat_title", "tg_set_chat_description", "tg_set_chat_photo",
        "tg_set_bot_photo", "tg_create_invite_link", "read_bot_logs",
    }
    if name in _PRIVILEGED and not is_owner:
        return "Недостаточно прав. Это действие доступно только администраторам.", None

    if e := debounce.check(name, args): return e, None
    if e := budget.charge(name):        return e, None

    if name == "think":
        import html as _html
        t = args.get("thought", "")
        logger.info(f"[think] {t[:300]}")
        safe_t = _html.escape(t[:1200])
        await _st(f"💭 <b>Размышляю:</b>\n<blockquote expandable>{safe_t}</blockquote>")
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

    if name == "send_with_buttons":
        text = args.get("text", "")
        rows = args.get("buttons", [])
        await _send({"type": "inline_buttons", "text": text, "buttons": rows})
        return "[ОТПРАВЛЕНО] Сообщение с кнопками отправлено.", None

    # ── Telegram API tools ───────────────────────────────────────────
    if name == "tg_send_poll":
        await _send({"type": "tg_poll", "question": args.get("question", ""),
                     "options": args.get("options", []),
                     "is_anonymous": args.get("is_anonymous", True),
                     "allows_multiple_answers": args.get("allows_multiple_answers", False)})
        return "[ОТПРАВЛЕНО] Опрос создан.", None

    if name == "tg_send_location":
        await _send({"type": "tg_location", "latitude": args.get("latitude"),
                     "longitude": args.get("longitude"),
                     "title": args.get("title"), "address": args.get("address")})
        return "[ОТПРАВЛЕНО] Локация отправлена.", None

    if name == "tg_react":
        await _send({"type": "tg_react", "emoji": args.get("emoji", "👍"),
                     "message_id": args.get("message_id")})
        return "Реакция добавлена.", None

    if name == "tg_pin_message":
        await _send({"type": "tg_pin", "message_id": args.get("message_id"),
                     "disable_notification": args.get("disable_notification", False)})
        return "Сообщение закреплено.", None

    if name == "tg_delete_message":
        await _send({"type": "tg_delete", "message_id": args.get("message_id")})
        return "Сообщение удалено.", None

    if name == "tg_forward_message":
        await _send({"type": "tg_forward", "from_chat_id": args.get("from_chat_id"),
                     "message_id": args.get("message_id")})
        return "[ОТПРАВЛЕНО] Сообщение переслано.", None

    if name == "tg_get_chat_info":
        r = await _tg_api("getChat", {"chat_id": chat_id})
        rc = await _tg_api("getChatMemberCount", {"chat_id": chat_id})
        if not r.get("ok"):
            return f"Ошибка: {r.get('description','unknown')}", None
        c = r["result"]
        count = rc.get("result", "?")
        result = (f"Чат: {c.get('title','N/A')}\nID: {c.get('id')}\n"
                  f"Тип: {c.get('type')}\nУчастников: {count}\n"
                  f"Описание: {c.get('description','—')}\nUsername: @{c.get('username','—')}")
        import html as _html
        await _st(f"ℹ️ <b>Инфо о чате:</b>\n<pre>{_html.escape(result)}</pre>")
        return result, None

    if name == "tg_ban_user":
        await _send({"type": "tg_ban", "user_id": args.get("user_id"),
                     "reason": args.get("reason", ""), "until_date": args.get("until_date")})
        return f"Пользователь {args.get('user_id')} заблокирован.", None

    if name == "tg_unban_user":
        await _send({"type": "tg_unban", "user_id": args.get("user_id")})
        return f"Пользователь {args.get('user_id')} разбанен.", None

    if name == "tg_kick_user":
        await _send({"type": "tg_kick", "user_id": args.get("user_id"),
                     "reason": args.get("reason", "")})
        return f"Пользователь {args.get('user_id')} кикнут.", None

    if name == "tg_send_chat_action":
        await _send({"type": "tg_chat_action", "action": args.get("action", "typing")})
        return "Действие отправлено.", None
    if name == "tg_restrict_member":
        await _send({"type": "tg_restrict", "user_id": args.get("user_id"),
                     "can_send_messages": args.get("can_send_messages", False),
                     "can_send_media": args.get("can_send_media", False),
                     "until_date": args.get("until_date")})
        return f"Пользователь {args.get('user_id')} ограничен.", None
    if name == "tg_unpin_message":
        await _send({"type": "tg_unpin", "message_id": args.get("message_id")})
        return "Сообщение откреплено.", None
    if name == "tg_create_invite_link":
        await _send({"type": "tg_invite_link", "name": args.get("name"),
                     "expire_date": args.get("expire_date"),
                     "member_limit": args.get("member_limit")})
        return "[ОТПРАВЛЕНО] Ссылка создана.", None
    if name == "tg_set_bot_photo":
        path = args.get("path", "")
        try:
            full = ws._safe_path(path)
            with open(full, "rb") as f:
                data = f.read()
        except Exception as e:
            return f"Не смог прочитать файл {path}: {e}", None
        await _send({"type": "tg_set_bot_photo", "data": data, "filename": path})
        return "[ОТПРАВЛЕНО] Аватарка бота установлена.", None
    if name == "tg_set_chat_description":
        await _send({"type": "tg_set_chat_description", "description": args.get("description", "")})
        return "Описание чата изменено.", None
    if name == "tg_set_chat_title":
        await _send({"type": "tg_set_chat_title", "title": args.get("title", "")})
        return "Название чата изменено.", None
    if name == "tg_copy_message":
        await _send({"type": "tg_copy_message", "from_chat_id": args.get("from_chat_id"),
                     "message_id": args.get("message_id"), "caption": args.get("caption")})
        return "[ОТПРАВЛЕНО] Сообщение скопировано.", None
    if name == "tg_send_sticker":
        await _send({"type": "tg_send_sticker", "sticker": args.get("sticker", "")})
        return "[ОТПРАВЛЕНО] Стикер отправлен.", None
    if name == "tg_send_contact":
        await _send({"type": "tg_send_contact", "phone": args.get("phone", ""),
                     "name": args.get("first_name", ""), "last_name": args.get("last_name", "")})
        return "[ОТПРАВЛЕНО] Контакт отправлен.", None
    if name == "tg_send_dice":
        await _send({"type": "tg_send_dice", "emoji": args.get("emoji", "🎲")})
        return "[ОТПРАВЛЕНО] Кубик брошен.", None
    if name == "tg_edit_message":
        await _send({"type": "tg_edit_message", "message_id": args.get("message_id"),
                     "text": args.get("text", "")})
        return "Сообщение отредактировано.", None
    if name == "tg_send_animation":
        await _send({"type": "tg_send_animation", "url": args.get("url",""),
                     "caption": args.get("caption","")})
        return "[ОТПРАВЛЕНО] GIF отправлен.", None
    if name == "tg_send_video_note":
        await _send({"type": "tg_send_video_note", "file_id": args.get("file_id","")})
        return "[ОТПРАВЛЕНО] Кружок отправлен.", None
    if name == "tg_send_venue":
        await _send({"type": "tg_send_venue", "latitude": args.get("latitude"),
                     "longitude": args.get("longitude"), "title": args.get("title",""),
                     "address": args.get("address","")})
        return "[ОТПРАВЛЕНО] Место отправлено.", None
    if name == "tg_promote_member":
        await _send({"type": "tg_promote", "user_id": args.get("user_id"),
                     "can_delete_messages": args.get("can_delete_messages", False),
                     "can_pin_messages": args.get("can_pin_messages", False),
                     "can_manage_chat": args.get("can_manage_chat", False),
                     "can_ban_members": args.get("can_ban_members", False),
                     "custom_title": args.get("custom_title","")})
        return f"Пользователь {args.get('user_id')} обновлён.", None
    if name == "tg_get_chat_member":
        r = await _tg_api("getChatMember", {"chat_id": chat_id, "user_id": args.get("user_id")})
        if not r.get("ok"):
            return f"Ошибка: {r.get('description','unknown')}", None
        m = r["result"]
        u = m.get("user", {})
        result = (f"Пользователь: {u.get('first_name','')} {u.get('last_name','')} "
                  f"(@{u.get('username','—')})\nID: {u.get('id')}\nСтатус: {m.get('status')}")
        return result, None

    if name == "tg_get_admins":
        r = await _tg_api("getChatAdministrators", {"chat_id": chat_id})
        if not r.get("ok"):
            return f"Ошибка: {r.get('description','unknown')}", None
        admins = []
        for m in r["result"]:
            u = m.get("user", {})
            name_str = f"{u.get('first_name','')} (@{u.get('username','—')}) [{m.get('status')}]"
            admins.append(name_str)
        return "Администраторы:\n" + "\n".join(admins), None

    if name == "tg_get_member_count":
        r = await _tg_api("getChatMemberCount", {"chat_id": chat_id})
        if not r.get("ok"):
            return f"Ошибка: {r.get('description','unknown')}", None
        return f"Участников в чате: {r['result']}", None
    if name == "tg_create_forum_topic":
        await _send({"type": "tg_create_forum_topic", "name": args.get("name",""),
                     "icon_emoji": args.get("icon_emoji","")})
        return "Топик создан.", None
    if name == "tg_close_forum_topic":
        await _send({"type": "tg_close_forum_topic",
                     "message_thread_id": args.get("message_thread_id")})
        return "Топик закрыт.", None
    if name == "tg_get_sticker_set":
        await _send({"type": "tg_get_sticker_set", "name": args.get("name","")})
        return "Инфо о стикер-паке запрошено.", None
    if name == "tg_approve_join_request":
        await _send({"type": "tg_approve_join", "user_id": args.get("user_id"),
                     "approve": args.get("approve", True)})
        return "Заявка обработана.", None
    if name == "tg_export_invite_link":
        await _send({"type": "tg_export_link"})
        return "Ссылка запрошена.", None
    if name == "tg_set_chat_photo":
        path = args.get("path", "")
        try:
            full = ws._safe_path(path)
            with open(full, "rb") as f:
                data = f.read()
        except Exception as e:
            return f"Не смог прочитать файл {path}: {e}", None
        await _send({"type": "tg_set_chat_photo", "data": data, "filename": path})
        return "[ОТПРАВЛЕНО] Аватарка чата установлена.", None
    if name == "fetch_tiktok_profile":
        username = args.get("username", "").lstrip("@").strip()
        if not username:
            return "Укажи username.", None
        import subprocess, json as _json
        cookie_path = "/root/cookies/tiktok.txt"
        try:
            # Use yt-dlp to get profile playlist metadata
            result = subprocess.run(
                ["yt-dlp", "--cookies", cookie_path, "--dump-json", "--flat-playlist",
                 "--playlist-end", "1", f"https://www.tiktok.com/@{username}"],
                capture_output=True, text=True, timeout=180
            )
            if result.returncode == 0 and result.stdout.strip():
                first = _json.loads(result.stdout.strip().splitlines()[0])
                channel = first.get("channel") or first.get("uploader", username)
                count = first.get("channel_follower_count") or "?"
                return (f"TikTok @{username}:\n"
                        f"Имя: {channel}\n"
                        f"Подписчиков: {count}\n"
                        f"Видео: {first.get('playlist_count','?')}\n"
                        f"Bio: {first.get('description','—')}"), None
            # Fallback: scrape via curl with cookies
            curl = subprocess.run(
                ["curl", "-s", "-b", cookie_path, "--user-agent",
                 "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                 f"https://www.tiktok.com/api/user/detail/?uniqueId={username}&aid=1988"],
                capture_output=True, text=True, timeout=15
            )
            if curl.returncode == 0:
                data = _json.loads(curl.stdout)
                user = data.get("userInfo", {}).get("user", {})
                stats = data.get("userInfo", {}).get("stats", {})
                return (f"TikTok @{username} (через API):\n"
                        f"Имя: {user.get('nickname','?')}\n"
                        f"Подписчиков: {stats.get('followerCount','?')}\n"
                        f"Лайки: {stats.get('heartCount','?')}\n"
                        f"Видео: {stats.get('videoCount','?')}\n"
                        f"Bio: {user.get('signature','—')}"), None
            return f"Не смог получить данные профиля @{username}: {result.stderr[:200]}", None
        except Exception as e:
            return f"Ошибка fetch_tiktok_profile: {e}", None

    if name == "fetch_with_cookies":
        url = args.get("url", "")
        if not url:
            return "Укажи URL.", None
        # SSRF guard: block private/loopback/link-local targets
        import socket as _sock, ipaddress as _ipa
        def _ssrf_safe(u: str) -> bool:
            from urllib.parse import urlparse as _up
            p = _up(u)
            if p.scheme not in ("http", "https") or not p.hostname:
                return False
            _BLOCKED = [_ipa.ip_network(n) for n in (
                "127.0.0.0/8", "::1/128", "10.0.0.0/8",
                "172.16.0.0/12", "192.168.0.0/16",
                "169.254.0.0/16", "fd00::/8",
            )]
            try:
                for *_, sa in _sock.getaddrinfo(p.hostname, None):
                    if any(_ipa.ip_address(sa[0]) in net for net in _BLOCKED):
                        return False
            except Exception:
                return False
            return True
        def _ssrf_resolve(u: str):
            from urllib.parse import urlparse as _up2
            p2 = _up2(u)
            if p2.scheme not in ("http", "https") or not p2.hostname:
                return None
            port = p2.port or (443 if p2.scheme == "https" else 80)
            _BLK2 = [_ipa.ip_network(n) for n in (
                "127.0.0.0/8", "::1/128", "0.0.0.0/8",
                "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "169.254.0.0/16", "fd00::/8", "fc00::/8",
                "fe80::/10", "100.64.0.0/10",
            )]
            try:
                safe_ip = None
                for *_, sa in _sock.getaddrinfo(p2.hostname, None):
                    ip = _ipa.ip_address(sa[0])
                    if any(ip in net for net in _BLK2 if ip.version == net.version):
                        return None
                    safe_ip = sa[0]
                return (p2.hostname, port, safe_ip)
            except Exception:
                return None
        resolved = _ssrf_resolve(url)
        if not resolved:
            return "Запрос к этому адресу запрещён (SSRF защита).", None
        cookie_path_map = {
            "youtube.com": "/root/cookies/youtube.txt",
            "youtu.be": "/root/cookies/youtube.txt",
            "tiktok.com": "/root/cookies/tiktok.txt",
            "instagram.com": "/root/cookies/instagram.txt",
            "x.com": "/root/cookies/x.txt",
            "twitter.com": "/root/cookies/x.txt",
            "reddit.com": "/root/cookies/reddit.txt",
        }
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        cookie_file = next((v for k, v in cookie_path_map.items() if k in host), None)
        import subprocess
        _host, _port, _safe_ip = resolved
        cmd = ["curl", "-s", "--max-time", "15", "--max-redirs", "0",
               "--resolve", f"{_host}:{_port}:{_safe_ip}",
               "--user-agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"]
        if cookie_file and os.path.exists(cookie_file):
            cmd += ["-b", cookie_file]
        cmd.append(url)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            out = result.stdout[:6000]
            if args.get("output_format") == "json":
                import json as _j
                try:
                    _j.loads(out)  # validate
                except Exception:
                    pass
            return out or "Пустой ответ.", None
        except Exception as e:
            return f"fetch_with_cookies error: {e}", None

    # ── End Telegram API tools ───────────────────────────────────────

    if name == "search_and_send_image":
        return await _tool_search_image(
            args.get("query", ""), args.get("description", ""), _send, status_cb
        ), None

    if name == "search_and_send_video":
        return await _tool_search_video(
            args.get("query", ""), args.get("description", ""),
            args.get("creator", ""), _send, status_cb
        ), None

    if name == "list_image_models":
        await _st("📋 Запрашиваю модели OpenAI...")
        return await _tool_list_image_models(), None

    if name == "generate_image":
        await _st("🎨 Генерирую картинку...")
        return await _tool_generate_image(
            args.get("prompt", ""), _send,
            provider=args.get("provider", "gemini"),
            model=args.get("model", ""),
        ), None

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

    if name == "analyze_audio":
        await _st("🎧 Слушаю результат через Gemini...")
        return await _tool_analyze_audio(
            path=args.get("path", ""), question=args.get("question", ""), ws=ws
        ), None

    if name == "analyze_image":
        await _st("🔍 Анализирую изображение через Gemini...")
        return await _tool_analyze_image(
            path=args.get("path", ""), question=args.get("question", ""), ws=ws
        ), None

    if name == "run_shell":
        import html as _html
        cmd = args.get("command", "")
        safe_cmd = _html.escape(cmd[:300])
        await _st(f"💻 Выполняю команду в Docker:\n<pre><code class=\"language-bash\">{safe_cmd}</code></pre>")
        return await _tool_run_shell(cmd, ws, _st), None

    if name == "playwright_browse":
        await _st(f"🌐 Открываю браузер: {args.get('url', '')[:80]}...")
        return await _tool_playwright_browse(
            url=args.get("url", ""),
            action=args.get("action", "screenshot"),
            selector=args.get("selector", ""),
            value=args.get("value", ""),
            js_code=args.get("js_code", ""),
            ws=ws, status_cb=_st, send_cb=send_media_cb,
        ), None

    if name == "write_file":
        path, content = args.get("path", "file.txt"), args.get("content", "")
        ws.write(path, content)
        return f"Written: {path} ({len(content)} chars)", None

    if name == "read_bot_logs":
        if not is_owner:
            return "Недостаточно прав. Это действие доступно только администраторам.", None
        import html as _html
        log_text = await _tool_read_bot_logs(args.get("lines", 100))
        # Показываем в статус-сообщении — обрезаем хвост для отображения
        display = log_text[-2500:] if len(log_text) > 2500 else log_text
        safe_log = _html.escape(display)
        await _st(f"📋 <b>Логи бота:</b>\n<pre><code>{safe_log}</code></pre>")
        return log_text, None  # агент видит полный текст

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
    initial_files: Optional[dict] = None,  # {filename: bytes} pre-loaded into workspace
) -> Tuple[Optional[str], Optional[dict]]:
    keys = await load_keys()
    if not keys:
        return "Gemini keys are dead.", None

    import time as _time
    from state import chat_workspaces as _cws
    _WS_TTL = 7200  # 2 часа
    _existing = _cws.get(chat_id)
    _existing_path = ""
    if _existing and _time.time() - _existing["ts"] < _WS_TTL:
        _existing_path = _existing["path"]
    ws = AgentWorkspace(existing_path=_existing_path)
    _cws[chat_id] = {"path": ws.host_path, "ts": _time.time()}
    if initial_files:
        ws.preload(initial_files)
    debounce = _DebounceHook()
    budget   = _ToolBudget()
    contents: list = [{"role": "user", "parts": [{"text": f"Задача от {username}:\n{task}"}]}]

    async def _st(t):
        try: await status_cb(t)
        except Exception: pass

    _TOOL_LABELS: dict[str, str] = {
        "think": "Размышляю...", "web_search": "Ищу в интернете...",
        "scrape_url": "Читаю страницу...", "fetch_json": "Запрашиваю данные...",
        "generate_project": "Генерирую проект...", "list_image_models": "Запрашиваю модели...", "generate_image": "Рисую...",
        "search_and_send_image": "Ищу картинку...", "download_image": "Скачиваю картинку...",
        "search_and_send_video": "Ищу видео...", "download_video": "Скачиваю видео...",
        "text_to_speech": "Озвучиваю...", "run_python": "Запускаю Python...",
        "analyze_audio": "Слушаю результат...", "analyze_image": "Анализирую изображение...", "run_shell": "Выполняю команду...", "playwright_browse": "Открываю браузер...",
        "write_file": "Записываю файл...",
        "read_bot_logs": "Читаю логи бота...", "read_file": "Читаю файл...", "calculate": "Считаю...", "translate": "Перевожу...",
        "qr_code": "Генерирую QR...", "create_chart": "Строю график...",
        "create_file": "Создаю файл...", "send_workspace_file": "Отправляю файл...",
        "send_with_buttons": "Отправляю кнопки...", "reply": "Формулирую ответ...",
    }
    last_action = "Формулирую план..."
    import time as _time
    _start_ts = _time.monotonic()

    def _fmt_status(action: str, step: int) -> str:
        elapsed = int(_time.monotonic() - _start_ts)
        m, s = divmod(elapsed, 60)
        t = f"{m}м {s}с" if m else f"{s}с"
        return f"🤖 Шаг {step+1}/{MAX_STEPS} · {t} · {action}"

    try:
        for step in range(MAX_STEPS):
            await _st(_fmt_status(last_action, step))

            if MAX_STEPS - step <= 3:
                contents.append({"role": "user", "parts": [{"text":
                    f"\n[СИСТЕМА: осталось {MAX_STEPS - step} шагов. Завершай.]"
                }]})

            candidate = await _gemini_call(keys, contents, is_owner=is_owner)

            # PROHIBITED_CONTENT — context is poisoned, retry with clean prompt
            if candidate.get("_blocked") == "PROHIBITED_CONTENT":
                from state import chat_context_buffer
                chat_context_buffer.pop(chat_id, None)
                # Rebuild contents without chat context
                clean_task = task.split("[/Справочный контекст]")[-1].strip() or task
                contents = [{"role": "user", "parts": [{"text": clean_task}]}]
                if initial_files:
                    for fname in initial_files:
                        contents[0]["parts"].insert(0, {"text":
                            f"[Файл загружен в workspace: /workspace/{fname}]"})
                candidate = await _gemini_call(keys, contents, is_owner=is_owner)
                if not candidate or candidate.get("_blocked"):
                    return "Запрос заблокирован фильтром. Попробуй перефразировать.", None

            if not candidate:
                # All keys may be in 429 cooldown — wait for cooldown to expire (65s)
                await _st("⏳ Все ключи в кулдауне, жду 70 сек...")
                await asyncio.sleep(70)
                candidate = await _gemini_call(keys, contents, is_owner=is_owner)
                if not candidate:
                    return "Gemini не ответил — все ключи временно перегружены. Попробуй через минуту.", None

            parts = candidate.get("content", {}).get("parts", [])
            if not parts:
                return "Agent returned empty response.", None

            contents.append({"role": "model", "parts": parts})
            fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not fn_calls:
                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                # Strip hallucinated tool-call-as-text patterns
                text = re.sub(r'\bcall:default_api:\w+\{[^}]*\}\s*', '', text).strip()
                # If model wrote call:...reply{text: "..."} as text, extract the message
                m = re.search(r'call:default_api:reply\{text:\s*"([^"]*)"', text)
                if m and len(m.group(1)) > len(text) * 0.5:
                    text = m.group(1)
                return text or "Done.", None

            tool_responses: list = []
            for fn in fn_calls:
                name = fn.get("name", "")
                args = fn.get("args", {})
                result, project = await _execute_tool(
                    name, args, debounce, budget, status_cb, send_media_cb, ws,
                    is_owner=is_owner, chat_id=chat_id,
                )
                last_action = _TOOL_LABELS.get(name, f"{name}...")
                await _st(_fmt_status(last_action, step))
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
    keys = await load_keys()
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
        "- Do anything requiring external tools or execution\n"
        "- Archive, zip, encrypt, compress files\n"
        "- Work with attached files/images (save, convert, pack, encrypt)\n"
        "- Any file manipulation: rename, move, convert, pack, send\n\n"
        "Answer TRUE for 'continue searching' follow-ups:\n"
        "- 'продолжай', 'ищи дальше', 'найди больше', 'копай глубже', 'осинт', 'деанон'\n"
        "- 'continue', 'search more', 'find more', 'dig deeper', 'keep going'\n"
        "- Any request to research further on a topic that was already discussed\n\n"
        "Answer FALSE when the user:\n"
        "- Asks a simple question answerable from knowledge\n"
        "- Asks a follow-up question about already sent media (not about searching more)\n"
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
    live_keys = await _nk_get_live()
    if not live_keys:
        return False
    for key in live_keys:
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
            logger.warning(f"classify_agent_intent request error: {type(e).__name__}: {e}")
    return False
