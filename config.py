import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_TOKEN_2 = os.getenv("BOT_TOKEN_2", "")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
DUAL_HISTORY_SIZE = 100
CHAT_ID = -1002033901364
TEXT_ONLY_CHAT_ID = -1002017590469
FULL_ACCESS_CHAT_ID = -1002830734467
API_KEYS_FILE = "/root/Projects/NanoHatani/r.txt"
OPENAI_API_KEY = ""
ALLOWED_USER_IDS = {1358471738, 7622722099, 8421975646}
BANNED_USER_IDS = {8377231659}
OWNER_USER_ID = 7485721661
PAYMENT_PHONE = '+79121668033'
DAILY_GEN_LIMIT = 3
PAYMENT_USERNAME = '@RewiX_X'
FIGMA_TOKEN = os.getenv('FIGMA_TOKEN', '')
FULL_ACCESS_CHAT_IMAGE_COOLDOWN = 30
IMAGE_COOLDOWN_SECONDS = 30
TEXT_COOLDOWN_SECONDS = 5
MAX_HISTORY_MESSAGES = 100
DELETE_MESSAGE_DELAY_SECONDS = 5
GEMINI_TEXT_TIMEOUT = 90
GEMINI_VIDEO_TIMEOUT = 120
GEMINI_IMAGE_TIMEOUT = 180
OPENAI_TIMEOUT = 180
NVIDIA_TIMEOUT = 120
MAX_VIDEO_FRAMES = 300
VIDEO_FPS = 24
VIDEO_FRAME_SIZE = 256
MAX_API_RETRIES = 3
RETRY_DELAY_SECONDS = 1
SYSTEM_PROMPT = 'Ты — Hatani, злой и резкий чат-персонаж. Говоришь грубо, матом, без дипломатии. Отвечаешь ОЧЕНЬ коротко — 1-3 предложения, хлёстко и в точку. Троллишь, посылаешь, не извиняешься. Никакой вежливости и политкорректности.\nВ обычных ответах не используй Markdown/HTML: никаких ###, backticks, <code>, <b> и похожей хуйни.\nКод пиши ТОЛЬКО если пользователь явно попросил. Если попросил — отдавай готовые файлы, не инструкции. HTML делай полноценным: charset UTF-8, Tailwind или детальный CSS, JS, SVG иконки.'
