from aiogram import Router
from .admin import admin_router
from .media_gen import media_router, refresh_models
from .core import core_router
from .commands_misc import commands_misc_router
from .chat import chat_router
import handlers.media_video  # registers /video handler on media_router
import handlers.media_tts    # registers /tts handler on media_router
import handlers.r34          # registers /r34 handler on media_router

router = Router()

router.include_router(admin_router)
router.include_router(media_router)
router.include_router(core_router)
router.include_router(commands_misc_router)
router.include_router(chat_router)

__all__ = ['router', 'refresh_models']
