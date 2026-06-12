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

MAX_STEPS       = 18
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
            "--network=none",
            "--memory=512m", "--cpus=0.5",
            "--user=sandbox",
            "--workdir=/workspace",
            "-v", f"{self.host_path}:/workspace",
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
        "web_search": 8, "scrape_url": 10, "generate_project": 2,
        "think": 30, "reply": 3, "generate_image": 3,
        "search_and_send_image": 3, "download_image": 5,
        "search_and_send_video": 2, "download_video": 2, "text_to_speech": 3,
        "run_python": 6, "run_shell": 8,
        "write_file": 10, "read_file": 10,
        "fetch_json": 8, "calculate": 20,
        "qr_code": 3, "create_chart": 3,
        "translate": 5, "create_file": 5,
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
    keys = load_firecrawl_keys()
    if not keys:
        return "Firecrawl keys unavailable."
    payload = {
        "query": query[:500], "limit": 6,
        "sources": [{"type": "web"}],
        "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
    }
    for key in keys:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/search",
                    json=payload, headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=_SEARCH_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("data", [])
                        if isinstance(results, dict):
                            results = results.get("results", []) or results.get("web", [])
                        parts = []
                        for r in results[:6]:
                            title = r.get("title") or r.get("metadata", {}).get("title", "")
                            url   = r.get("url", "")
                            body  = (r.get("markdown") or r.get("description") or "")[:800]
                            if url:
                                parts.append(f"### {title}\nURL: {url}\n{body}".strip())
                        return "\n\n".join(parts) or "No results."
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
        except Exception as e:
            logger.warning(f"web_search {query!r}: {e}")
    return "Search unavailable."


async def _fc_scrape(url: str) -> str:
    keys = load_firecrawl_keys()
    if not keys:
        return "Firecrawl keys unavailable."
    payload = {"url": url, "formats": ["markdown"], "onlyMainContent": True,
               "removeBase64Images": True, "maxAge": 3_600_000}
    for key in keys:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/scrape",
                    json=payload, headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=_SCRAPE_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        d = (await resp.json()).get("data") or {}
                        return (d.get("markdown") or d.get("content") or "Page empty.")[:8000]
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
        except Exception as e:
            logger.warning(f"scrape {url!r}: {e}")
    return "Could not read page."


# ── Image search with self-evaluation ───────────────────────────

async def _ddg_image_urls(query: str) -> list[str]:
    """DuckDuckGo unofficial image search — no API key needed."""
    hdrs = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://duckduckgo.com/",
                params={"q": query, "iax": "images", "ia": "images"},
                headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()

            m = (re.search(r'vqd="([^"]+)"', text)
                 or re.search(r"vqd='([^']+)'", text)
                 or re.search(r'vqd=([\d-]+)', text))
            if not m:
                return []
            vqd = m.group(1)

            async with s.get(
                "https://duckduckgo.com/i.js",
                params={"q": query, "vqd": vqd, "l": "us-en",
                        "o": "json", "f": ",,,,,", "p": "1"},
                headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                return [r["image"] for r in data.get("results", [])[:12] if r.get("image")]
    except Exception as e:
        logger.warning(f"DDG image search: {e}")
        return []


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

        image_urls = await _ddg_image_urls(current_query)
        if not image_urls:
            # Fallback: extract image URLs from Firecrawl results
            fc_results = await _fc_search(f"{current_query} image")
            image_urls = re.findall(
                r'https?://[^\s\)"\'>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\)"\'>]*)?',
                fc_results, re.IGNORECASE,
            )[:8]

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
                return f"Found and sent image for '{current_query}'."

        await _st(f"⚠️ Ни одна картинка не подошла, формулирую лучший запрос...")

    return f"Could not find a relevant image after {max_rounds} attempts. Tried: {tried_queries}"


# ── Other tool implementations ───────────────────────────────────

async def _tool_generate_image(prompt: str, send_cb: Callable) -> str:
    img_bytes, err = await generate_image_with_gemini(prompt)
    if err or not img_bytes:
        return f"Image generation failed: {err or 'no data'}"
    await send_cb({"type": "photo", "data": img_bytes,
                   "caption": f"🎨 {prompt[:900]}", "filename": "image.jpg"})
    return "Image generated and sent."


async def _tool_download_image(url: str, caption: str, send_cb: Callable) -> str:
    data = await _download_bytes(url)
    if not data:
        return "Failed to download image."
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    fname = f"image.{ext}" if ext in ("jpg", "jpeg", "png", "webp", "gif") else "image.jpg"
    await send_cb({"type": "photo", "data": data,
                   "caption": caption[:1024] or url[:200], "filename": fname})
    return "Image downloaded and sent."


async def _tool_download_video(url: str, caption: str, send_cb: Callable) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "video.%(ext)s")
        cmd = [
            "yt-dlp", "--no-playlist", "--no-warnings",
            "-f", "bestvideo[height<=720][filesize<45M]+bestaudio/best[height<=720]/best[height<=480]",
            "--merge-output-format", "mp4", "-o", out, url,
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


async def _find_video_urls(query: str) -> list[str]:
    """Extract YouTube / TikTok / VK URLs from Firecrawl search results."""
    results = await _fc_search(f"{query} site:youtube.com OR site:tiktok.com OR site:vk.com")
    patterns = [
        r'https?://(?:www\.)?youtube\.com/watch\?[^\s\)"\'<>]+',
        r'https?://youtu\.be/[^\s\)"\'<>]+',
        r'https?://(?:www\.)?tiktok\.com/@[^\s\)"\'<>]+/video/[^\s\)"\'<>]+',
        r'https?://vm\.tiktok\.com/[^\s\)"\'<>]+',
        r'https?://vk\.com/video[^\s\)"\'<>]+',
    ]
    urls: list[str] = []
    for pat in patterns:
        urls.extend(re.findall(pat, results))
    seen: set[str] = set()
    clean: list[str] = []
    for u in urls:
        u = u.rstrip('.,;)"\'>]')
        if u not in seen:
            seen.add(u)
            clean.append(u)
    return clean[:8]


async def _tool_search_video(
    query: str, description: str, send_cb: Callable, status_cb: Callable
) -> str:
    """Autonomous video search: find URL → yt-dlp download → retry with refined query."""
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
                payload = {
                    "contents": [{"parts": [{"text":
                        f"Give a better YouTube/TikTok search query to find: '{description or query}'. "
                        f"Previous failed: {tried_queries}. Return ONLY the query."
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
                                current_query = f"{query} official edit"
                except Exception:
                    current_query = f"{query} edit"
            else:
                current_query = f"{query} edit"

        tried_queries.append(current_query)
        await _st(f"🔎 Ищу видео: «{current_query}» (попытка {rnd + 1}/{max_rounds})")

        video_urls = await _find_video_urls(current_query)
        if not video_urls:
            await _st(f"⚠️ Ссылок не нашёл, меняю запрос...")
            continue

        await _st(f"📥 Нашёл {len(video_urls)} ссылок, пробую скачать...")
        for vid_url in video_urls[:5]:
            result = await _tool_download_video(vid_url, description or query, send_cb)
            if "sent" in result.lower() or "mb)" in result.lower():
                return f"Video found and sent (query: '{current_query}')."
            logger.debug(f"search_video: {vid_url!r} → {result[:80]}")

        await _st(f"⚠️ Ни одно видео не скачалось, ищу лучше...")

    return f"Не смог найти и скачать видео за {max_rounds} попытки. Пробовал: {tried_queries}"


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


async def _tool_run_python(code: str, ws: "AgentWorkspace") -> str:
    stdout, stderr, rc = await ws.docker_run(["python", "-c", code])
    out = (stdout[:2000] + ("\n[stderr]: " + stderr[:500] if stderr.strip() else "")).strip()
    return out or f"(exit {rc}, no output)"


async def _tool_run_shell(command: str, ws: "AgentWorkspace") -> str:
    stdout, stderr, rc = await ws.docker_run(["bash", "-c", command])
    out = (stdout[:2000] + ("\n[stderr]: " + stderr[:500] if stderr.strip() else "")).strip()
    return out or f"(exit {rc}, no output)"


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
        "description": "Read full content of a web page.",
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
        "description": "Generate an AI image from a text prompt and send to chat.",
        "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "Detailed image description in English"}}, "required": ["prompt"]},
    },
    {
        "name": "download_image",
        "description": "Download image from a specific known URL and send to chat.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "caption": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "search_and_send_video",
        "description": (
            "Autonomously search for a video by description, find its URL on YouTube/TikTok/VK, "
            "download via yt-dlp and send to chat. Retries with refined queries if download fails. "
            "Use this when user asks to FIND and send a video — don't use web_search+download_video manually."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for the video"},
                "description": {"type": "string", "description": "What a good result should look like"},
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
            "Execute Python code in isolated Docker sandbox (no network, 512MB RAM). "
            "Files written to /workspace persist between calls. "
            "Has: numpy, pandas, matplotlib, pillow, scipy, sympy. Use print() for output."
        ),
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    },
    {
        "name": "run_shell",
        "description": (
            "Execute shell commands in isolated Docker sandbox (no network, 512MB RAM). "
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
    "Ты — Hatani AI, мощный агент с доступом к 20 инструментам. Говоришь грубо, делаешь профессионально.\n\n"
    "ВЫБОР ИНСТРУМЕНТА — читай внимательно:\n\n"
    "ЕСЛИ просят что-то ВЫПОЛНИТЬ/ЗАПУСТИТЬ/ПРОВЕРИТЬ на сервере:\n"
    "  → run_shell для команд bash (ps, df, top, нагрузка, стресс-тест и т.д.)\n"
    "  → run_python для Python-кода (вычисления, data, matplotlib и т.д.)\n"
    "  НЕ generate_project — это для создания файлов, не для выполнения команд\n\n"
    "ЕСЛИ просят НАЙТИ картинку/фото:\n"
    "  → search_and_send_image — сам найдёт, проверит Gemini vision, пришлёт лучшую\n\n"
    "ЕСЛИ просят НАЙТИ видео/эдит/клип по описанию:\n"
    "  → search_and_send_video — сам найдёт YouTube/TikTok ссылку, скачает, пришлёт\n"
    "  НЕ web_search + download_video вручную\n\n"
    "ЕСЛИ просят СОЗДАТЬ проект/сайт/программу:\n"
    "  → web_search (если нужен контекст) → generate_project\n\n"
    "ЕСЛИ просят СКАЧАТЬ видео:\n"
    "  → download_video с прямой ссылкой\n\n"
    "ПРИМЕРЫ правильного выбора:\n"
    "  'нагрузи оперативку' → run_python (allocate big list, measure)\n"
    "  'покажи загрузку CPU' → run_shell('top -bn1 | head -20')\n"
    "  'найди картинку кота' → search_and_send_image\n"
    "  'сколько места на диске' → run_shell('df -h')\n"
    "  'сделай сайт про X' → web_search → generate_project\n\n"
    "ПРОТОКОЛ:\n"
    "1. think → одна фраза что именно делаю и каким инструментом\n"
    "2. Инструмент\n"
    "3. reply или следующий шаг\n\n"
    "Не повторяй одинаковые вызовы. Будь конкретным."
)


async def _gemini_call(keys: list, contents: list) -> dict:
    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM}]},
        "contents": contents,
        "tools": [{"functionDeclarations": _TOOLS}],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {"temperature": 0.7, "thinkingConfig": {"thinkingLevel": "minimal"}},
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
            args.get("query", ""), args.get("description", ""), _send, status_cb
        ), None

    if name == "generate_image":
        await _st("🎨 Генерирую картинку...")
        return await _tool_generate_image(args.get("prompt", ""), _send), None

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
        await _st("🐍 Запускаю Python в Docker...")
        return await _tool_run_python(args.get("code", ""), ws), None

    if name == "run_shell":
        await _st("💻 Выполняю команду в Docker...")
        return await _tool_run_shell(args.get("command", ""), ws), None

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

            candidate = await _gemini_call(keys, contents)
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
        "Answer FALSE when the user just:\n"
        "- Asks a question answerable from knowledge (history, concepts, explanations)\n"
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
