"""
Voice cloning and TTS settings DB shims (re-exported from consolidated queries module).
"""
from database.queries import (
    add_voice, get_voices, get_voice_by_id, delete_voice,
    get_settings, save_settings,
)
from database.connection import get_db

__all__ = [
    'add_voice', 'get_voices', 'get_voice_by_id', 'delete_voice',
    'get_settings', 'save_settings', 'get_db',
]
