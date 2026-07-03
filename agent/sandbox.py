"""
Sandbox execution tools — Python, shell, Playwright, media analysis, and output sanitizers.
"""
import logging
import os
import re
from typing import Callable

import aiohttp

from keys import load_keys, remove_key
from keys import get_live_keys as _nk_get_live, mark_cooldown as _nk_cooldown

from .workspace import AgentWorkspace

logger = logging.getLogger(__name__)

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
