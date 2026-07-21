import json
import logging
import posixpath
import re
from typing import Dict, Any, List
from urllib.parse import unquote

from config import CODE_GEN_MAX_OUTPUT_TOKENS, CODE_GEN_MODELS, CODE_GEN_TIMEOUT
from keys import load_keys, strip_code_fences
from shared_types import gemini_post


# ── Code generation service ───────────────────────────────────────────────

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
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError(f'project JSON root must be an object, got {type(payload).__name__}')
    return payload


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
    if not await load_keys():
        return {'ok': False, 'error': 'Ключи сдохли.'}
    user_prompt = f'''User request:
{prompt}

Generate the deliverable as real files. Remember: ONLY JSON with project_name, summary, run_instructions, files.'''
    last_error = ''
    for model_name in CODE_GEN_MODELS:
        gen_config: Dict[str, Any] = {'temperature': 0.25, 'maxOutputTokens': CODE_GEN_MAX_OUTPUT_TOKENS, 'responseMimeType': 'application/json'}
        if model_name.startswith('gemini-3.5'):
            gen_config['thinkingConfig'] = {'thinkingLevel': 'high'}
        else:
            gen_config['thinkingConfig'] = {'thinkingBudget': -1}
        payload = {
            'systemInstruction': {'parts': [{'text': _PROJECT_GEN_SYSTEM_PROMPT}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_prompt}]}],
            'generationConfig': gen_config,
        }
        logging.info(f'Project gen: trying {model_name}...')
        data, _key, err = await gemini_post(f'models/{model_name}:generateContent', payload, timeout=CODE_GEN_TIMEOUT)
        if data:
            try:
                candidate = data['candidates'][0]
                if candidate.get('finishReason') == 'SAFETY':
                    return {'ok': False, 'error': 'Модель зацензурила запрос.'}
                raw = candidate['content']['parts'][0]['text']
                project = _normalize_project_payload(_extract_project_json(raw))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = f'{model_name}: {type(exc).__name__}: {exc}'
                logging.warning('Project gen invalid response: %s', last_error)
                continue
            logging.info(f"Project gen: success with {model_name}, files={len(project['files'])}")
            return project
        if err:
            logging.warning(f'Project gen failed on {model_name}: {err}')
            last_error = f'{model_name}: {err}'
    return {'ok': False, 'error': last_error or 'Gemini вернул пустой ответ без описания ошибки.'}


