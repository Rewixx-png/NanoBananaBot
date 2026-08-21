import logging
from aiogram import Bot
from config import CHAT_ID, ALLOWED_USER_IDS, BANNED_USER_IDS, TEXT_ONLY_CHAT_ID, FULL_ACCESS_CHAT_ID, ADMIN_IDS

def is_banned(user_id: int) -> bool:
    return user_id in BANNED_USER_IDS

def make_safe_caption(prefix: str, prompt: str) -> str:
    max_len = 1024
    if len(prefix) + len(prompt) <= max_len:
        return f"{prefix}{prompt}"
    allowed_prompt_len = max_len - len(prefix) - 3
    if allowed_prompt_len > 0:
        return f"{prefix}{prompt[:allowed_prompt_len]}..."
    else:
        return prefix[:max_len]

async def check_membership(bot: Bot, user_id: int, chat_id: int=None) -> bool:
    if user_id in ADMIN_IDS:
        return True
    if user_id in BANNED_USER_IDS:
        return False
    if user_id in ALLOWED_USER_IDS:
        return True
    if chat_id in (TEXT_ONLY_CHAT_ID, FULL_ACCESS_CHAT_ID):
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHAT_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logging.error(f'Ошибка проверки подписки: {e}')
        return False


import html as _html
from html.parser import HTMLParser as _HTMLParser


class _HTMLSanitizer(_HTMLParser):
    def __init__(self, allowed_tags, allowed_attrs, allowed_protocols):
        super().__init__(convert_charrefs=False)
        self.allowed_tags = set(tag.lower() for tag in (allowed_tags or []))
        self.allowed_attrs = {tag.lower(): set(attrs) for tag, attrs in (allowed_attrs or {}).items()}
        self.allowed_protocols = set(proto.lower() for proto in (allowed_protocols or []))
        self.out = []

    def handle_starttag(self, tag, attrs):
        self._handle_tag(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._handle_tag(tag, attrs, True)

    def _handle_tag(self, tag, attrs, self_closing):
        tag = tag.lower()
        if tag not in self.allowed_tags:
            return
        valid_attrs = []
        for k, v in attrs:
            k = k.lower()
            if k in self.allowed_attrs.get(tag, set()):
                if v is not None and k in ('href', 'src', 'url'):
                    val_str = v.strip()
                    if ':' in val_str and not val_str.startswith(('/', '#')):
                        proto = val_str.split(':', 1)[0].lower()
                        if proto not in self.allowed_protocols:
                            continue
                valid_attrs.append((k, v))
        attr_str = ''.join(f' {k}="{_html.escape(v, quote=True)}"' if v is not None else f' {k}' for k, v in valid_attrs)
        close_str = ' /' if self_closing else ''
        self.out.append(f'<{tag}{attr_str}{close_str}>')

    def handle_endtag(self, tag):
        if tag.lower() in self.allowed_tags:
            self.out.append(f'</{tag.lower()}>')

    def handle_data(self, data):
        self.out.append(data)

    def handle_entityref(self, name):
        self.out.append(f'&{name};')

    def handle_charref(self, name):
        self.out.append(f'&#{name};')


def clean_html(text: str, tags: list, attributes: dict = None, protocols: list = None, strip: bool = True) -> str:
    if not text:
        return ''
    s = _HTMLSanitizer(tags, attributes, protocols or ['http', 'https', 'mailto', 'tel', 'tg'])
    s.feed(text)
    return ''.join(s.out)


_http_session = None


async def get_http_session():
    """Lazy-init shared persistent HTTP session for API requests."""
    global _http_session
    import aiohttp
    if hasattr(aiohttp.ClientSession, "return_value") or hasattr(aiohttp.ClientSession, "mock_calls"):
        return aiohttp.ClientSession()
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300, force_close=False)
        timeout = aiohttp.ClientTimeout(total=120, connect=15, sock_read=30)
        _http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _http_session


async def close_http_session():
    """Gracefully close the shared HTTP session on bot shutdown."""
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None


async def run_ffmpeg(args: list[str], timeout: int = 30) -> tuple[int, bytes, bytes]:
    """Run an ffmpeg/ffprobe command asynchronously with timeout and return (returncode, stdout, stderr)."""
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, stdout, stderr
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise TimeoutError(f"Command {' '.join(args[:2])}... timed out after {timeout}s")
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Binary {args[0] if args else 'ffmpeg'} not found in PATH") from e