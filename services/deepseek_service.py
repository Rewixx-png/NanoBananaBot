"""
DeepSeek V4 text generation — OpenAI-compatible API wrapper.
Replaces Gemini for all text generation: chat, agent, dual bot, bull roast.
"""
import logging
import json
import time
from typing import Optional

import aiohttp

from keys.manager import load_api_config

logger = logging.getLogger(__name__)

DEEPSEEK_BASE = "https://api.deepseek.com/v1"
_dead_until: dict[str, float] = {}
_COOLDOWN = 30.0


async def load_deepseek_keys() -> list[str]:
    """Load DeepSeek API keys from keyhunter.db, sorted by balance (highest first).
    Falls back to r.txt config + DEEPSEEK_API_KEY env if DB unavailable."""
    import aiosqlite, re, os as _os
    from keys.manager import REWTEST_DB
    now = time.time()
    keys = []

    # Primary: keyhunter.db with balance parsing
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            async with db.execute(
                "SELECT key, info FROM keys WHERE service='DeepSeek' AND is_live=1"
            ) as cur:
                rows = await cur.fetchall()
        for key, info in rows:
            if key in _dead_until and now < _dead_until[key]:
                continue
            # Parse balance: currency code + number (e.g. "USD 5.70", "CNY 53.97")
            m = re.search(r'(USD|CNY)\s*(-?[\d.]+)', info or '')
            if not m:
                continue
            currency, balance_str = m.group(1), m.group(2)
            try:
                balance = float(balance_str)
            except ValueError:
                continue  # skip inf/nan/non-numeric
            # Skip keys with low/no balance (minimum $0.50 USD)
            if 'no balance' in (info or '').lower() or balance <= 0:
                continue
            # Normalize: CNY → USD (approximate rate 7.2)
            balance_usd = balance if currency == 'USD' else balance / 7.2
            if balance_usd < 0.50:
                continue
            keys.append((balance_usd, key))
    except Exception as e:
        logger.warning(f"DeepSeek keyhunter read failed: {e}")

    if keys:
        keys.sort(key=lambda x: x[0], reverse=True)
        logger.info(f"DeepSeek: loaded {len(keys)} keys from keyhunter (top balance: ${keys[0][0]:.2f})")
        return [k for _, k in keys]

    # Fallback: r.txt config + env
    env_key = _os.getenv("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        keys_fallback = [env_key]
    else:
        keys_fallback = []
    try:
        config = load_api_config()
        cfg_keys = config.get("deepseek", [])
        if isinstance(cfg_keys, list):
            keys_fallback.extend(cfg_keys)
        elif isinstance(cfg_keys, str) and cfg_keys.strip():
            keys_fallback.append(cfg_keys.strip())
    except Exception as e:
        logger.debug(f"deepseek keys from r.txt skipped: {e}")
    return [k for k in keys_fallback if k not in _dead_until or now >= _dead_until[k]]

def _mark_dead(key: str):
    _dead_until[key] = time.time() + _COOLDOWN


_call_counter: int = 0


def _gemini_contents_to_openai_messages(contents: list) -> list[dict]:
    """Convert Gemini-format contents → OpenAI-format messages for DeepSeek."""
    global _call_counter
    messages = []
    for c in contents:
        role = c.get("role", "user")
        if role == "model":
            role = "assistant"
        parts = c.get("parts", [])
        text_parts = [p.get("text", "") for p in parts if "text" in p]
        fc_parts = [p.get("functionCall") for p in parts if "functionCall" in p]
        fr_parts = [p.get("functionResponse") for p in parts if "functionResponse" in p]

        if fc_parts:
            tool_calls = []
            for fc in fc_parts:
                _call_counter += 1
                tool_calls.append({
                    "id": str(_call_counter),
                    "type": "function",
                    "function": {"name": fc["name"], "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False)},
                })
            messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
        if fr_parts:
            # Find the last assistant message with tool_calls to match IDs
            call_ids = []
            for prev in reversed(messages):
                if prev.get("role") == "assistant" and prev.get("tool_calls"):
                    call_ids = [tc["id"] for tc in prev["tool_calls"]]
                    break
            for i, fr in enumerate(fr_parts):
                call_id = call_ids[i] if i < len(call_ids) else str(i)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(fr.get("response", {}).get("result", "")),
                })
        if not fc_parts and not fr_parts and text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts)})
    return messages
def _gemini_tools_to_openai(tools: list) -> list[dict]:
    """Convert Gemini tools → OpenAI tools format (handles both flat and wrapped)."""
    result = []
    for t in tools:
        # Wrapped format: {"functionDeclarations": [...]}
        for fd in t.get("functionDeclarations", []):
            result.append({
                "type": "function",
                "function": {
                    "name": fd["name"],
                    "description": fd.get("description", ""),
                    "parameters": fd.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        # Flat format: {"name": "...", "description": "...", "parameters": {...}}
        if "name" in t and "parameters" in t:
            result.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            })
    return result

def _openai_response_to_gemini(data: dict) -> dict:
    """Convert OpenAI chat completion → Gemini-compatible response dict."""
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")

    # Tool calls?
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        parts = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {}
            parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
        return {"content": {"parts": parts, "role": "model"}, "_finish": finish}

    # Text response
    text = msg.get("content", "") or ""
    return {"content": {"parts": [{"text": text}], "role": "model"}, "_finish": finish}


async def deepseek_chat(
    messages: list[dict],
    system_prompt: str = "",
    model: str = "deepseek-v4-flash",
    temperature: float = 1.0,
    max_tokens: int = 800,
    thinking: bool = False,
    tools: list[dict] = None,
    timeout: int = 90,
) -> Optional[dict]:
    keys = await load_deepseek_keys()
    if not keys:
        return None
    url = f"{DEEPSEEK_BASE}/chat/completions"
    payload: dict = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if system_prompt:
        payload["messages"] = [{"role": "system", "content": system_prompt}] + (list(messages) if messages else [])
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        payload["thinking"] = {"type": "disabled"}
    elif thinking and "v4" in model:
        payload["thinking"] = {"type": "enabled"}
    for key in keys:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    body = await resp.text()
                    logger.warning(f"DeepSeek HTTP {resp.status}: {body[:200]}")
                    if resp.status in (429, 401, 403):
                        _mark_dead(key); continue
        except Exception as e:
            logger.warning(f"DeepSeek: {type(e).__name__}: {e}")
            continue
    return None


async def deepseek_text(
    prompt: str,
    system_prompt: str = "",
    model: str = "deepseek-v4-flash",
    temperature: float = 1.0,
    max_tokens: int = 800,
    timeout: int = 90,
) -> Optional[str]:
    """Simple text generation — returns string or None."""
    messages = [{"role": "user", "content": prompt}]
    data = await deepseek_chat(
        messages=messages, system_prompt=system_prompt,
        model=model, temperature=temperature, max_tokens=max_tokens, timeout=timeout,
    )
    if data:
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return None
