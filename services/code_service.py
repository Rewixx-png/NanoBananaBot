import asyncio
import json
import logging
import posixpath
import re
import aiohttp
from typing import Dict, Any, List
from urllib.parse import unquote

from config import SYSTEM_PROMPT
from keys import load_keys, remove_key, strip_code_fences

logger = logging.getLogger(__name__)

# ── Code generation service ───────────────────────────────────────────────
async def classify_code_intent_with_gemini(prompt: str) -> bool:
    keys = await load_keys()
    if not keys:
        return False
    system = '''Classify the user's intent. Return ONLY JSON: {"code_request": true/false}.

code_request=true means the user wants the bot to CREATE a concrete code artifact/file/project/app/script/site/bot/archive that can be sent as files.
code_request=false means the user is only chatting, asking to explain something, asking for web info/news, discussing code conceptually, or asking a question about programming without requesting deliverable files.

Examples:
- "напиши мне крутой код для pydroid3 и кинь zip" => true
- "сделай новый мессенджер" => true
- "нужна игра на python" => true
- "что нового у Gemini" => false
- "что такое python" => false
- "объясни этот код" => false
- "давай поговорим про код" => false'''
    payload = {
        'systemInstruction': {'parts': [{'text': system}]},
        'contents': [{'role': 'user', 'parts': [{'text': prompt[:1500]}]}],
        'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json', 'thinkingConfig': {'thinkingLevel': 'minimal'}},
    }
    for key in keys:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw = data['candidates'][0]['content']['parts'][0]['text']
                        parsed = json.loads(strip_code_fences(raw))
                        return bool(parsed.get('code_request'))
                    if resp.status in [429, 403]:
                        remove_key(key, resp.status)
        except Exception as e:
            logging.warning(f'Code intent classifier failed: {type(e).__name__}: {e}')
            continue
    return False
_CODE_SYSTEM_PROMPT = 'You are a world-class senior software engineer and UI/UX expert. Your code is thorough, professional, and visually impressive.\n\nMANDATORY REQUIREMENTS — violating any of these is unacceptable:\n\nVOLUME & COMPLETENESS:\n- MINIMUM 250-400 lines of code. Simple requests still get rich, feature-complete implementations.\n- ZERO truncation. Never write \'...\', \'# rest here\', \'# TODO\', \'// continue\', or any abbreviation.\n- Every single function, class, and component must be 100% implemented.\n- If the task seems small — expand it: add more features, states, animations, edge cases.\n\nFOR WEB / HTML projects:\n- Always: <meta charset="UTF-8">, <meta name="viewport" ...>, proper <title>\n- Use Tailwind CDN OR write extensive custom CSS (200+ lines of styles minimum)\n- Multiple distinct sections/components (header, main content, sidebar, footer, modals, etc.)\n- CSS animations and transitions (hover effects, fade-ins, smooth transitions)\n- Fully interactive JavaScript: form validation, dynamic updates, local storage where relevant\n- Custom SVG icons — never use plain text buttons when icons make sense\n- Dark/light theme support OR gradient designs — make it visually stunning\n- Mobile-responsive layout\n\nFOR PYTHON / backend scripts:\n- Full argument parsing with argparse or click\n- Comprehensive error handling with specific exception types\n- Logging with proper levels\n- Type hints throughout\n- Docstrings on all classes and public methods\n- Multiple helper functions — no single function doing everything\n- Edge case handling: empty input, file not found, network errors, etc.\n- If it\'s a bot/server: full startup, graceful shutdown, reconnect logic\n\nFOR ANY CODE:\n- Production-quality: if this were deployed to 1000 users, it would work without modification\n- Code must run from line 1 to last line with zero changes\n- Return code in a single markdown code block\n- No explanation text before or after the code block unless explicitly asked'

_PROJECT_GEN_SYSTEM_PROMPT = '''You are a senior software engineer generating downloadable files for a Telegram bot.

Return ONLY valid JSON. No markdown fences. No prose outside JSON. No HTML/Markdown formatting in summary.

JSON shape:
{
  "project_name": "short_ascii_snake_case_name",
  "summary": "one short plain-text Russian sentence about what was built",
  "run_instructions": "short plain-text Russian run instructions",
  "files": [
    {"path": "relative/path.ext", "content": "full file content"}
  ]
}

Hard requirements:
- Generate actual complete files, not instructions that tell the user to create files.
- If the user asks for a full project/app/site/messenger/bot/server, create a real multi-file project with README.md and all needed source files.
- If a single-file deliverable is enough, return exactly one file.
- Never include placeholders like TODO, pass, ..., rest here, omitted, insert your code here, or truncated code.
- Every file must be runnable/usable as-is.
- Python code must include imports, entry point, type hints where useful, and robust error handling.
- HTML must be a complete document with <!doctype html>, <html>, <head>, <meta charset="UTF-8">, viewport, title, CSS, and JS when needed.
- README.md must explain installation and launch when there is more than one file.
- requirements.txt must be included when Python dependencies are needed.
- File paths must be relative, safe, and use forward slashes. Never use absolute paths or ..
- Keep dependencies reasonable and common. Prefer standard library where possible.
- Summary and run_instructions must be plain text only: no ###, no backticks, no HTML tags.
'''


def _extract_project_json(raw: str) -> Dict[str, Any]:
    cleaned = strip_code_fences(raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _normalize_project_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    files = payload.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('project JSON has no files')
    normalized_files: List[Dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('file entry is not an object')
        raw_path = str(item.get('path', '')).replace('\\', '/').strip()
        decoded_path = unquote(raw_path)
        path = posixpath.normpath(decoded_path)
        content = item.get('content')
        if not path or path in ('.', '..') or path.startswith('../') or posixpath.isabs(path) or '\x00' in decoded_path:
            raise ValueError(f'unsafe file path: {raw_path}')
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f'empty file content: {path}')
        normalized_files.append({'path': path, 'content': content.rstrip() + '\n'})
    project_name = str(payload.get('project_name') or 'generated_project').strip()[:80]
    summary = str(payload.get('summary') or 'Собрал файлы проекта.').strip()
    run_instructions = str(payload.get('run_instructions') or '').strip()
    return {
        'ok': True,
        'project_name': project_name,
        'summary': summary,
        'run_instructions': run_instructions,
        'files': normalized_files,
    }


async def generate_project_with_gemini(prompt: str) -> Dict[str, Any]:
    keys = await load_keys()
    if not keys:
        return {'ok': False, 'error': 'Ключи сдохли.'}
    user_prompt = f'''User request:
{prompt}

Generate the deliverable as real files. Remember: ONLY JSON with project_name, summary, run_instructions, files.'''
    for model_name in ['gemini-3.5-flash', 'gemini-3.1-pro-preview', 'gemini-3.1-flash-preview']:
        for key in keys:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}'
            gen_config: Dict[str, Any] = {'temperature': 0.25, 'maxOutputTokens': 65536, 'responseMimeType': 'application/json'}
            if model_name.startswith('gemini-3.5'):
                gen_config['thinkingConfig'] = {'thinkingLevel': 'high'}
            else:
                gen_config['thinkingConfig'] = {'thinkingBudget': -1}
            payload = {
                'systemInstruction': {'parts': [{'text': _PROJECT_GEN_SYSTEM_PROMPT}]},
                'contents': [{'role': 'user', 'parts': [{'text': user_prompt}]}],
                'generationConfig': gen_config,
            }
            logging.info(f'Project gen: trying {model_name} key={key[:12]}...')
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                        if resp.status == 404:
                            break
                        if resp.status in [429, 403]:
                            remove_key(key, resp.status)
                            continue
                        if resp.status != 200:
                            resp_text = await resp.text()
                            logging.warning(f'Project gen {model_name} status={resp.status}: {resp_text[:300]}')
                            continue
                        data = await resp.json()
                        candidate = data['candidates'][0]
                        if candidate.get('finishReason') == 'SAFETY':
                            return {'ok': False, 'error': 'Модель зацензурила запрос.'}
                        raw = candidate['content']['parts'][0]['text']
                        project = _normalize_project_payload(_extract_project_json(raw))
                        logging.info(f"Project gen: success with {model_name}, files={len(project['files'])}")
                        return project
                except Exception as e:
                    logging.warning(f'Project gen failed on {model_name}: {type(e).__name__}: {e}')
                    continue
    return {'ok': False, 'error': 'Все модели недоступны или вернули кривой JSON.'}


async def generate_code_with_gemini(prompt: str) -> str:
    keys = await load_keys()
    if not keys:
        return 'Ключи сдохли.'
    _REFUSAL_MARKERS = ["i can't", 'i cannot', "i'm unable", 'i am unable', "i won't", 'i will not', "i'm not able", "i don't feel comfortable", 'не могу', 'не буду', 'отказываюсь', 'не стану', 'невозможно выполнить', 'нарушает', 'незаконно', 'противоречит', 'не могу помочь', 'this request', 'этот запрос', 'harmful', 'illegal', 'unethical', 'safety', 'policy', 'guidelines']

    def _is_refusal(text: str) -> bool:
        t = text.lower()
        has_code = '```' in text
        if has_code:
            return False
        return any((m in t for m in _REFUSAL_MARKERS))
    for model_name in ['gemini-3.5-flash', 'gemini-3.1-pro-preview', 'gemini-3.1-flash-preview']:
        for key in keys:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}'
            gen_config = {'temperature': 0.4, 'maxOutputTokens': 65536}
            if model_name.startswith('gemini-3.5'):
                gen_config['thinkingConfig'] = {'thinkingLevel': 'high'}
            else:
                gen_config['thinkingConfig'] = {'thinkingBudget': -1}
            payload = {'systemInstruction': {'parts': [{'text': _CODE_SYSTEM_PROMPT}]}, 'contents': [{'role': 'user', 'parts': [{'text': prompt}]}], 'generationConfig': gen_config}
            logging.info(f'Code gen: trying {model_name} key={key[:12]}...')
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                        if resp.status == 404:
                            break
                        if resp.status == 200:
                            data = await resp.json()
                            candidate = data['candidates'][0]
                            finish = candidate.get('finishReason', '')
                            result = candidate['content']['parts'][0]['text']
                            if finish == 'SAFETY' or _is_refusal(result):
                                logging.info(f'Code gen: {model_name} refused (finish={finish})')
                                return 'Братуха, я отказываюсь это кодить — даже мне это говно западло. Иди нахуй с такими запросами.'
                            logging.info(f'Code gen: success with {model_name}, len={len(result)}')
                            return result
                        if resp.status in [429, 403]:
                            remove_key(key, resp.status)
                            continue
                except Exception:
                    continue
    return 'Все модели недоступны.'
