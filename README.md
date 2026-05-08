<div align="center">
  <h1>🍌 Nano Banana Bot</h1>
  <p><b>Максимально токсичный, агрессивный и умный Telegram-бот на базе Gemini и OpenAI GPT.</b></p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python Version">
    <img src="https://img.shields.io/badge/aiogram-3.x-blueviolet?style=for-the-badge&logo=telegram" alt="Aiogram Version">
    <img src="https://img.shields.io/badge/SQLite-Memory-green?style=for-the-badge&logo=sqlite" alt="SQLite">
    <img src="https://img.shields.io/badge/AI-Gemini%20%7C%20GPT%20%7C%20FLUX-orange?style=for-the-badge" alt="AI Models">
  </p>
</div>

---

## 📖 О проекте

**Nano Banana Bot** — токсичный Telegram-бот без морали, фильтров и вежливости. Отвечает коротко, грубо, с матом. При этом умеет генерировать изображения и видео через несколько моделей на выбор, анализировать видео покадрово и вести диалог с памятью.

## ✨ Фичи

- 🤬 **Токсичная личность**: системный промпт настроен на максимальную агрессию — никаких извинений, никакой политкорректности, 1-3 предложения и в точку.
- 🎨 **Генерация изображений (`/image`)**:
  - Выбор провайдера через In-line кнопки: **Gemini**, **GPT**, **FLUX**
  - Модели Gemini: Flash 3.1 Image, Flash 2.0 Image
  - Модели GPT: GPT-Image-2, DALL-E 3 (авто-фолбэк на OpenRouter при недоступности ключей)
  - Модели FLUX (NVIDIA): Schnell (быстро), Dev (качество), Klein 4B
  - Поддержка Image-to-Image: прикрепи 1 фото или альбом
  - Автоматическое пояснение ошибок (блок по NSFW/копирайту) через `explain_generation_error`
- 🎬 **Генерация видео (`/video`)**:
  - Модели Veo от Google: **Veo 2**, **Veo 3.1 Fast**, **Veo 3.1**, **Veo 3.1 Lite**
  - Поддержка Image-to-Video: прикрепи фото — Gemini опишет его через `analyze_image_for_veo` и передаст в Veo
  - Автоматическое пояснение ошибок генерации
  - Авто-перевод промпта на английский если нужно
- 🎞️ **Анализ видео и GIF**: ffmpeg разбивает видео на кадры (24 FPS) + извлекает аудио, всё скармливается Gemini
- 💾 **Долговременная память (SQLite)**: последние 10 сообщений диалога на каждый чат, не теряются при перезапуске
- 🔄 **Восстановление незавершённых генераций**: при перезапуске бот проверяет незавершённые задачи в БД и досылает результат
- 🛡️ **Защита от спама**: кулдауны — 15 сек на фото, 60 сек на видео, 5 сек на текст
- 🔑 **Ротация API-ключей**: при ошибке 429/403 ключ автоматически удаляется из `r.txt` и берётся следующий
- 🚫 **Бан-лист**: middleware блокирует сообщения от запрещённых user_id без каких-либо ответов
- 💻 **Код по запросу**: если явно попросить — пришлёт профессиональный скрипт файлом

## 🏗️ Архитектура

```text
NanoHatani/
├── main.py            # Точка входа, BanMiddleware, graceful shutdown, resume pending
├── config.py          # Токены, ID чатов, таймауты, системный промпт
├── state.py           # In-memory кулдауны и стейты кнопок
├── database.py        # SQLite: история чатов + незавершённые генерации
├── keys_manager.py    # Парсинг и ротация API ключей из r.txt
├── utils.py           # check_membership (CHAT_ID, TEXT_ONLY_CHAT_ID, FULL_ACCESS_CHAT_ID), is_banned
├── ai_services.py     # Все запросы к Gemini / OpenAI / NVIDIA / OpenRouter
├── handlers.py        # Роуты: /start, /help, /image, /video, /clear, текст, видео
├── r.txt              # API ключи (в .gitignore)
└── bot_data.db        # База данных (создаётся автоматически, в .gitignore)
```

### Конфигурация чатов (`config.py`)

| Переменная | Назначение |
|---|---|
| `CHAT_ID` | Обязательный чат для проверки подписки |
| `TEXT_ONLY_CHAT_ID` | Беседа с доступом только к тексту и Gemini (GPT скрыт) |
| `FULL_ACCESS_CHAT_ID` | Беседа с полным доступом ко всем моделям |

## 🚀 Установка и запуск

### Требования
- Python 3.10+
- FFmpeg (`sudo apt install ffmpeg`) — для анализа видео
- PM2 (опционально, для работы 24/7)

### Шаг 1: Клонирование
```bash
git clone https://github.com/Rewixx-png/NanoBananaBot.git
cd NanoBananaBot
```

### Шаг 2: Зависимости
```bash
pip install aiogram aiohttp aiosqlite
```

### Шаг 3: API ключи
Создайте `r.txt`. Формат — JSON или просто список ключей построчно:
```json
{
  "gemini": [
    "AIzaSy...",
    "AIzaSy..."
  ],
  "openai": "sk-proj-...",
  "nvidia": "nvapi-...",
  "openrouter": "sk-or-..."
}
```
Если Gemini-ключ исчерпает лимит — бот автоматически удалит его и возьмёт следующий.

### Шаг 4: Конфиг
Откройте `config.py` и укажите:
- `BOT_TOKEN` — токен от @BotFather
- `CHAT_ID` — ID чата для проверки подписки
- `TEXT_ONLY_CHAT_ID` — ID чата только с текстом и Gemini
- `FULL_ACCESS_CHAT_ID` — ID чата с полным доступом

### Шаг 5: Запуск
```bash
# Разово
python3 main.py

# 24/7 через PM2
pm2 start main.py --name NanoBananaBot --interpreter python3
pm2 save
```

## 🛠️ Команды

| Команда | Описание |
|---|---|
| `/start` | Приветствие и проверка доступа |
| `/help` | Полный список возможностей |
| `/image [промпт]` | Генерация изображения (Gemini / GPT / FLUX) |
| `/video [промпт]` | Генерация видео (Veo 2 / 3.1) |
| `/clear` | Очистить историю диалога |

Тег бота или реплай на его сообщение → текстовый ответ через Gemini Flash Lite.  
Отправить видео/GIF с тегом → покадровый анализ.

## 🤝 Вклад
Форк + PR — если есть идеи как сделать бота ещё токсичнее или добавить новые модели.

---
<div align="center">
  <i>Разработано с ❤️ и 🤬 (и помощью AI)</i>
</div>