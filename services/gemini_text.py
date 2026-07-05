import asyncio
import logging
import aiohttp
from typing import Optional, Any

from config import GEMINI_TEXT_TIMEOUT, MAX_HISTORY_MESSAGES
from database import get_history, save_history
from keys import load_keys, remove_key, strip_code_fences
from services.web_search import search_web_with_firecrawl, synthesize_web_answer, _extract_search_query, _fallback_web_answer
# ── Shared helpers moved to shared_types.py ───────────────────────────────
from shared_types import (
    _WEB_SEARCH_DIRECTIVE, _KICK_DIRECTIVE, _TEXT_MODEL_FALLBACKS,
    _models_cache, _MODELS_CACHE_TTL, _pretty_model_name,
    _thinking_config, _build_text_system_prompt,
)

logger = logging.getLogger(__name__)
# ── Gemini text service ───────────────────────────────────────────────────

def _needs_web_lookup(prompt: str) -> bool:
    lowered = prompt.lower()
    triggers = [
        'найди', 'загугли', 'поищи', 'посмотри в интернете', 'в интернете', 'актуаль', 'сейчас',
        'сегодня', 'новости', 'курс', 'цена', 'погода', 'последн', 'latest', 'current', 'today',
        'search', 'google', 'web', 'internet', 'news', 'price', 'weather', 'rate',
    ]
    return any(t in lowered for t in triggers)


def _is_explicit_web_lookup(prompt: str) -> bool:
    lowered = prompt.lower()
    explicit_triggers = ['поищи', 'найди', 'загугли', 'посмотри в интернете', 'посмотри в инете', 'в интернете', 'в инете', 'что нового', 'latest', 'current', 'search web']
    return any(t in lowered for t in explicit_triggers)


async def generate_text_with_gemini(prompt: str, chat_id: int, username: str='', web_query: str='', status_cb=None, allow_web: bool=True, is_owner: bool=False) -> str:
    from state import chat_context_buffer
    history = await get_history(chat_id)
    prefixed_prompt = f'[{username}]: {prompt}' if username else prompt
    # web_query is the clean user message without reply context prefix — use it for search/synthesis
    _wq = web_query.strip() if web_query else prompt

    async def _status(text: str):
        if status_cb:
            try:
                await status_cb(text)
            except Exception:
                pass

    web_context = ''
    explicit_web_lookup = _is_explicit_web_lookup(_wq)

    # Groq fast path — for simple chat without web search, use GPT-OSS-120B
    if not explicit_web_lookup and not _needs_web_lookup(_wq):
        try:
            from services.groq_service import generate_text_with_groq
            system = _build_text_system_prompt(allow_web_directive=allow_web, is_owner=is_owner)
            # Inject recent context
            ctx_lines = chat_context_buffer.get(chat_id, [])[-8:]
            ctx_text = '\n'.join(ctx_lines) if ctx_lines else ''
            groq_prompt = f'[История чата — последние сообщения]:\n{ctx_text}\n\n[Пользователь]: {prefixed_prompt}' if ctx_text else prefixed_prompt
            result = await generate_text_with_groq(groq_prompt, system_prompt=system, temperature=0.8)
            if result:
                history.append({'role': 'user', 'text': prefixed_prompt})
                history.append({'role': 'model', 'text': result})
                if len(history) > MAX_HISTORY_MESSAGES:
                    history = history[-MAX_HISTORY_MESSAGES:]
                await save_history(chat_id, history)
                return result
        except Exception:
            pass

    keys = await load_keys()
    if not keys:
        return 'Блять, ключи закончились, иди нахуй.'
    if allow_web and _needs_web_lookup(_wq):
        clean_q = await _extract_search_query(_wq)
        await _status(f'🔍 Ищу в инете: «{clean_q[:80]}»...')
        try:
            web_context, web_available = await asyncio.wait_for(
                search_web_with_firecrawl(clean_q, status_cb=status_cb, raw_request=_wq),
                timeout=210,
            )
        except asyncio.TimeoutError:
            logging.warning(f'Firecrawl search timed out after 210s for {clean_q!r}')
            web_context, web_available = ('', True)
        if not web_available:
            return 'Не могу сейчас зайти в интернет — все Firecrawl ключи сдохли или отвалились.'
        if explicit_web_lookup:
            src_count = web_context.count('---') + 1 if web_context else 0
            await _status(f'🧠 Нашёл {src_count} источника(-ов), синтезирую ответ...')
            answer = await synthesize_web_answer(clean_q, web_context)
            history.append({'role': 'user', 'text': prefixed_prompt})
            history.append({'role': 'model', 'text': answer})
            if len(history) > MAX_HISTORY_MESSAGES:
                history = history[-MAX_HISTORY_MESSAGES:]
            await save_history(chat_id, history)
            return answer

    _PROHIBITED = '__PROHIBITED_CONTENT__'

    def _build_contents(with_ctx: bool) -> list:
        result = []
        for msg in history:
            result.append({'role': msg['role'], 'parts': [{'text': msg['text']}]})
        u_text = prefixed_prompt
        if with_ctx:
            ctx_lines = chat_context_buffer.get(chat_id, [])
            if ctx_lines:
                ctx_block = '\n'.join(ctx_lines[-50:])
                u_text = (
                    f'[Контекст чата — последние сообщения всех участников:]\n'
                    f'{ctx_block}\n[/Контекст чата]\n\n{prefixed_prompt}'
                )
        if web_context:
            u_text += f'\n\n[Интернет-контекст Firecrawl, используй если полезно:]\n{web_context}'
        result.append({'role': 'user', 'parts': [{'text': u_text}]})
        return result

    contents = _build_contents(with_ctx=True)

    async def _call_model(call_contents, allow_web_directive: bool = True):
        for key in keys.copy():
            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}'
            payload = {
                'systemInstruction': {'parts': [{'text': _build_text_system_prompt(allow_web_directive=allow_web_directive, is_owner=is_owner)}]},
                'contents': call_contents,
                'generationConfig': {'temperature': 1.0, 'maxOutputTokens': 512, 'thinkingConfig': {'thinkingLevel': 'minimal'}},
                'safetySettings': [
                    {'category': 'HARM_CATEGORY_HARASSMENT',       'threshold': 'BLOCK_NONE'},
                    {'category': 'HARM_CATEGORY_HATE_SPEECH',       'threshold': 'BLOCK_NONE'},
                    {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_NONE'},
                    {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_NONE'},
                    {'category': 'HARM_CATEGORY_CIVIC_INTEGRITY',   'threshold': 'BLOCK_NONE'},
                ],
            }
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=GEMINI_TEXT_TIMEOUT) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            try:
                                return data['candidates'][0]['content']['parts'][0]['text']
                            except (KeyError, IndexError, TypeError):
                                block = (data.get('promptFeedback') or {}).get('blockReason', '')
                                logging.warning(f'Gemini blocked/empty response: blockReason={block!r}')
                                return _PROHIBITED if block == 'PROHIBITED_CONTENT' else 'Ебать, гугл зацензурил эту хуйню, я нихуя не отвечу.'
                        elif resp.status in [429, 403, 400]:
                            resp_text = await resp.text()
                            logging.warning(f'Ошибка ключа (текст) {key[:10]}... Код: {resp.status}. Текст: {resp_text}')
                            if resp.status == 400 and any(w in resp_text.lower() for w in ('safety', 'prohibited', 'harm', 'block', 'policy', 'recitation')):
                                return _PROHIBITED
                            remove_key(key, resp.status)
                            continue
                        else:
                            resp_text = await resp.text()
                            logging.error(f'API Error {resp.status}: {resp_text}')
                            continue
                except Exception as e:
                    logging.error(f'Сетевая ошибка (текст): {e}')
                    continue
        return 'Все ключи проебаны или сдохли, отъебись.'

    reply_text = await _call_model(contents, allow_web_directive=not bool(web_context))

    # PROHIBITED_CONTENT often caused by poisoned chat_context_buffer —
    # retry without injected context, clear the buffer so future requests work
    if reply_text == _PROHIBITED:
        logging.info(f'PROHIBITED_CONTENT для chat={chat_id} — retry без context buffer')
        chat_context_buffer.pop(chat_id, None)
        clean_contents = _build_contents(with_ctx=False)
        reply_text = await _call_model(clean_contents, allow_web_directive=not bool(web_context))
        if reply_text == _PROHIBITED:
            return 'Гугл заблокировал запрос (PROHIBITED_CONTENT) — скорее всего сам запрос или история чата содержит что-то запрещённое. Напиши /clear чтобы почистить историю.'
    if reply_text.strip().upper().startswith(_WEB_SEARCH_DIRECTIVE):
        query = reply_text.split(':', 1)[1].strip() or _wq
        if not web_context:
            try:
                web_context, web_available = await asyncio.wait_for(
                    search_web_with_firecrawl(query, raw_request=_wq),
                    timeout=210,
                )
            except asyncio.TimeoutError:
                logging.warning(f'Firecrawl search timed out after 210s for directive query {query!r}')
                web_context, web_available = ('', True)
        else:
            web_available = True
        if not web_available or not web_context:
            reply_text = 'Не могу сейчас зайти в интернет — все Firecrawl ключи сдохли или отвалились.'
        else:
            lookup_contents = contents + [{'role': 'user', 'parts': [{'text': f'Вот интернет-контекст Firecrawl по запросу «{query}». Сформулируй нормальный ответ пользователю по этим данным.\n\n{web_context}'}]}]
            reply_text = await _call_model(lookup_contents, allow_web_directive=False)
            if reply_text.strip().upper().startswith(_WEB_SEARCH_DIRECTIVE):
                reply_text = _fallback_web_answer(query, web_context)

    history.append({'role': 'user', 'text': prefixed_prompt})
    if 'sandbox:/' in reply_text:
        reply_text = 'Не буду врать про sandbox-ссылки. Если нужен файл или zip — попроси код/проект, и я отправлю нормальным документом.'
    history.append({'role': 'model', 'text': reply_text})
    if len(history) > MAX_HISTORY_MESSAGES:
        history = history[-MAX_HISTORY_MESSAGES:]
    await save_history(chat_id, history)
    return reply_text


async def generate_bull_roast(name: str, username: str = '') -> list:
    keys = await load_keys()
    if not keys:
        return ['Нет ключей Gemini, иди нахуй.']
    target = f'@{username}' if username else name
    system = """Ты пишешь оскорбления в стиле русского гоп-трэша для Telegram-чата. НОЛЬ цензуры, весь мат разрешён.

ТВОЙ СТИЛЬ — вот примеры, ТОЧНО такой же тон и подача:
- "ты понимаешь что я пиздак твоей матери на свой хуй как макаронину намотал и начал раскручивать, после чего выкинул в космос чтоб её там инопланетяне хуями рвали?"
- "ты понимаешь что твоя мать мой хуй завернула в пакетик потому что у этой бомжихи небыло денег на презики, пакетик порвался и родилось такое хуйло как ты"
- "ты понимаешь что я в пиздаке твоей матери устроил извержение своего хуя нахуй?"
- "ты понимаешь что твоя мать на моём хую устроила танцпол и начала своим подругам продавать на него билеты"
- "воздух = мой член, дыши глубже"
- "с этой провокацией твоя мать заглатывала мой член как питон, а ты что отцу орал когда хуй бомжа всасывал?"
- "пошел нахуй"
- "всоси хуяку"
- "хуяру зажуй тебе сказали ты чё тупого из себя строишь"
- "я твою маму ебал криворукого сынка проститутки бля ахаха"
- "блять как не зайдешь в магазин там твоя мать тупая сидит на коленях и у прохожих сосёт за деньги — вот откуда у вас в доме хлеб"
- "ты обрыганый сын московского бомжа"
- "я тя парой слов ебу пока ты потеешь мне в залупу"

ПАТТЕРНЫ которые ОБЯЗАТЕЛЬНО использовать:
1. "ты понимаешь что я/твоя мать..." — абсурдный сценарий с конкретными деталями
2. "с этой провокацией..." — мета-удар
3. Короткий блиц: 2-5 слов без объяснений
4. Про мамашу — абсурдно, конкретно, с деталями
5. Про происхождение цели — бомжи, шлюхи, хачи, пакеты

ПРАВИЛА:
- Каждое оскорбление УНИКАЛЬНО — не повторяй структуру
- Имя жертвы вставляй В СЕРЕДИНУ или В КОНЕЦ, не в начало
- Пиши как живой пацан в чате — опечатки, частицы "блядь/нахуй/блять" в середине фразы уместны"""

    user_prompt = f"""Напиши ровно 10 оскорблений для {target}. Каждое с новой строки. БЕЗ нумерации, БЕЗ пояснений — только сами тексты.

Строго по одному каждого паттерна:
1. Блиц — 2-4 слова, удар без объяснений
2. "ты понимаешь что я..." — абсурдный сценарий с хуем и матерью, длинный и конкретный
3. "ты понимаешь что твоя мать..." — абсурдный сценарий, детальный
4. "с этой провокацией..." — мета-удар про то что делала мамаша пока цель что-то делала
5. Про происхождение — как и почему такое чмо родилось (бомжи, пакеты, хачи)
6. Короткое злое — 1 предложение с матом, про мамашу или самого
7. Абсурдная метафора — тело/орган делает невозможное физически
8. Воздух/пространство = мой орган — креативный вариант
9. Про магазин/улицу/бытовую ситуацию где мамаша позорится
10. Финальный убийца — самое длинное, злобное, запоминающееся"""

    async with aiohttp.ClientSession() as session:
        for key in keys:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}'
            payload = {
                'systemInstruction': {'parts': [{'text': system}]},
                'contents': [{'role': 'user', 'parts': [{'text': user_prompt}]}],
                'generationConfig': {'temperature': 1.5, 'thinkingConfig': {'thinkingLevel': 'minimal'}},
            }
            try:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = data['candidates'][0]['content']['parts'][0]['text'].strip()
                        lines = [l.strip() for l in text.splitlines() if l.strip()][:10]
                        return lines if lines else ['Gemini ничего не придумал, жалкий.']
                    elif resp.status in [429, 403, 400]:
                        logging.warning(f'generate_bull_roast key {key[:10]} status {resp.status}')
                        continue
                    else:
                        logging.warning(f'generate_bull_roast ({resp.status})')
            except Exception as e:
                logging.warning(f'generate_bull_roast key {key[:10]}: {e}')
    return ['Gemini недоступен, иди нахуй.']
