import asyncio
import json
import logging
import re
import aiohttp
from datetime import datetime
from typing import Tuple

from config import (
    BOT_USERNAMES,
    DEEPSEEK_MODEL,
    FIRECRAWL_SCRAPE_URL,
    FIRECRAWL_SEARCH_URL,
    JINA_READER_BASE_URL,
)
from keys import load_firecrawl_keys, remove_key, strip_code_fences
from services.deepseek_service import deepseek_text
from services.security_utils import is_safe_url
from shared_types import _WEB_SEARCH_DIRECTIVE


# ── Web search implementation ─────────────────────────────────────────────
_FIRECRAWL_DEAD_KEY_STATUSES = {401, 402}
_FIRECRAWL_TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}


def _strip_bot_mentions(text: str, repl: str = '') -> str:
    if not BOT_USERNAMES:
        return text
    pattern = r'@(?:' + '|'.join(re.escape(u.lstrip('@')) for u in BOT_USERNAMES if u) + r')\b'
    return re.sub(pattern, repl, text, flags=re.IGNORECASE)


def _clean_web_query(prompt: str) -> str:
    cleaned = re.sub(r'^\[Контекст — ты написал ранее: «.*?»\]\s*', '', prompt.strip(), flags=re.DOTALL).strip()
    cleaned = _strip_bot_mentions(cleaned, '').strip()
    # Strip trigger verbs at the start
    cleaned = re.sub(
        r'^(поищи(те)?|найди(те)?|загугли(те)?|погугли(те)?|ищи|поиск|найди|поисковый запрос)[,:\s]+',
        '', cleaned, flags=re.IGNORECASE,
    ).strip()
    # Strip filler phrases like "фулл инфу на", "инфо о", "информацию про", etc.
    cleaned = re.sub(
        r'(фулл\s+)?(инфу|инфо|информацию|данные|сведения)\s+(на|про|об?|по|о)\s+',
        '', cleaned, flags=re.IGNORECASE,
    ).strip()
    # Strip location noise: "в инете", "в интернете", "онлайн", "мне", "нам"
    cleaned = re.sub(
        r'\b(в\s+(инете|интернете|сети)|онлайн|мне|нам)\b\s*',
        '', cleaned, flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r'\b(поищи(те)?|найди(те)?|загугли(те)?|погугли(те)?|ищи)\b\s*(на|про|по|о)?\s*', '', cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'\b(инфу|инфо|информацию|данные|сведения)\b\s*(на|про|по|о)?\s*', '', cleaned, flags=re.IGNORECASE).strip()
    # Strip trailing/leading punctuation leftovers
    cleaned = cleaned.strip(',:;-— ')
    lowered = cleaned.lower()
    if any(word in lowered for word in ['gemini', 'гемини', 'джемини']) and any(word in lowered for word in ['нового', 'новости', 'обнов', 'latest', 'release']):
        return f'site:gemini.google/release-notes Gemini release updates latest Google AI {datetime.now().year}'
    return cleaned or prompt.strip()


def _dedupe_texts(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        value = re.sub(r'\s+', ' ', str(item)).strip().strip(',:;-— ')
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _extract_search_targets(text: str) -> list[str]:
    cleaned = _strip_bot_mentions(text or '', ' ')
    candidates = []
    for quoted in re.findall(r'["«“]([^"»”]{3,80})["»”]', cleaned):
        if re.search(r'[A-Za-zА-Яа-яЁё0-9]', quoted):
            candidates.append(quoted)
    for mention in re.findall(r'@[A-Za-zА-Яа-яЁё0-9_]{3,64}', cleaned):
        candidates.append(mention[1:])
    for token in re.findall(r'(?<![\w@])[A-Za-zА-Яа-яЁё0-9][\w.-]{2,79}(?![\w.-])', cleaned, flags=re.UNICODE):
        token = token.strip('.,:;!?()[]{}<>"\'«»“”')
        low = token.casefold()
        if low in {'инете', 'интернете', 'поиск', 'поищи', 'найди', 'инфу', 'инфо', 'online', 'social', 'telegram'}:
            continue
        if not ('.' in token or '_' in token or '-' in token or re.search(r'\d', token)):
            continue
        if not re.search(r'[A-Za-zА-Яа-яЁё]', token):
            continue
        candidates.append(token)
    return _dedupe_texts(candidates)[:3]


def _looks_like_account_target(target: str) -> bool:
    value = target.strip().lstrip('@')
    return bool(re.search(r'[_-]|\d', value) or ('.' in value and value != value.casefold()) or (len(value) > 3 and value.upper() == value))


def _web_query_variants(query: str) -> list[str]:
    cleaned = _clean_web_query(query)
    variants = []
    targets = _extract_search_targets(query) or _extract_search_targets(cleaned)
    for target in targets[:2]:
        exact = f'"{target}"'
        if _looks_like_account_target(target):
            variants.extend([
                f'{exact} telegram OR vk OR social',
                exact,
                f'{exact} telegram',
                f'{exact} vk',
                f'{exact} tiktok OR instagram OR youtube',
                f'site:t.me {exact}',
                f'site:vk.com {exact}',
                f'site:x.com OR site:twitter.com {exact}',
                f'site:instagram.com {exact}',
                f'site:tiktok.com {exact}',
                f'site:youtube.com {exact}',
                f'site:reddit.com {exact}',
                f'site:github.com OR site:gitlab.com {exact}',
                f'site:habr.com OR site:dtf.ru OR site:vc.ru {exact}',
                f'site:boosty.to OR site:donationalerts.com {exact}',
                f'site:lolz.live OR site:zelenka.guru {exact}',
                f'{exact} -fxnetworks -fxnow -disney',
            ])
        else:
            variants.extend([
                exact,
                f'{exact} official',
                f'{exact} news OR новости',
                f'{exact} site:reddit.com OR site:youtube.com',
                f'{exact} site:habr.com OR site:vc.ru OR site:dtf.ru',
                cleaned,
            ])
    variants.append(cleaned)
    return _dedupe_texts(variants)[:18]


def _web_context_quality(query: str, context: str) -> tuple[int, str]:
    body = (context or '').casefold()
    if len(body.strip()) < 250:
        return (0, 'thin')
    targets = _extract_search_targets(query)
    score = 0
    if not targets:
        return (55 if len(body.strip()) >= 600 else 35, 'generic')
    for target in targets[:2]:
        target_cf = target.casefold().strip('"\'')
        if target_cf and target_cf in body:
            score = max(score, 50)
        chunks = [c for c in re.split(r'[^0-9a-zа-яё]+', target_cf) if len(c) >= 2]
        if chunks and chunks[0] in body and sum((chunk in body for chunk in chunks)) >= min(2, len(chunks)):
            score = max(score, 35)
        if '.fx' in target_cf and any(term in body for term in ['fxnetworks.com', 'fx networks', 'the bear', 'wrexham', 'television network']):
            score -= 35
    if len(body) >= 1000:
        score += 15
    if body.count('http') >= 1:
        score += 10
    if context.count('---') >= 1:
        score += 10
    if any(term in body for term in ['official', 'profile', 'telegram', 'vk', 'github', 'gitlab', 'instagram', 'youtube', 'tiktok', 'linkedin', 'reddit', 'twitter', 'x.com', 'habr', 'vc.ru', 'dtf', 'boosty']):
        score += 10
    return (max(0, min(score, 100)), 'ok' if score >= 55 else 'weak')


def _protect_search_targets(query: str, source: str) -> str:
    targets = _extract_search_targets(source)
    if not targets:
        return query
    query_cf = (query or '').casefold()
    if any(target.casefold() in query_cf for target in targets):
        return query
    variants = _web_query_variants(source)
    return variants[0] if variants else query


async def _extract_search_query(user_message: str) -> str:
    system = (
        'Ты генератор поисковых запросов. '
        'Из сообщения пользователя пойми суть и верни ТОЛЬКО поисковый запрос — '
        '2-10 слов, без объяснений, без лишних слов. '
        'Запрос должен быть оптимизирован для поиска в Google/Bing. '
        'Если в сообщении есть конкретное имя, ник, продукт — обязательно включи его точно. '
        'Если объект похож на ник/username/уникальный идентификатор с точками, подчёркиваниями, цифрами или странным регистром — сохрани его в двойных кавычках. '
        'Если ищут человека или аккаунт, добавь telegram OR vk OR social. '
        'Никаких markdown, никаких пояснений — только сам запрос.'
    )
    q = await deepseek_text(
        user_message, system_prompt=system,
        model=DEEPSEEK_MODEL, temperature=0.3, max_tokens=40, timeout=8,
    )
    if q:
        q = re.sub(r'\s+', ' ', q).strip()
        if q and len(q) < 200:
            return _protect_search_targets(q, user_message)
    return _protect_search_targets(_clean_web_query(user_message), user_message)


def _parse_query_plan(raw: str) -> list[str]:
    text = strip_code_fences(raw or '').strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = parsed.get('queries') or parsed.get('search_queries') or []
        if isinstance(parsed, list):
            return _dedupe_texts([str(item) for item in parsed])[:16]
    except Exception:
        pass
    lines = re.split(r'[\n;]+', text)
    cleaned = [re.sub(r'^\s*[-*\d.)]+\s*', '', line).strip() for line in lines]
    return _dedupe_texts(cleaned)[:16]


async def _plan_firecrawl_queries(user_request: str, seed_query: str) -> list[str]:
    source = user_request.strip() or seed_query.strip()
    fallback = _dedupe_texts(_web_query_variants(source) + _web_query_variants(seed_query))[:18]
    system = (
        'Ты планировщик веб-поиска Firecrawl. Получишь сырой запрос пользователя и начальный смысл. '
        'Составь 8-12 точных поисковых запросов для Firecrawl, чтобы найти достоверную инфу по разным площадкам. '
        'Не копируй сырой текст пользователя целиком. Убирай мусор: обращения к боту, "поищи", "инфу", "в инете". '
        'Если есть ник, username, код, доменоподобная строка или идентификатор — ищи его в кавычках. '
        'Обязательно смешивай источники: общий web, Telegram, VK, X/Twitter, Instagram, TikTok, YouTube, Reddit, GitHub/GitLab, Habr/VC/DTF, Boosty/DonationAlerts, форумы. '
        'Если первые результаты могут спутаться с брендом/сериалом/сайтом — добавь запросы с минус-словами и source/site-операторами. '
        'Верни только JSON-массив строк.'
    )
    user_text = (
        f'Сырой запрос пользователя: {source}\n'
        f'Начальный смысл: {seed_query}\n'
        f'Цели/ники: {", ".join(_extract_search_targets(source) or _extract_search_targets(seed_query)) or "не выделены"}\n'
        f'Базовые варианты: {json.dumps(fallback, ensure_ascii=False)}'
    )
    try:
        raw = await deepseek_text(
            user_text,
            system_prompt=system,
            model=DEEPSEEK_MODEL,
            temperature=0.2,
            max_tokens=512,
            timeout=12,
        )
        if raw:
            planned = [_protect_search_targets(q, source) for q in _parse_query_plan(raw)]
            planned = _dedupe_texts(planned + fallback)[:18]
            if planned:
                return planned
    except Exception as e:
        logging.warning(f'Firecrawl query planner failed: {type(e).__name__}: {e}')
    return fallback


def _clean_web_snippet(text: str) -> str:
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'[*_`>\\]+', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


async def _firecrawl_scrape_url(url: str, keys: list[str]) -> str:
    if not is_safe_url(url):
        return ''
    endpoint = FIRECRAWL_SCRAPE_URL
    payload = {
        'url': url,
        'formats': ['markdown'],
        'onlyMainContent': True,
        'removeBase64Images': True,
        'maxAge': 4 * 60 * 60 * 1000,
        'storeInCache': True,
    }
    for key in (keys or []):
        headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        d = data.get('data') or data
                        text = (d.get('markdown') or d.get('content') or '').strip()
                        if text:
                            return text
                    if resp.status in _FIRECRAWL_DEAD_KEY_STATUSES:
                        await remove_key(key, resp.status)
                        continue
                    if resp.status in _FIRECRAWL_TRANSIENT_STATUSES:
                        logging.warning(f'Firecrawl scrape transient HTTP {resp.status} for {url}; key kept alive')
                        continue
                    body = await resp.text()
                    logging.warning(f'Firecrawl scrape request HTTP {resp.status} for {url}: {body[:300]}')
        except Exception as e:
            logging.warning(f'Firecrawl scrape {url}: {type(e).__name__}')

    # Fallback: Jina Reader (free, no key needed)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f'{JINA_READER_BASE_URL}/{url}',
                headers={'Accept': 'text/plain', 'X-Return-Format': 'markdown', 'User-Agent': 'Mozilla/5.0'},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    text = (await resp.text()).strip()
                    if len(text) > 200:
                        return text
    except Exception as e:
        logging.debug(f'Jina scrape fallback {url}: {type(e).__name__}')
    return ''


def _extract_links_from_markdown(md: str) -> list[str]:
    urls = re.findall(r'https?://[^\s\)\]\"\'>]+', md)
    seen, result = set(), []
    skip_ext = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js', '.ico', '.woff', '.mp4', '.webp')
    for u in urls:
        u = u.rstrip('.,;):')
        if u in seen or any(u.lower().endswith(e) for e in skip_ext):
            continue
        seen.add(u)
        result.append(u)
    return result[:10]


async def search_web_with_firecrawl(query: str, status_cb=None, raw_request: str='') -> Tuple[str, bool]:
    initial_keys = await load_firecrawl_keys()

    async def _st(text: str):
        if status_cb:
            try:
                await status_cb(text)
            except Exception:
                pass

    async def _refine_queries(contexts: list[str], attempted: list[str]) -> list[str]:
        source = raw_request or query
        context_digest = '\n---\n'.join(_clean_web_snippet(c[:1800]) for c in contexts[-4:] if c)[:6000]
        system = (
            'Ты web-research агент. По сырому запросу пользователя, уже проверенным запросам и найденным кускам страниц составь новые поисковые запросы. '
            'Ищи как человек: если нашёл профиль — ищи его следы, упоминания, соцсети, репозитории, видео, форумы, русскоязычные площадки, связанные ссылки. '
            'Не повторяй старые запросы. Возвращай только JSON-массив из 4-8 строк.'
        )
        user_text = (
            f'Сырой запрос: {source}\n'
            f'Начальный запрос: {query}\n'
            f'Уже пробовали: {json.dumps(attempted[-12:], ensure_ascii=False)}\n'
            f'Найденные фрагменты:\n{context_digest}'
        )
        try:
            raw = await deepseek_text(
                user_text,
                system_prompt=system,
                model=DEEPSEEK_MODEL,
                temperature=0.35,
                max_tokens=512,
                timeout=15,
            )
            if raw:
                refined = [_protect_search_targets(q, source) for q in _parse_query_plan(raw)]
                return [q for q in _dedupe_texts(refined) if q.casefold() not in {a.casefold() for a in attempted}][:8]
        except Exception as e:
            logging.warning(f'Firecrawl query refiner failed: {type(e).__name__}: {e}')
        return []

    async def _run_query(search_query: str) -> Tuple[str, bool]:
        raw_results = []
        api_available = False
        query_keys: list[str] = []

        # Primary: DuckDuckGo (free, no key)
        try:
            from ddgs import DDGS
            ddg_results = await asyncio.get_event_loop().run_in_executor(
                None, lambda: list(DDGS().text(search_query[:500], max_results=8))
            )
            if ddg_results:
                api_available = True
                raw_results = [
                    {'url': r.get('href', ''), 'title': r.get('title', ''),
                     'description': r.get('body', '')}
                    for r in ddg_results if r.get('href')
                ]
        except Exception as e:
            logging.warning(f'DDG search error: {type(e).__name__}: {e}')

        # Fallback: Firecrawl if keys available and DDG failed
        if not raw_results:
            query_keys = await load_firecrawl_keys()
            for key in query_keys:
                headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
                payload = {'query': search_query[:500], 'limit': 8, 'sources': [{'type': 'web'}]}
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(FIRECRAWL_SEARCH_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                            if resp.status == 200:
                                api_available = True
                                data = await resp.json()
                                raw_results = data.get('data', [])
                                if isinstance(raw_results, dict):
                                    raw_results = raw_results.get('results', []) or raw_results.get('web', []) or []
                                break
                            if resp.status in _FIRECRAWL_DEAD_KEY_STATUSES:
                                await remove_key(key, resp.status)
                                continue
                except Exception as e:
                    logging.warning(f'Firecrawl search error: {type(e).__name__}: {e}')

        if not raw_results:
            return ('', api_available)

        top_items = [r for r in raw_results if isinstance(r, dict) and r.get('url')][:8]
        top_urls = [r['url'] for r in top_items]
        if not top_urls:
            return ('', api_available)

        await _st(f'Читаю страниц: {len(top_urls)}...')
        scrape_keys = await load_firecrawl_keys() or query_keys or initial_keys
        scraped_pages = await asyncio.gather(*[_firecrawl_scrape_url(u, scrape_keys) for u in top_urls], return_exceptions=True)

        parts = []
        candidate_links = []

        for i, item in enumerate(top_items):
            url = item.get('url', '')
            title = item.get('title') or item.get('metadata', {}).get('title') or url
            desc = str(item.get('description') or item.get('metadata', {}).get('description') or '').strip()
            scraped_result = scraped_pages[i]
            scraped = scraped_result if isinstance(scraped_result, str) else ''

            if len(scraped.strip()) >= 300:
                content = _clean_web_snippet(scraped[:7000])
                parts.append(f'{title}\n{url}\n{content}')
                candidate_links.extend(_extract_links_from_markdown(scraped)[:6])
            elif desc:
                parts.append(f'{title}\n{url}\n{desc}')
                candidate_links.extend(_extract_links_from_markdown(scraped)[:3])

        if len('\n'.join(parts)) < 5000 and candidate_links:
            await _st('Проверяю связанные страницы...')
            seen_urls = set(top_urls)
            depth2_urls = []
            for u in candidate_links:
                if u not in seen_urls and len(depth2_urls) < 6:
                    seen_urls.add(u)
                    depth2_urls.append(u)

            if depth2_urls:
                scrape_keys = await load_firecrawl_keys() or scrape_keys
                depth2_pages = await asyncio.gather(*[_firecrawl_scrape_url(u, scrape_keys) for u in depth2_urls], return_exceptions=True)
                depth3_candidates = []
                for url, page_md in zip(depth2_urls, depth2_pages):
                    if not isinstance(page_md, str) or not page_md or len(page_md.strip()) < 150:
                        continue
                    parts.append(f'{url}\n{_clean_web_snippet(page_md[:5000])}')
                    depth3_candidates.extend(_extract_links_from_markdown(page_md)[:4])
                if len('\n'.join(parts)) < 9000 and depth3_candidates:
                    await _st('Проверяю ещё один уровень ссылок...')
                    seen_urls.update(depth2_urls)
                    depth3_urls = []
                    for u in depth3_candidates:
                        if u not in seen_urls and len(depth3_urls) < 3:
                            seen_urls.add(u)
                            depth3_urls.append(u)
                    if depth3_urls:
                        scrape_keys = await load_firecrawl_keys() or scrape_keys
                        depth3_pages = await asyncio.gather(*[_firecrawl_scrape_url(u, scrape_keys) for u in depth3_urls], return_exceptions=True)
                        for url, page_md in zip(depth3_urls, depth3_pages):
                            if not isinstance(page_md, str) or not page_md or len(page_md.strip()) < 150:
                                continue
                            parts.append(f'{url}\n{_clean_web_snippet(page_md[:4000])}')

        if not parts:
            return ('', api_available)
        return ('\n\n---\n\n'.join(parts), api_available)

    any_available = bool(initial_keys)
    best_context = ''
    weak_contexts = []
    relevant_contexts = []
    usable_contexts = []
    seen_context_urls = set()
    seen_context_digests = set()
    attempted = []

    def _remember_context(context: str) -> bool:
        urls = set(re.findall(r'https?://[^\s\)\]"\'>]+', context or ''))
        digest = _clean_web_snippet((context or '')[:900]).casefold()
        if urls and urls.issubset(seen_context_urls):
            return False
        if not urls and digest in seen_context_digests:
            return False
        seen_context_urls.update(urls)
        if digest:
            seen_context_digests.add(digest)
        return True

    def _merge_contexts(contexts: list[str], limit: int = 16000) -> str:
        chunks = []
        size = 0
        for context in contexts:
            if not context or size >= limit:
                continue
            remaining = limit - size
            chunk = context[:remaining]
            chunks.append(chunk)
            size += len(chunk)
        return '\n\n---\n\n'.join(chunks)

    variants = await _plan_firecrawl_queries(raw_request or query, query)
    if variants:
        await _st(f'Составил запросов: {len(variants)}.')
    queue = list(variants)
    max_attempts = 16
    while queue and len(attempted) < max_attempts:
        variant = queue.pop(0)
        if variant.casefold() in {a.casefold() for a in attempted}:
            continue
        attempted.append(variant)
        await _st(f'Запрос {len(attempted)}/{max_attempts}: «{variant[:80]}»')
        context, available = await _run_query(variant)
        any_available = any_available or available
        if len(context) > len(best_context):
            best_context = context
        quality, reason = _web_context_quality(raw_request or query, context)
        if context and quality >= 40 and _remember_context(context):
            usable_contexts.append(context)
        if context and quality >= 55:
            if context not in relevant_contexts:
                relevant_contexts.append(context)
            await _st(f'Источник подходит ({quality}/100). Ищу ещё...')
            if len(relevant_contexts) >= 3 or len('\n\n---\n\n'.join(relevant_contexts)) >= 14000:
                await _st(f'Собрал источников: {len(relevant_contexts)}. Готовлю ответ...')
                return (_merge_contexts(relevant_contexts), True)
            refined = await _refine_queries(relevant_contexts + weak_contexts, attempted)
            if refined:
                queue.extend(refined)
            continue
        if context:
            weak_contexts.append(context)
            await _st(f'Результат слабый ({quality}/100). Пробую другой запрос...')
            logging.info(f'Firecrawl context rejected as {reason} for {query!r} via {variant!r}, quality={quality}')
        if len(attempted) in {4, 8, 12} and (weak_contexts or relevant_contexts):
            refined = await _refine_queries(relevant_contexts + weak_contexts, attempted)
            if refined:
                await _st(f'Составил ещё запросов: {len(refined)}.')
                queue.extend(refined)

    if relevant_contexts:
        await _st(f'Собрал источников: {len(relevant_contexts)}. Готовлю ответ...')
        extra = [c for c in usable_contexts if c not in relevant_contexts]
        return (_merge_contexts(relevant_contexts + extra), True)
    if usable_contexts:
        await _st('Собрал несколько источников. Готовлю ответ...')
        return (_merge_contexts(usable_contexts), True)
    if any_available:
        return ('', True)
    return (best_context, False)


def _fallback_web_answer(query: str, web_context: str) -> str:
    chunks = [part.strip() for part in web_context.split('---') if part.strip()]
    if not chunks:
        return 'Интернет открылся, но нормальной инфы не вытащил.'
    clean_query = _clean_web_query(query)
    lines = [f'Разжевал, что нашёл по «{clean_query}»:']
    for chunk in chunks[:3]:
        raw_lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        title = _clean_web_snippet(raw_lines[0]) if raw_lines else 'Источник'
        url = raw_lines[1] if len(raw_lines) > 1 and raw_lines[1].startswith('http') else ''
        body_start = 2 if url else 1
        body = _clean_web_snippet(' '.join(raw_lines[body_start:]))[:650]
        if not body:
            continue
        sentences = re.split(r'(?<=[.!?])\s+', body)
        digest = ' '.join(sentences[:2]).strip() or body[:260]
        item = f'• {title}: {digest}'
        if url:
            item += f'\n  {url}'
        lines.append(item)
    return '\n'.join(lines)


async def synthesize_web_answer(prompt: str, web_context: str) -> str:
    if not web_context or len(web_context.strip()) < 80:
        return f'Я искал «{_clean_web_query(prompt)}» — поиск вернул пустоту. Либо чел не светится в паблике, либо Firecrawl ничего не нашёл. Больше нихуя.'
    clean_query = _clean_web_query(prompt)
    system = (
        'Ты Hatani AI. Тебе дали результаты Firecrawl. Ответь пользователю по-русски, коротко и живо, в своём токсичном стиле.\n\n'
        'ЖЁСТКИЕ ПРАВИЛА — нарушение любого = плохой ответ:\n'
        '- Используй ТОЛЬКО факты из Firecrawl-контекста ниже. Не вспоминай ничего из памяти.\n'
        '- Никогда не выдумывай URL, коды ошибок, названия страниц или имена людей которых нет в контексте.\n'
        '- Если контекст не содержит ничего конкретного по запросу — честно скажи "нихуя конкретного не нашёл" в своём стиле. Не фантазируй.\n'
        '- Никаких ** жирных **, * курсивов *, # заголовков, ``` блоков — только обычный текст.\n'
        '- Не печатай WEB_SEARCH.\n'
        '- Не копируй сырой текст страниц простынёй.\n'
        '- Пиши связным текстом или нумерованным списком с цифрами (1. 2. 3.), не с символами * или -.\n'
        '- Ссылки вставляй голым URL-ом, только 1-2 самых полезных.'
    )
    user_text = f'Запрос пользователя: {clean_query}\n\nFirecrawl-контекст:\n{web_context[:14000]}'
    answer = await deepseek_text(
        user_text, system_prompt=system,
        model=DEEPSEEK_MODEL, temperature=0.3, max_tokens=800, timeout=25,
    )
    if answer and not answer.upper().startswith(_WEB_SEARCH_DIRECTIVE):
        return answer
    return _fallback_web_answer(prompt, web_context)
