"""
Agent orchestration loop — _sonnet_call, _execute_tool, run_agent, classify_agent_intent.

Supporting modules: workspace, safety, search, media, sandbox, tools, tg_api, prompts.
"""
import asyncio
import ipaddress
import json
import logging
import html
import os
import re
import socket
from typing import Any, Callable, Optional, Tuple
from urllib.parse import urlparse

from services.code_service import generate_project_with_gemini
from services.security_utils import is_safe_url

from services.omniroute_service import (
    analyze_image_with_omniroute,
    gemini_contents_to_openai_messages,
    gemini_tools_to_openai,
    omniroute_chat,
    omniroute_text,
    openai_response_to_gemini,
)
logger = logging.getLogger(__name__)

from config import (
    AGENT_CLASSIFY_MAX_TOKENS,
    AGENT_CLASSIFY_TIMEOUT,
    AGENT_MAX_STEPS,
    AGENT_PROJECT_TIMEOUT,
    AGENT_SONNET_MAX_TOKENS,
    AGENT_SONNET_TIMEOUT,
    NEWS_EMOJI_IDS,
    OMNIROUTE_AGENT_MODEL,
    get_cookie_path,
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
    _tool_tts, _tool_qr_code, _tool_translate,
)

from .prompts import _TOOLS, _build_system


async def _run_exec(cmd: list[str], timeout: float) -> Tuple[int, str, str]:
    """Run external binary asynchronously with a hard timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode(errors="replace"),
            stderr_b.decode(errors="replace"),
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        await proc.wait()
        raise


def _resolve_safe_host(url: str) -> Optional[Tuple[str, int, str]]:
    """Validate URL via central SSRF policy and return (host, port, ip) for pinning."""
    if not is_safe_url(url):
        return None
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return None
    for *_, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return (host, port, str(ip))
    return None


async def _sonnet_call(contents: list, is_owner: bool = False) -> dict:
    import time as _t
    errors = []
    if len(contents) > 31:
        contents = [contents[0]] + contents[-30:]

    system = _build_system(is_owner)
    tools = gemini_tools_to_openai(_TOOLS)
    messages = gemini_contents_to_openai_messages(contents)

    for attempt in range(3):
        started = _t.monotonic()
        try:
            data = await omniroute_chat(
                messages=messages,
                system_prompt=system,
                model=OMNIROUTE_AGENT_MODEL,
                max_tokens=AGENT_SONNET_MAX_TOKENS,
                tools=tools,
                timeout=AGENT_SONNET_TIMEOUT,
            )
            if not data:
                raise RuntimeError(f'OmniRoute ({OMNIROUTE_AGENT_MODEL}) не ответил')
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")
            if attempt < 2:
                await asyncio.sleep(2)
            continue

        choice = (data.get("choices") or [{}])[0]
        logger.info(
            "agent: OmniRoute %s raw finish=%s has_tools=%s",
            OMNIROUTE_AGENT_MODEL,
            choice.get("finish_reason", "?"),
            bool((choice.get("message") or {}).get("tool_calls")),
        )
        result = openai_response_to_gemini(data)
        elapsed = _t.monotonic() - started
        function_calls = [part.get("functionCall") for part in result.get("content", {}).get("parts", []) if "functionCall" in part]
        text = " ".join(part.get("text", "") for part in result.get("content", {}).get("parts", []) if "text" in part).strip()
        if function_calls:
            logger.info("agent: OmniRoute %s tool calls [%.1fs]: %s", OMNIROUTE_AGENT_MODEL, elapsed, [call['name'] for call in function_calls])
            return result
        if text:
            logger.info("agent: OmniRoute %s text [%.1fs] (%d chars)", OMNIROUTE_AGENT_MODEL, elapsed, len(text))
            return result
        errors.append(f"OmniRoute {OMNIROUTE_AGENT_MODEL}: пустой ответ (finish={choice.get('finish_reason', '?')})")

    first = errors[0][:300] if errors else "неизвестная ошибка"
    rest = f" + ещё {len(errors) - 1}" if len(errors) > 1 else ""
    return {"_error": f"OmniRoute ({OMNIROUTE_AGENT_MODEL}): {first}{rest}"}

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
        await _st(f"Ищу: «{html.escape(q[:80])}»")
        return await _fc_search(q), None
    if name == "scrape_url":
        url = (args.get("url") or "").strip()
        if not url:
            return "Ошибка: пустой URL. Укажи ссылку для парсинга.", None
        await _st("Читаю страницу...")
        return await _fc_scrape(url), None

    if name == "generate_project":
        await _st("Генерирую проект...")
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
        if not username or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username):
            return "Укажи корректный TikTok username.", None
        cookie_path = get_cookie_path("tiktok.com")
        cookie_args = ["--cookies", cookie_path] if cookie_path and os.path.exists(cookie_path) else []
        try:
            rc, stdout, stderr = await _run_exec(
                ["yt-dlp", *cookie_args, "--dump-json", "--flat-playlist",
                 "--playlist-end", "1", f"https://www.tiktok.com/@{username}"],
                timeout=180,
            )
            if rc == 0 and stdout.strip():
                first = json.loads(stdout.strip().splitlines()[0])
                channel = first.get("channel") or first.get("uploader", username)
                count = first.get("channel_follower_count") or "?"
                return (f"TikTok @{username}:\n"
                        f"Имя: {channel}\n"
                        f"Подписчиков: {count}\n"
                        f"Видео: {first.get('playlist_count','?')}\n"
                        f"Bio: {first.get('description','—')}"), None
            # Fallback: scrape via curl with cookies
            curl_cmd = ["curl", "-s"]
            if cookie_path and os.path.exists(cookie_path):
                curl_cmd += ["-b", cookie_path]
            curl_cmd += [
                "--user-agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                f"https://www.tiktok.com/api/user/detail/?uniqueId={username}&aid=1988",
            ]
            c_rc, c_out, _ = await _run_exec(curl_cmd, timeout=15)
            if c_rc == 0 and c_out.strip():
                data = json.loads(c_out)
                user = data.get("userInfo", {}).get("user", {})
                stats = data.get("userInfo", {}).get("stats", {})
                return (f"TikTok @{username} (через API):\n"
                        f"Имя: {user.get('nickname','?')}\n"
                        f"Подписчиков: {stats.get('followerCount','?')}\n"
                        f"Лайки: {stats.get('heartCount','?')}\n"
                        f"Видео: {stats.get('videoCount','?')}\n"
                        f"Bio: {user.get('signature','—')}"), None
            return f"Не смог получить данные профиля @{username}: {stderr[:200]}", None
        except asyncio.TimeoutError:
            return f"Таймаут при получении профиля @{username}.", None
        except Exception as e:
            return f"Ошибка fetch_tiktok_profile: {e}", None

    if name == "fetch_with_cookies":
        url = args.get("url", "")
        if not url:
            return "Укажи URL.", None
        resolved = _resolve_safe_host(url)
        if not resolved:
            return "Запрос к этому адресу запрещён (SSRF защита).", None
        cookie_file = get_cookie_path(url)
        _host, _port, _safe_ip = resolved
        cmd = ["curl", "-s", "--max-time", "15", "--max-redirs", "0",
               "--resolve", f"{_host}:{_port}:{_safe_ip}",
               "--user-agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"]
        if cookie_file and os.path.exists(cookie_file):
            cmd += ["-b", cookie_file]
        cmd.append(url)
        try:
            _, stdout, _ = await _run_exec(cmd, timeout=20)
            out = stdout[:6000]
            if args.get("output_format") == "json":
                try:
                    json.loads(out)
                except Exception:
                    pass
            return out or "Пустой ответ.", None
        except asyncio.TimeoutError:
            return "fetch_with_cookies error: timeout", None
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
        await _st("Запрашиваю модели OpenAI...")
        return await _tool_list_image_models(), None

    if name == "generate_image":
        await _st("Генерирую картинку...")
        return await _tool_generate_image(
            args.get("prompt", ""), _send,
            provider=args.get("provider", "gemini"),
            model=args.get("model", ""),
        ), None

    if name == "download_image":
        await _st("Скачиваю картинку...")
        return await _tool_download_image(args.get("url", ""), args.get("caption", ""), _send), None

    if name == "download_video":
        await _st("Скачиваю видео (до 2 мин)...")
        return await _tool_download_video(args.get("url", ""), args.get("caption", ""), _send), None

    if name == "text_to_speech":
        await _st("Озвучиваю...")
        return await _tool_tts(args.get("text", ""), args.get("voice", "Kore"), args.get("language", "ru-RU"), _send), None

    if name == "run_python":
        code = args.get("code", "")
        safe_code = html.escape(code[:300])
        await _st(f"Запускаю Python в sandbox:\n<pre><code class=\"language-python\">{safe_code}{'…' if len(code) > 300 else ''}</code></pre>")
        return await _tool_run_python(code, ws, _st), None

    if name == "analyze_audio":
        await _st("Анализирую аудио...")
        return await _tool_analyze_audio(
            path=args.get("path", ""), question=args.get("question", ""), ws=ws
        ), None

    if name == "analyze_image":
        await _st("Анализирую изображение...")
        return await _tool_analyze_image(
            path=args.get("path", ""), question=args.get("question", ""), ws=ws
        ), None

    if name == "run_shell":
        cmd = args.get("command", "")
        safe_cmd = html.escape(cmd[:300])
        await _st(f"Выполняю команду в sandbox:\n<pre><code class=\"language-bash\">{safe_cmd}</code></pre>")
        return await _tool_run_shell(cmd, ws, _st), None

    if name == "playwright_browse":
        await _st(f"Открываю браузер: {args.get('url', '')[:80]}...")
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
        log_text = await _tool_read_bot_logs(args.get("lines", 100))
        display = log_text[-2500:] if len(log_text) > 2500 else log_text
        safe_log = html.escape(display)
        await _st(f"<b>Логи бота:</b>\n<pre><code>{safe_log}</code></pre>")
        return log_text, None

    if name == "read_file":
        return ws.read(args.get("path", "")), None

    if name == "fetch_json":
        await _st(f"{args.get('url', '')[:60]}...")
        return await _tool_fetch_json(args.get("url", "")), None

    if name == "calculate":
        return _ast_eval(args.get("expression", "0")), None

    if name == "qr_code":
        await _st("Генерирую QR...")
        return await _tool_qr_code(args.get("text", ""), args.get("caption", ""), _send), None

    if name == "translate":
        await _st(f"Перевожу на {args.get('target_language', '?')}...")
        return await _tool_translate(args.get("text", ""), args.get("target_language", "English")), None

    if name == "send_workspace_file":
        await _st(f"Отправляю файл из workspace: {args.get('path', '')}...")
        return await _tool_send_workspace_file(
            args.get("path", ""),
            args.get("caption", ""),
            ws,
            _send,
            media_type=args.get("media_type", "auto"),
        ), None

    if name == "create_file":
        await _st(f"Создаю {args.get('filename', '')}...")
        return await _tool_create_file(
            args.get("filename", "file.txt"), args.get("content", ""),
            args.get("caption", ""), _send,
        ), None

    if name == "vision":
        path = args.get("path", "")
        prompt = args.get("prompt", "Опиши это изображение.")
        if not path:
            return "Укажи путь к файлу в workspace.", None
        try:
            safe_path = ws._safe_path(path)
        except ValueError as e:
            return f"Доступ запрещён: {e}", None
        if not os.path.isfile(safe_path):
            return f"Файл не найден: {path}. Используй путь внутри workspace.", None
        await _st("Анализирую изображение...")
        try:
            with open(safe_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            return f"Не могу прочитать файл: {e}", None
        try:
            result = await analyze_image_with_omniroute(img_bytes, prompt)
        except Exception as e:
            return f"Vision ошибка: {type(e).__name__}: {e}", None
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
    _existing = _cws.get(chat_id)
    _existing_path = _existing["path"] if _existing else ""
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

    _TOOL_STATUS: dict[str, str] = {
        "think": "Формулирую план",
        "web_search": "Веб-поиск",
        "scrape_url": "Читаю страницу",
        "fetch_json": "Запрашиваю данные",
        "generate_project": "Генерирую проект",
        "list_image_models": "Запрашиваю модели",
        "generate_image": "Рисую",
        "search_and_send_image": "Ищу картинку",
        "download_image": "Скачиваю картинку",
        "search_and_send_video": "Ищу видео",
        "download_video": "Скачиваю видео",
        "text_to_speech": "Озвучиваю",
        "run_python": "Запускаю Python",
        "analyze_audio": "Анализирую аудио",
        "analyze_image": "Анализирую фото",
        "run_shell": "Выполняю команду",
        "playwright_browse": "Открываю браузер",
        "write_file": "Записываю файл",
        "read_bot_logs": "Читаю логи бота",
        "read_file": "Читаю файл",
        "calculate": "Считаю",
        "translate": "Перевожу",
        "qr_code": "Генерирую QR",
        "create_file": "Создаю файл",
        "send_workspace_file": "Отправляю файл",
        "send_with_buttons": "Отправляю кнопки",
        "reply": "Формулирую ответ",
    }
    _DETAIL_FIELDS = {
        "web_search": "query", "scrape_url": "url", "fetch_json": "url",
        "generate_project": "prompt", "playwright_browse": "url",
        "write_file": "path", "read_file": "path",
        "create_file": "filename", "send_workspace_file": "path",
        "run_shell": "command", "run_python": "code",
    }
    last_tool, last_args = "think", {}
    _start_ts = _time.monotonic()

    def _fmt_status(name: str, args: dict) -> str:
        label = _TOOL_STATUS.get(name, name.replace("_", " "))
        elapsed = int(_time.monotonic() - _start_ts)
        minutes, seconds = divmod(elapsed, 60)
        duration = f"{minutes}м {seconds}с" if minutes else f"{seconds}с"
        detail = str(args.get(_DETAIL_FIELDS.get(name, ""), "")).strip()
        detail = " ".join(detail.split())
        if len(detail) > 120:
            detail = detail[:119] + "…"
        lines = [f'<b>{html.escape(label)}</b>']
        if detail:
            lines.append(f"└ <i>{html.escape(detail)}</i>")
        lines.append(f"<i>Работаю · {duration}</i>")
        return "\n".join(lines)

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
            # DeepSeek V4 can answer text-only, leaking its chain-of-thought into
            # content (thinking is disabled). Never trust raw text — force the
            # final answer through the reply tool, whose args.text stays clean.
            contents.append({"role": "user", "parts": [{"text":
                "[СИСТЕМА: не отвечай обычным текстом. Заверши задачу строго через инструмент reply.]"
            }]})
            continue

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


async def classify_agent_intent(prompt: str) -> bool:
    """Semantically decide via OmniRoute whether a request needs agent tools (no keyword matching)."""
    system = (
        "You decide semantically whether a Telegram message requires AI agent tools or media/file actions.\n\n"
        "Answer TRUE when the user's intent requires:\n"
        "- Running code, shell commands, calculations, browser automation, or server operations\n"
        "- Searching the internet, checking fresh facts, news, links, or social media profiles\n"
        "- Creating, editing, converting, packing, or sending files/code/HTML/archives/documents\n"
        "- Finding, downloading, generating, or editing images, videos, audio, voice, charts, or QR codes\n"
        "- Performing Telegram chat actions (polls, reactions, avatar changes, moderation, etc.)\n"
        "- Continuing a previous search, investigation, or file task\n\n"
        "Answer FALSE when the user only wants casual chat or a direct text answer from knowledge.\n\n"
        "Reply with ONLY one word: true or false"
    )
    try:
        text = await omniroute_text(
            prompt=prompt[:800],
            system_prompt=system,
            model=OMNIROUTE_AGENT_MODEL,
            max_tokens=AGENT_CLASSIFY_MAX_TOKENS,
            timeout=AGENT_CLASSIFY_TIMEOUT,
        )
        if not text:
            return False
    except Exception as error:
        logger.warning(f"Agent intent classification failed: {type(error).__name__}: {error}")
        return False
    return text.strip().lower().startswith("true")


async def classify_media_mode(prompt: str, filename: str = "") -> str:
    """Semantically classify an attached image/video request: 'vision' (inspect/describe) vs 'agent' (process/edit/act)."""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return "vision"
    system = (
        "The user attached an image or video file to a Telegram message. Decide what they want done with it.\n\n"
        "Answer VISION when the user only wants to look at, describe, explain, read text from, translate text on, "
        "identify, rate, or answer questions about the attached image/video.\n\n"
        "Answer AGENT when the user wants to perform an action or tool operation on or using the file: "
        "edit, crop, resize, convert format, extract frames/audio, apply filters, compress, archive/zip, "
        "send back as a file/document/sticker/gif, set as chat/bot avatar, run Python/ffmpeg on it, "
        "or do web search / multi-step work.\n\n"
        "Reply with ONLY one word: vision or agent"
    )
    user_msg = f"Filename: {filename or 'media'}\nUser request: {clean_prompt[:800]}"
    try:
        text = await omniroute_text(
            prompt=user_msg,
            system_prompt=system,
            model=OMNIROUTE_AGENT_MODEL,
            max_tokens=AGENT_CLASSIFY_MAX_TOKENS,
            timeout=AGENT_CLASSIFY_TIMEOUT,
        )
        if text and text.strip().lower().startswith("agent"):
            return "agent"
    except Exception as error:
        logger.warning(f"Media mode classification failed: {type(error).__name__}: {error}")
    return "vision"
