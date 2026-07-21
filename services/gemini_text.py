import asyncio
import logging
from services.deepseek_service import deepseek_text
from services.openrouter import generate_text_with_openrouter, OPENROUTER_TEXT_MODEL

from config import GEMINI_TEXT_TIMEOUT, MAX_HISTORY_MESSAGES
from database import get_history, save_history
from services.web_search import search_web_with_firecrawl, _extract_search_query, _fallback_web_answer
# ── Shared helpers moved to shared_types.py ───────────────────────────────
from shared_types import (
    _WEB_SEARCH_DIRECTIVE, _build_text_system_prompt, gemini_post, gemini_text_of,
)

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
            await _status(f'🧠 Нашёл {src_count} источника(-ов), синтезирую ответ через Claude Sonnet 5...')
            if not web_context:
                return f'Я искал «{clean_q}» — поиск вернул пустоту. Firecrawl не нашёл ни одного пригодного источника.'


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
        try:
            text = await generate_text_with_openrouter(
                prompt=call_contents[-1]['parts'][0]['text'] if call_contents else prompt,
                system_prompt=_build_text_system_prompt(allow_web_directive=allow_web_directive, is_owner=is_owner),
                model=OPENROUTER_TEXT_MODEL,
                max_tokens=800 if web_context else 300,
                timeout=GEMINI_TEXT_TIMEOUT,
            )
        except Exception as error:
            logging.error(f"Claude Sonnet 5 text generation failed: {type(error).__name__}: {error}", exc_info=True)
            if not is_owner and not explicit_web_lookup and not web_context:
                try:
                    from services.groq_service import generate_text_with_groq
                    context_lines = chat_context_buffer.get(chat_id, [])[-8:]
                    context_text = '\n'.join(context_lines) if context_lines else ''
                    fallback_prompt = (
                        f'[История чата — последние сообщения]:\n{context_text}\n\n[Пользователь]: {prefixed_prompt}'
                        if context_text else prefixed_prompt
                    )
                    fallback = await generate_text_with_groq(
                        fallback_prompt,
                        system_prompt=_build_text_system_prompt(allow_web_directive=allow_web_directive, is_owner=is_owner),
                        temperature=0.8,
                        max_tokens=300,
                    )
                    if fallback:
                        logging.warning("Claude Sonnet 5 failed; used Groq fallback")
                        return fallback
                except Exception as fallback_error:
                    logging.error(f"Groq fallback failed: {type(fallback_error).__name__}: {fallback_error}", exc_info=True)
            return f"Claude Sonnet 5 не ответил: {type(error).__name__}: {error}"
        return text

    reply_text = await _call_model(contents, allow_web_directive=not bool(web_context))

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

    text = await deepseek_text(
        prompt=user_prompt, system_prompt=system,
        model="deepseek-v4-flash", temperature=1.5, max_tokens=2000, timeout=20,
    )
    if text:
        lines = [l.strip() for l in text.splitlines() if l.strip()][:10]
        return lines if lines else ['DeepSeek ничего не придумал, жалкий.']
    return ['DeepSeek недоступен, иди нахуй.']


async def translate_to_english(prompt: str) -> str:
    """Translate a Russian image-generation prompt to English via Gemini."""
    import re as _re
    if not _re.search('[а-яёА-ЯЁ]', prompt):
        return prompt
    payload = {
        'contents': [{'parts': [{'text': f'Translate this image generation prompt to English for an AI image generator. Return ONLY the translated prompt, no explanations:\n{prompt}'}]}],
        'generationConfig': {'temperature': 0.1, 'thinkingConfig': {'thinkingBudget': 0}},
    }
    data, _key, _err = await gemini_post("models/gemini-3.5-flash:generateContent", payload, timeout=30.0)
    if data is not None:
        translated = gemini_text_of(data).strip()
        if translated:
            logging.info(f"Промпт переведён: '{prompt}' → '{translated}'")
            return translated
    return prompt
