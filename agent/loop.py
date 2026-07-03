"""
Agent orchestration loop — _gemini_call, _execute_tool, run_agent, classify_agent_intent.

Supporting modules: workspace, safety, search, media, sandbox, tools, tg_api, prompts.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from typing import Any, Callable, Optional, Tuple

import aiohttp

from ai_services import generate_project_with_gemini
from keys import load_keys, remove_key
from keys import get_live_keys as _nk_get_live, sync_from_keyhunter as _nk_sync, init_db as _nk_init

from services.security_utils import is_safe_url
logger = logging.getLogger(__name__)

MAX_STEPS       = 60
_SEARCH_TIMEOUT = 20.0
_SCRAPE_TIMEOUT = 25.0
_LLM_TIMEOUT    = 60.0
_PROJECT_TIMEOUT= 180.0


from .workspace import AgentWorkspace
from .safety import _DebounceHook, _ToolBudget

# ── Firecrawl helpers ────────────────────────────────────────────
from .search import _fc_search, _fc_scrape, _search_image_urls, _download_bytes, _image_is_relevant
from .tools import _tool_fetch_json, _ast_eval, _tool_read_bot_logs, _tool_send_workspace_file, _tool_create_file
from .tg_api import _PRIVILEGED, handle_tg_tool
from .sandbox import _snip_output, _mask_cookies, _tool_run_python, _tool_run_shell, _tool_analyze_image, _tool_analyze_audio, _tool_playwright_browse

# ── Media tools ─────────────────────────────────────────────────
from .media import (
    _tool_search_image, _tool_generate_image, _tool_list_image_models,
    _tool_download_image, _cookies_for_url, _tool_download_video,
    _find_video_urls, _verify_video_creator, _tool_search_video,
    _tool_tts, _tool_qr_code, _tool_create_chart, _tool_translate,
)




from .prompts import _TOOLS, _SYSTEM, _build_system


async def _gemini_call(keys: list, contents: list, is_owner: bool = False) -> dict:
    """Call Gemini 3.5 Flash with tools — returns dict with '_error' on failure."""
    import time as _t
    errors = []  # accumulate errors from all models

    if len(contents) > 16:
        contents = [contents[0]] + contents[-15:]

    for model_name in ("gemini-3.5-flash", "gemini-3.1-pro-preview"):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": _build_system(is_owner)}]},
            "contents": contents,
            "tools": [{"functionDeclarations": _TOOLS}],
            "generationConfig": {"temperature": 1.0, "thinkingConfig": {"thinkingLevel": "minimal"}},
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }
        for key in (keys or [])[:8]:
            t0 = _t.monotonic()
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(url, json=payload,
                            headers={"Content-Type": "application/json", "x-goog-api-key": key},
                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                c = candidates[0]
                                parts = c.get("content", {}).get("parts", [])
                                fc = [p.get("functionCall") for p in parts if "functionCall" in p]
                                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                                dt = _t.monotonic() - t0
                                if fc:
                                    logger.info(f"agent: Gemini tool calls [{dt:.1f}s]: {[f['name'] for f in fc]}")
                                    return {"content": {"parts": parts, "role": "model"}, "_finish": c.get("finishReason", "")}
                                if text:
                                    logger.info(f"agent: Gemini text [{dt:.1f}s] ({len(text)} chars)")
                                    return {"content": {"parts": [{"text": text}], "role": "model"}, "_finish": c.get("finishReason", "")}
                                err_msg = f"{model_name}: empty response ({c.get('finishReason', '?')})"
                                errors.append(err_msg); logger.warning(f"agent: {err_msg}")
                        elif resp.status in (429, 403):
                            errors.append(f"{model_name}: HTTP {resp.status} (rate limited)")
                            continue
                        else:
                            body = await resp.text()
                            errors.append(f"{model_name}: HTTP {resp.status}")
                            logger.warning(f"agent: {model_name}: HTTP {resp.status}")
            except Exception as e:
                errors.append(f"{model_name}: {type(e).__name__}: {e}")
                logger.warning(f"agent: {model_name}: {type(e).__name__}: {e}")
    return {"_error": "; ".join(errors) if errors else "Gemini: no keys available"}


# ── Execute one tool ─────────────────────────────────────────────


async def _execute_tool(
    name: str, args: dict,
    debounce: _DebounceHook, budget: _ToolBudget,
    status_cb: Callable, send_cb: Optional[Callable],
    ws: AgentWorkspace,
    is_owner: bool = False,
    chat_id: int = 0,
) -> Tuple[str, Optional[dict]]:
    # Tool aliases (Groq model may use different names)
    if name == "web_scrape":
        name = "scrape_url"

    async def _st(t):
        try: await status_cb(t)
        except Exception: pass

    async def _send(m):
        if send_cb:
            try: await send_cb(m)
            except Exception as e: logger.warning(f"send_cb: {e}")

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
    if name.startswith("tg_"):
        result = await handle_tg_tool(name, args, ws, _send, chat_id, _st)
        if not result[0].startswith("Unknown tg tool:"):
            return result
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
            ws=ws, status_cb=_st, send_cb=send_cb,
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
        # ── Groq fast path: try to answer without tools first ──────────────
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
                logger.warning("agent: Groq returned empty, retrying once...")
                candidate = await _gemini_call(keys, contents, is_owner=is_owner)
                if not candidate:
                    return f"Модели недоступны: {candidate.get('_error', 'неизвестная ошибка')}", None

            if candidate.get("_error"):
                return f"Модели недоступны: {candidate['_error']}", None

            parts = candidate.get("content", {}).get("parts", [])
            if not parts:
                return f"Агент не смог ответить. Причина: {candidate.get('_finish', 'пустой ответ')}", None

            contents.append({"role": "model", "parts": parts})
            fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not fn_calls:
                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                # Strip hallucinated tool-call-as-text patterns (both Gemini and Groq formats)
                text = re.sub(r'\b(?:call:default_api:)?\w+\(\w+="([^"]*)"\)', r'\1', text)
                text = re.sub(r'<\w+\.?\w+="([^"]*)"\s*/?>', r'\1', text)
                text = re.sub(r'<reply[^>]*>|</reply>', '', text)
                text = text.strip()
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
