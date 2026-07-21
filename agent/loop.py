"""
Agent orchestration loop — _sonnet_call, _execute_tool, run_agent, classify_agent_intent.

Supporting modules: workspace, safety, search, media, sandbox, tools, tg_api, prompts.
"""
import asyncio
import logging
import html
import os
import re
from typing import Any, Callable, Optional, Tuple


from services.code_service import generate_project_with_gemini

from services.deepseek_service import _gemini_contents_to_openai_messages, _gemini_tools_to_openai, _openai_response_to_gemini
from services.openrouter import generate_text_with_openrouter, openrouter_chat
logger = logging.getLogger(__name__)

from config import (
    AGENT_CLASSIFY_MAX_TOKENS,
    AGENT_CLASSIFY_TIMEOUT,
    AGENT_MAX_STEPS,
    AGENT_PROJECT_TIMEOUT,
    AGENT_SONNET_MAX_TOKENS,
    AGENT_SONNET_TIMEOUT,
    AGENT_WORKSPACE_TTL,
    NEWS_EMOJI_IDS,
    OPENROUTER_TEXT_MODEL,
)

_E = NEWS_EMOJI_IDS


from .workspace import AgentWorkspace
from .safety import _DebounceHook, _ToolBudget

# ── Firecrawl helpers ────────────────────────────────────────────
from .search import _fc_search, _fc_scrape
from .tools import _tool_fetch_json, _ast_eval, _tool_read_bot_logs, _tool_send_workspace_file, _tool_create_file
from .tg_api import _PRIVILEGED, handle_tg_tool
from .sandbox import _tool_run_python, _tool_run_shell, _tool_analyze_image, _tool_analyze_audio, _tool_playwright_browse

# ── Media tools ─────────────────────────────────────────────────
from .media import (
    _tool_search_image, _tool_generate_image, _tool_list_image_models,
    _tool_download_image, _tool_download_video, _tool_search_video,
    _tool_tts, _tool_qr_code, _tool_create_chart, _tool_translate,
)




from .prompts import _TOOLS, _build_system


async def _sonnet_call(contents: list, is_owner: bool = False) -> dict:
    import time as _t
    errors = []
    if len(contents) > 31:
        contents = [contents[0]] + contents[-30:]

    system = _build_system(is_owner)
    tools = _gemini_tools_to_openai(_TOOLS)
    messages = _gemini_contents_to_openai_messages(contents)

    for attempt in range(3):
        started = _t.monotonic()
        try:
            data = await openrouter_chat(
                messages=messages,
                system_prompt=system,
                model=OPENROUTER_TEXT_MODEL,
                max_tokens=AGENT_SONNET_MAX_TOKENS,
                tools=tools,
                timeout=AGENT_SONNET_TIMEOUT,
            )
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")
            if attempt < 2:
                await asyncio.sleep(2)
            continue

        logger.info(f"agent: Sonnet raw finish={data.get('choices',[{}])[0].get('finish_reason','?')} has_tools={bool(data.get('choices',[{}])[0].get('message',{}).get('tool_calls'))}")
        result = _openai_response_to_gemini(data)
        elapsed = _t.monotonic() - started
        function_calls = [part.get("functionCall") for part in result.get("content", {}).get("parts", []) if "functionCall" in part]
        text = " ".join(part.get("text", "") for part in result.get("content", {}).get("parts", []) if "text" in part).strip()
        if function_calls:
            logger.info(f"agent: Sonnet tool calls [{elapsed:.1f}s]: {[call['name'] for call in function_calls]}")
            return result
        if text:
            logger.info(f"agent: Sonnet text [{elapsed:.1f}s] ({len(text)} chars)")
            return result
        errors.append(f"Claude Sonnet 5: пустой ответ (finish={data.get('choices',[{}])[0].get('finish_reason','?')})")

    first = errors[0][:300] if errors else "неизвестная ошибка"
    rest = f" + ещё {len(errors) - 1}" if len(errors) > 1 else ""
    return {"_error": f"Claude Sonnet 5: {first}{rest}"}

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
        except Exception as e: logger.debug(f"status_cb suppressed: {e}")

    async def _send(m):
        if send_cb:
            try: await send_cb(m)
            except Exception as e: logger.warning(f"send_cb: {e}")

    if name in _PRIVILEGED and not is_owner:
        return "Недостаточно прав. Это действие доступно только администраторам.", None

    if e := debounce.check(name, args): return e, None
    if e := budget.charge(name):        return e, None

    if name == "think":
        thought = str(args.get("thought", ""))
        logger.info("[think] private plan recorded (%d chars)", len(thought))
        await _st("Планирую следующий шаг...")
        return "ok", None

    if name == "web_search":
        q = (args.get("query") or "").strip()
        if not q:
            return "Ошибка: пустой поисковый запрос. Уточни что искать.", None
        await _st(f"🔎 Ищу: «{html.escape(q[:80])}»")
        return await _fc_search(q), None
    if name == "scrape_url":
        url = (args.get("url") or "").strip()
        if not url:
            return "Ошибка: пустой URL. Укажи ссылку для парсинга.", None
        await _st("📄 Читаю страницу...")
        return await _fc_scrape(url), None

    if name == "generate_project":
        await _st("⚙️ Генерирую проект...")
        try:
            p = await asyncio.wait_for(generate_project_with_gemini(args.get("prompt", "")), timeout=AGENT_PROJECT_TIMEOUT)
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
        await _st(f"🐍 Запускаю Python в sandbox:\n<pre><code class=\"language-python\">{safe_code}{'…' if len(code) > 300 else ''}</code></pre>")
        return await _tool_run_python(code, ws, _st), None

    if name == "analyze_audio":
        await _st("🎧 Анализирую аудио...")
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
        await _st(f"💻 Выполняю команду в sandbox:\n<pre><code class=\"language-bash\">{safe_cmd}</code></pre>")
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

    if name == "vision":
        path = args.get("path", "")
        prompt = args.get("prompt", "Опиши это изображение.")
        import os as _os2
        if not path or not _os2.path.isfile(path):
            return f"Файл не найден: {path}. Используй путь из workspace.", None
        await _st("🔍 Анализирую изображение через NVIDIA Vision...")
        try:
            with open(path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            return f"Не могу прочитать файл: {e}", None
        from services.nvidia_vision import analyze_image
        result = await analyze_image(img_bytes, prompt)
        if result:
            return result, None
        return "Vision анализ не дал результата.", None
    return f"Unknown tool: {name}", None


# ── Public API ───────────────────────────────────────────────────

async def run_agent(
    task: str,
    chat_id: int,
    username: str,
    status_cb: Callable[[str], Any],
    send_media_cb: Optional[Callable] = None,
    is_owner: bool = False,
    initial_files: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[dict]]:

    import time as _time
    from state import chat_workspaces as _cws
    _WS_TTL = AGENT_WORKSPACE_TTL
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
        except Exception as e: logger.debug(f"status_cb suppressed: {e}")

    _TOOL_STATUS: dict[str, tuple[str, str, str]] = {
        "think": (_E["think"], "💭", "Формулирую план"),
        "web_search": (_E["search"], "🔍", "Веб-поиск"),
        "scrape_url": (_E["link"], "🔗", "Читаю страницу"),
        "fetch_json": (_E["globe"], "🌐", "Запрашиваю данные"),
        "generate_project": (_E["screen"], "🖥", "Генерирую проект"),
        "list_image_models": (_E["info"], "ℹ️", "Запрашиваю модели"),
        "generate_image": (_E["sparkle"], "✨", "Рисую"),
        "search_and_send_image": (_E["eyes"], "👀", "Ищу картинку"),
        "download_image": (_E["download"], "⬇️", "Скачиваю картинку"),
        "search_and_send_video": (_E["play"], "▶️", "Ищу видео"),
        "download_video": (_E["download"], "⬇️", "Скачиваю видео"),
        "text_to_speech": (_E["microphone"], "🎙", "Озвучиваю"),
        "run_python": (_E["screen"], "🖥", "Запускаю Python"),
        "analyze_audio": (_E["music"], "🎵", "Анализирую аудио"),
        "analyze_image": (_E["eyes"], "👀", "Анализирую фото"),
        "run_shell": (_E["screen"], "🖥", "Выполняю команду"),
        "playwright_browse": (_E["globe"], "🌐", "Открываю браузер"),
        "write_file": (_E["pencil"], "✏️", "Записываю файл"),
        "read_bot_logs": (_E["chart"], "📊", "Читаю логи бота"),
        "read_file": (_E["attachment"], "📎", "Читаю файл"),
        "calculate": (_E["idea"], "💡", "Считаю"),
        "translate": (_E["globe"], "🌐", "Перевожу"),
        "qr_code": (_E["link"], "🔗", "Генерирую QR"),
        "create_chart": (_E["growth"], "📈", "Строю график"),
        "create_file": (_E["pencil"], "✏️", "Создаю файл"),
        "send_workspace_file": (_E["attachment"], "📎", "Отправляю файл"),
        "send_with_buttons": (_E["chat"], "💬", "Отправляю кнопки"),
        "reply": (_E["chat"], "💬", "Формулирую ответ"),
    }
    _DETAIL_FIELDS = {
        "web_search": "query", "scrape_url": "url", "fetch_json": "url",
        "generate_project": "prompt", "playwright_browse": "url",
        "write_file": "path", "read_file": "path",
        "create_file": "filename", "send_workspace_file": "path",
    }
    last_tool, last_args = "think", {}
    _start_ts = _time.monotonic()

    def _fmt_status(name: str, args: dict) -> str:
        emoji_id, emoji, label = _TOOL_STATUS.get(
            name, (_E["lightning"], "⚡️", name.replace("_", " "))
        )
        elapsed = int(_time.monotonic() - _start_ts)
        minutes, seconds = divmod(elapsed, 60)
        duration = f"{minutes}м {seconds}с" if minutes else f"{seconds}с"
        detail = str(args.get(_DETAIL_FIELDS.get(name, ""), "")).strip()
        detail = " ".join(detail.split())
        if len(detail) > 120:
            detail = detail[:119] + "…"
        lines = [f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji> <b>{html.escape(label)}</b>']
        if detail:
            lines.append(f"└ <i>{html.escape(detail)}</i>")
        lines.append(
            '<tg-emoji emoji-id="5386367538735104399">⌛</tg-emoji> '
            f"<i>Работаю · {duration}</i>"
        )
        return "\n".join(lines)

    try:
        # Claude Sonnet 5 drives every agent step, including tool calls.
        for step in range(AGENT_MAX_STEPS):
            await _st(_fmt_status(last_tool, last_args))

            if AGENT_MAX_STEPS - step <= 3:
                contents.append({"role": "user", "parts": [{"text":
                    f"\n[СИСТЕМА: осталось {AGENT_MAX_STEPS - step} шагов. Завершай.]"
                }]})

            candidate = await _sonnet_call(contents, is_owner=is_owner)


            if candidate.get("_error"):
                return f"Модели недоступны: {candidate['_error']}", None

            parts = candidate.get("content", {}).get("parts", [])
            if not parts:
                return f"Агент не смог ответить. Причина: {candidate.get('_finish', 'пустой ответ')}", None

            contents.append({"role": "model", "parts": parts})
            fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not fn_calls:
                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                text = re.sub(r'\b(?:call:default_api:)?\w+\(\w+="([^"]*)"\)', r'\1', text)
                text = re.sub(r'<\w+\.?\w+="([^"]*)"\s*/?>', r'\1', text)
                text = re.sub(r'<reply[^>]*>|</reply>', '', text)
                text = text.strip()
                return text or "Done.", None

            tool_responses: list = []
            for fn in fn_calls:
                name = fn.get("name", "")
                args = fn.get("args", {})
                last_tool, last_args = name, args
                await _st(_fmt_status(name, args))
                result, project = await _execute_tool(
                    name, args, debounce, budget, status_cb, send_media_cb, ws,
                    is_owner=is_owner, chat_id=chat_id,
                )
                last_tool, last_args = name, args
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
    Uses Claude Sonnet 5 to understand ambiguous intent; explicit triggers stay local."""
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
    agent_triggers = [
        'search', 'find', 'generate', 'create', 'make', 'draw', 'send', 'download',
        'convert', 'продолжай', 'ищи дальше', 'скинь', 'дай', 'переведи',
    ]
    if any(t in prompt.lower() for t in agent_triggers):
        return True
    try:
        text = await generate_text_with_openrouter(
            prompt=prompt[:800],
            system_prompt=system,
            model=OPENROUTER_TEXT_MODEL,
            max_tokens=AGENT_CLASSIFY_MAX_TOKENS,
            timeout=AGENT_CLASSIFY_TIMEOUT,
        )
    except Exception as error:
        logger.warning(f"Agent intent classification failed: {type(error).__name__}: {error}")
        return False
    return text.strip().lower().startswith("true")
