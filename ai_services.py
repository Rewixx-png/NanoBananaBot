import asyncio
import time
import aiohttp
import json
import base64
import tempfile
import os
import subprocess
import logging
import re
import shutil
import posixpath
from urllib.parse import unquote
from typing import Tuple, Optional, Any, Dict, List
from config import SYSTEM_PROMPT, GEMINI_TEXT_TIMEOUT, GEMINI_VIDEO_TIMEOUT, GEMINI_IMAGE_TIMEOUT, OPENAI_TIMEOUT, NVIDIA_TIMEOUT, MAX_HISTORY_MESSAGES, MAX_VIDEO_FRAMES, VIDEO_FPS, VIDEO_FRAME_SIZE, MAX_API_RETRIES, RETRY_DELAY_SECONDS
from database import get_history, save_history
from keys import load_keys, load_openai_key, load_openai_keys, load_nvidia_keys, load_openrouter_keys, load_replicate_keys, load_groq_keys, load_firecrawl_keys, remove_key, strip_code_fences
# ── Proxy re-exports (canonical code moved to services/) ──────────────────
from services.nvidia import generate_image_with_nvidia, translate_to_english
from services.replicate import generate_image_with_replicate, fetch_replicate_image_models, _REPLICATE_MODELS, _DYNAMIC_REPLICATE_VERSIONS
from services.openrouter import generate_image_with_openrouter
from services.openai_service import generate_image_with_gpt, parse_openai_image_response, is_openai_verification_error, is_openai_timeout_error, fetch_openai_image_models
from services.web_search import search_web_with_firecrawl, synthesize_web_answer
from services.video_service import generate_video_with_gemini, generate_video_with_veo, generate_video_with_omni, start_veo_generation, poll_veo_operation, analyze_image_for_veo, fetch_veo_models
from services.audio_service import generate_tts_with_gemini, analyze_voice_with_gemini, fetch_gemini_tts_models
from services.code_service import classify_code_intent_with_gemini, generate_code_with_gemini, generate_project_with_gemini
from services.gemini_image import generate_image_with_gemini, analyze_photo_with_gemini, generate_reviewed_image_with_gemini, classify_draw_intent_with_gemini, review_image_with_gemini, generate_image_prompt, fetch_gemini_image_models
from services.pil_codegen import generate_image_via_code
from services.upscale_service import upscale_image
from services.gemini_text import generate_text_with_gemini, generate_bull_roast
from services.error_explainer import explain_generation_error
# ── Shared types / constants (canonical home: shared_types.py) ────────────
from shared_types import (
    _WEB_SEARCH_DIRECTIVE, _KICK_DIRECTIVE, _TEXT_MODEL_FALLBACKS,
    _models_cache, _MODELS_CACHE_TTL, _pretty_model_name,
    _thinking_config, _guess_image_mime, _build_text_system_prompt,
)


