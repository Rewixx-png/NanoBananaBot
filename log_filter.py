"""
Logging filter that masks API keys before they're written to bot.log.

Prevents: Gemini (AIza...), OpenAI (sk-...), Replicate (r8_...),
Firecrawl (fc-...), Groq (gsk_...), NVIDIA (nvapi-...), Figma (figd_...).
"""
import re
import logging

# Patterns matching API key prefixes — keep first 6 chars + "..."
_KEY_PATTERNS: list[tuple[str, str]] = [
    (r'AIza[0-9A-Za-z_-]{20,}',    'AIzaSy...'),
    (r'sk-[a-zA-Z0-9]{32,}',        'sk-...'),
    (r'sk-proj-[A-Za-z0-9_-]{20,}', 'sk-proj-...'),
    (r'nvapi-[a-zA-Z0-9]{40,}',     'nvapi-...'),
    (r'fc-[a-f0-9]{30,}',           'fc-...'),
    (r'r8_[a-zA-Z0-9]{30,}',        'r8_...'),
    (r'gsk_[a-zA-Z0-9]{30,}',       'gsk_...'),
    (r'figd_[a-zA-Z0-9_-]{20,}',    'figd_...'),
    (r'x-goog-api-key:\s*\S+',      'x-goog-api-key: [REDACTED]'),
]
_COMPILED = [(re.compile(p), r) for p, r in _KEY_PATTERNS]


class APIKeyMaskingFilter(logging.Filter):
    """Filter that redacts API key material from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern, replacement in _COMPILED:
            msg = pattern.sub(replacement, msg)
        record.msg = msg
        record.args = ()  # prevent formatting with original args
        return True
