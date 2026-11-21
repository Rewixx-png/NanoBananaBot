"""
Logging filter that masks API keys and bot tokens before they're written to logs.

Covers: Gemini, OpenAI/DeepSeek/OpenRouter, ElevenLabs, Replicate, Firecrawl,
Groq, NVIDIA, Figma, Telegram bot tokens, and auth headers.
"""
import re
import logging

_KEY_PATTERNS: list[tuple[str, str]] = [
    (r'AIza[0-9A-Za-z_-]{20,}',                 'AIzaSy...'),
    (r'sk-proj-[A-Za-z0-9_-]{20,}',             'sk-proj-...'),
    (r'sk-or-v1-[A-Za-z0-9]{20,}',              'sk-or-v1-...'),
    (r'sk_[a-fA-F0-9]{32,}',                    'sk_...'),
    (r'sk-[a-zA-Z0-9_-]{20,}',                  'sk-...'),
    (r'nvapi-[a-zA-Z0-9_-]{30,}',               'nvapi-...'),
    (r'fc-[a-f0-9]{24,}',                       'fc-...'),
    (r'r8_[a-zA-Z0-9]{30,}',                    'r8_...'),
    (r'gsk_[a-zA-Z0-9]{30,}',                   'gsk_...'),
    (r'figd_[a-zA-Z0-9_-]{20,}',                'figd_...'),
    (r'/bot\d{8,11}:[A-Za-z0-9_-]{30,}/',       '/bot[REDACTED]/'),
    (r'\b\d{8,11}:AA[A-Za-z0-9_-]{30,}\b',      '[TG_TOKEN_REDACTED]'),
    (r'(?i)(x-goog-api-key|xi-api-key):\s*\S+', r'\1: [REDACTED]'),
    (r'(?i)(Authorization:\s*Bearer)\s+\S+',    r'\1 [REDACTED]'),
]
_COMPILED = [(re.compile(p), r) for p, r in _KEY_PATTERNS]


def mask_secrets(text: str) -> str:
    """Redact API keys and tokens from arbitrary text."""
    if not text:
        return text
    out = text
    for pattern, replacement in _COMPILED:
        out = pattern.sub(replacement, out)
    return out


class APIKeyMaskingFilter(logging.Filter):
    """Filter that redacts API key material from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = ()  # prevent formatting with original args
        return True
