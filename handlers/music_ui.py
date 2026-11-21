"""UI components, cards, and styled inline keyboards for /music."""

from html import escape
from typing import Optional

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from config import NEWS_EMOJI_IDS, SUNO_LYRICS_MAX_CHARS
from state import pending_lyria_configs, pending_suno_configs

_BTN_FIELDS = getattr(InlineKeyboardButton, "model_fields", {})
_HAS_BTN_STYLE = "style" in _BTN_FIELDS
_HAS_BTN_ICON = "icon_custom_emoji_id" in _BTN_FIELDS
_HAS_COPY_TEXT = "copy_text" in _BTN_FIELDS

_SUNO_DURATIONS = (30, 60, 90, 120, 180, 240, 360)
_SUNO_QUICK_DURATIONS = (30, 60, 120, 180)
_SUNO_STYLE_WEIGHTS = (0.0, 0.25, 0.5, 0.65, 0.8, 1.0)
_SUNO_WEIRDNESS = (0.0, 0.25, 0.5, 0.65, 1.0)
_SUNO_AUDIO_WEIGHTS = (0.0, 0.25, 0.5, 0.65, 1.0)
_SUNO_VARIETY = (0, 1, 2, 3, 4)
_SUNO_VOCAL = (("Любой", ""), ("Мужской", "m"), ("Женский", "f"))

_LYRIA_TEMPS = (
    ("Спокойный", "safe"),
    ("Обычный", "normal"),
    ("Смелый", "bold"),
    ("Дикий", "wild"),
)

_MODEL_ICONS = {
    "suno-v6": "music",
    "suno-v6-wild": "sparkle",
    "suno-v6-mini": "lightning",
    "lyria-35": "music",
    "lyria-pro": "sparkle",
    "lyria-clip": "lightning",
}


def tg_icon(name: str, fallback: str = "") -> str:
    """Render a Telegram custom emoji tag if configured."""
    emoji_id = NEWS_EMOJI_IDS.get(name)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback or "•"}</tg-emoji>'
    return fallback


def ui_button(
    text: str,
    *,
    callback_data: Optional[str] = None,
    style: Optional[str] = None,
    icon: Optional[str] = None,
    copy_text: Optional[str] = None,
) -> InlineKeyboardButton:
    """Create an InlineKeyboardButton with Telegram Bot API 9.4+ style and icon support."""
    kwargs: dict = {"text": text}
    if copy_text is not None and _HAS_COPY_TEXT:
        kwargs["copy_text"] = CopyTextButton(text=copy_text[:256])
    else:
        kwargs["callback_data"] = callback_data
    if style and _HAS_BTN_STYLE:
        kwargs["style"] = style
    if icon and _HAS_BTN_ICON:
        emoji_id = NEWS_EMOJI_IDS.get(icon)
        if emoji_id:
            kwargs["icon_custom_emoji_id"] = emoji_id
    return InlineKeyboardButton(**kwargs)


def _next_in_cycle(values: tuple, current, cast=None):
    for i, v in enumerate(values):
        if cast:
            try:
                match = cast(v) == cast(current)
            except (TypeError, ValueError):
                match = str(v) == str(current)
        else:
            match = str(v) == str(current)
        if match:
            return values[(i + 1) % len(values)]
    return values[0]


def _cut(text: str, limit: int = 84) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _idea_quote(prompt: str, limit: int = 420) -> str:
    text = (prompt or "").strip()
    if not text:
        return "<blockquote><i>Идея не задана</i></blockquote>"
    safe = escape(text[:limit] + ("…" if len(text) > limit else ""))
    if len(text) > 160 or text.count("\n") >= 3:
        return f"<blockquote expandable>{safe}</blockquote>"
    return f"<blockquote>{safe}</blockquote>"


def _fmt_duration(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m}:{s:02d}" if m else f"{s}с"


def _pct(val) -> str:
    try:
        return f"{int(round(float(val) * 100))}%"
    except (TypeError, ValueError):
        return "—"


# ── Step 0: Bare /music (ask for idea) ───────────────────────────────────

def music_ask_idea_text() -> str:
    return (
        f"{tg_icon('music', '♪')} <b>Новая композиция</b>\n\n"
        "Пришли описание одним сообщением: жанр, настроение, инструменты или тему.\n\n"
        "<i>Примеры (нажми, чтобы скопировать):</i>\n"
        "<code>мрачный synthwave, 118 bpm, без вокала</code>\n"
        "<code>инди-рок о ночной Москве, хриплый мужской вокал</code>"
    )


def music_ask_idea_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Suno Студия · все инструменты", callback_data=f"ss:hub:{request_id}", style="primary", icon="sparkle")],
        [ui_button("Отмена", callback_data=f"musiccancel:{request_id}")],
    ])


def music_edit_idea_text(current_prompt: str) -> str:
    return (
        f"{tg_icon('pencil', '✎')} <b>Изменить идею</b>\n\n"
        f"{_idea_quote(current_prompt, 300)}\n\n"
        "Пришли новое описание следующим сообщением."
    )


# ── Step 1: Provider selection ───────────────────────────────────────────

def music_provider_text(prompt: str) -> str:
    return (
        f"{tg_icon('music', '♪')} <b>Музыка</b> · <i>шаг 1 из 3</i>\n"
        f"{_idea_quote(prompt)}\n\n"
        "<b>Suno V6</b> — песни с вокалом, текстом и двумя вариантами.\n"
        "<b>Lyria</b> — инструментал и зарисовки от Google."
    )


def music_provider_keyboard(request_id: str, providers: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(
            f"{providers['suno']['label']} · {providers['suno']['desc']}",
            callback_data=f"musicprov:{request_id}:suno",
            icon="microphone",
        )],
        [ui_button(
            f"{providers['lyria']['label']} · {providers['lyria']['desc']}",
            callback_data=f"musicprov:{request_id}:lyria",
            icon="sparkle",
        )],
        [ui_button(
            "Suno Студия · текст, стемы, голоса, клипы",
            callback_data=f"ss:hub:{request_id}",
            icon="chart",
        )],
        [
            ui_button("Изменить идею", callback_data=f"musicedit:{request_id}", icon="pencil"),
            ui_button("Отмена", callback_data=f"musiccancel:{request_id}"),
        ],
    ])


# ── Step 2: Model selection ──────────────────────────────────────────────

def music_model_text(prompt: str, provider_label: str) -> str:
    return (
        f"{tg_icon('music', '♪')} <b>{escape(provider_label)}</b> · <i>шаг 2 из 3</i>\n"
        f"{_idea_quote(prompt, 260)}\n\n"
        "Выбери движок:"
    )


def music_model_keyboard(request_id: str, provider: str, models: dict) -> InlineKeyboardMarkup:
    rows = [
        [ui_button(
            f"{info['label']} · {info['desc']}",
            callback_data=f"musicsel:{request_id}:{provider}:{mk}",
            icon=_MODEL_ICONS.get(mk, "music"),
        )]
        for mk, info in models.items()
    ]
    rows.append([
        ui_button("← Провайдеры", callback_data=f"musicprovback:{request_id}"),
        ui_button("Отмена", callback_data=f"musiccancel:{request_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Step 3A: Suno Quick & Studio screens ─────────────────────────────────

def suno_quick_text(request_id: str) -> str:
    d = pending_suno_configs.get(request_id, {})
    cfg = d.get("cfg", {})
    label = escape(str(d.get("label", "Suno V6")))
    style = (cfg.get("style") or "").strip()
    lyrics = (cfg.get("lyrics") or "").strip()
    duration = int(cfg.get("duration", 60))
    instrumental = str(cfg.get("instrumental", "0")) == "1"

    lines = [
        f"{tg_icon('microphone', '♪')} <b>{label}</b> · <i>быстрый режим</i>",
        _idea_quote(d.get("prompt", ""), 300),
        "",
        f"Стиль: <code>{escape(_cut(style, 100))}</code>" if style else "Стиль: <i>из идеи</i>",
    ]
    if instrumental:
        lines.append("Вокал: <code>инструментал</code>")
    elif lyrics:
        lines.append(f"Текст: <code>{len(lyrics)} / {SUNO_LYRICS_MAX_CHARS} симв.</code>")
    else:
        lines.append("Текст: <i>напишет Suno</i>")
    lines.append(f"Длительность: <code>{_fmt_duration(duration)}</code>")
    return "\n".join(lines)


def suno_quick_keyboard(request_id: str) -> InlineKeyboardMarkup:
    d = pending_suno_configs.get(request_id, {})
    cfg = d.get("cfg", {})
    instrumental = str(cfg.get("instrumental", "0")) == "1"
    duration = int(cfg.get("duration", 60))
    next_duration = _next_in_cycle(_SUNO_QUICK_DURATIONS, duration, int)

    rows = [
        [ui_button("Сгенерировать", callback_data=f"sunogen:{request_id}", style="success", icon="play")],
    ]
    if instrumental:
        rows.append([ui_button("Стиль", callback_data=f"sunoinput:{request_id}:style", icon="pencil")])
    else:
        rows.append([
            ui_button("Текст песни", callback_data=f"sunoinput:{request_id}:lyrics", icon="pencil"),
            ui_button("Стиль", callback_data=f"sunoinput:{request_id}:style", icon="pencil"),
        ])
    rows.append([
        ui_button(
            f"Инструментал: {'вкл' if instrumental else 'выкл'}",
            callback_data=f"sunocfg:{request_id}:instrumental:{'0' if instrumental else '1'}",
            style="primary" if instrumental else None,
        ),
        ui_button(
            f"Длина: {_fmt_duration(duration)}",
            callback_data=f"sunocfg:{request_id}:duration:{next_duration}",
        ),
    ])
    rows.append([ui_button("Все настройки", callback_data=f"sunoall:{request_id}")])
    rows.append([
        ui_button("← Модели", callback_data=f"sunoback:{request_id}"),
        ui_button("Отмена", callback_data=f"sunocancel:{request_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def suno_studio_text(request_id: str) -> str:
    d = pending_suno_configs.get(request_id, {})
    cfg = d.get("cfg", {})
    label = escape(str(d.get("label", "Suno V6")))
    style = (cfg.get("style") or "").strip()
    lyrics = (cfg.get("lyrics") or "").strip()
    title = (cfg.get("title") or "").strip()
    neg = (cfg.get("neg") or "").strip()
    instrumental = str(cfg.get("instrumental", "0")) == "1"
    vocal = {"m": "мужской", "f": "женский"}.get(cfg.get("vocal"), "любой")
    duration = int(cfg.get("duration", 60))
    variety = int(cfg.get("variety", 1))
    sw = _pct(cfg.get("style_weight", 0.65))
    weird = _pct(cfg.get("weirdness", 0.25))
    aw = _pct(cfg.get("audio_weight", 0.5))

    lines = [
        f"{tg_icon('microphone', '♪')} <b>{label}</b> · <i>все настройки</i>",
        _idea_quote(d.get("prompt", ""), 220),
        "",
        f"Стиль: <code>{escape(_cut(style, 90))}</code>" if style else "Стиль: <i>из идеи</i>",
    ]
    if instrumental:
        lines.append("Вокал: <code>без вокала</code>")
    else:
        lines.append(f"Текст: <code>{escape(_cut(lyrics, 70))}</code>" if lyrics else "Текст: <i>напишет Suno</i>")
        lines.append(f"Голос: <code>{vocal}</code>")
    if title:
        lines.append(f"Название: <code>{escape(title)}</code>")
    if neg:
        lines.append(f"Исключить: <code>{escape(_cut(neg, 70))}</code>")
    if cfg.get("persona_label"):
        lines.append(f"Персона/голос: <code>{escape(str(cfg['persona_label']))}</code>")
    refs_count = sum(len(cfg.get(k) or []) for k in ("image_urls", "video_urls", "audio_urls", "midi_urls"))
    if str(cfg.get("custom_mode", "1")) == "0" or refs_count:
        lines.append(f"Режим: <code>авто + референсы ({refs_count})</code>")

    tuning = f"Стиль <code>{sw}</code> · Креатив <code>{weird}</code>"
    if not instrumental:
        tuning += f" · Аудио <code>{aw}</code>"
    lines.append("")
    lines.append(tuning)
    lines.append(f"Разнообразие <code>{variety}/4</code> · Длина <code>{_fmt_duration(duration)}</code>")
    return "\n".join(lines)


def suno_studio_keyboard(request_id: str) -> InlineKeyboardMarkup:
    d = pending_suno_configs.get(request_id, {})
    cfg = d.get("cfg", {})
    instrumental = str(cfg.get("instrumental", "0")) == "1"
    vocal = str(cfg.get("vocal", ""))
    duration = int(cfg.get("duration", 60))
    variety = int(cfg.get("variety", 1))
    sw = float(cfg.get("style_weight", 0.65))
    weird = float(cfg.get("weirdness", 0.25))
    aw = float(cfg.get("audio_weight", 0.5))

    next_dur = _next_in_cycle(_SUNO_DURATIONS, duration, int)
    next_var = _next_in_cycle(_SUNO_VARIETY, variety, int)
    next_sw = _next_in_cycle(_SUNO_STYLE_WEIGHTS, sw, float)
    next_weird = _next_in_cycle(_SUNO_WEIRDNESS, weird, float)
    next_aw = _next_in_cycle(_SUNO_AUDIO_WEIGHTS, aw, float)

    rows = [
        [ui_button("Сгенерировать", callback_data=f"sunogen:{request_id}", style="success", icon="play")],
        [
            ui_button("Стиль", callback_data=f"sunoinput:{request_id}:style", icon="pencil"),
            ui_button("Текст песни", callback_data=f"sunoinput:{request_id}:lyrics", icon="pencil"),
        ],
        [
            ui_button("Название", callback_data=f"sunoinput:{request_id}:title"),
            ui_button("Исключить", callback_data=f"sunoinput:{request_id}:neg"),
        ],
        [
            ui_button(
                "С вокалом",
                callback_data=f"sunocfg:{request_id}:instrumental:0",
                style="primary" if not instrumental else None,
            ),
            ui_button(
                "Инструментал",
                callback_data=f"sunocfg:{request_id}:instrumental:1",
                style="primary" if instrumental else None,
            ),
        ],
    ]
    if not instrumental:
        rows.append([
            ui_button(
                label,
                callback_data=f"sunocfg:{request_id}:vocal:{val}",
                style="primary" if vocal == val else None,
            )
            for label, val in _SUNO_VOCAL
        ])
    rows.append([
        ui_button(f"Длина · {_fmt_duration(duration)}", callback_data=f"sunocfg:{request_id}:duration:{next_dur}"),
        ui_button(f"Вариации · {variety}/4", callback_data=f"sunocfg:{request_id}:variety:{next_var}"),
    ])
    sound_row = [
        ui_button(f"Стиль · {_pct(sw)}", callback_data=f"sunocfg:{request_id}:style_weight:{next_sw}"),
        ui_button(f"Креатив · {_pct(weird)}", callback_data=f"sunocfg:{request_id}:weirdness:{next_weird}"),
    ]
    if not instrumental:
        sound_row.append(
            ui_button(f"Аудио · {_pct(aw)}", callback_data=f"sunocfg:{request_id}:audio_weight:{next_aw}")
        )
    rows.append(sound_row)
    refs_count = sum(len(cfg.get(k) or []) for k in ("image_urls", "video_urls", "audio_urls", "midi_urls"))
    rows.append([
        ui_button("Буст стиля", callback_data=f"sunoboost:{request_id}", icon="sparkle"),
        ui_button("Персона / голос", callback_data=f"sunopersona:{request_id}", icon="microphone"),
    ])
    rows.append([
        ui_button(
            f"Референсы ({refs_count})" if refs_count else "Референсы (медиа)",
            callback_data=f"sunorefs:{request_id}",
            style="primary" if refs_count else None,
            icon="attachment",
        ),
    ])
    rows.append([
        ui_button("Быстрый режим", callback_data=f"sunoquick:{request_id}", icon="lightning"),
        ui_button("Сбросить", callback_data=f"sunoreset:{request_id}"),
    ])
    rows.append([
        ui_button("← Модели", callback_data=f"sunoback:{request_id}"),
        ui_button("Отмена", callback_data=f"sunocancel:{request_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Step 3B: Lyria settings screen ───────────────────────────────────────

def lyria_cfg_text(request_id: str) -> str:
    d = pending_lyria_configs.get(request_id, {})
    cfg = d.get("cfg", {})
    label = escape(str(d.get("label", "Lyria")))
    instrumental = str(cfg.get("instrumental", "0")) == "1"
    temp_map = dict((v, k) for k, v in _LYRIA_TEMPS)
    temp_label = temp_map.get(str(cfg.get("temp", "normal")), "Обычный")

    lines = [
        f"{tg_icon('sparkle', '♪')} <b>{label}</b> · <i>настройки</i>",
        _idea_quote(d.get("prompt", ""), 260),
        "",
    ]
    for field, title in (("style", "Стиль"), ("mood", "Настроение"), ("structure", "Структура")):
        val = (cfg.get(field) or "").strip()
        lines.append(f"{title}: <code>{escape(_cut(val, 80))}</code>" if val else f"{title}: <i>не задано</i>")
    lines.append(f"Вокал: <code>{'без вокала' if instrumental else 'с вокалом'}</code>")
    lines.append(f"Креативность: <code>{temp_label}</code>")
    lines.append("")
    lines.append("<i>Lyria принимает только креативность — стиль, настроение и структура добавляются в промпт.</i>")
    return "\n".join(lines)


def lyria_cfg_keyboard(request_id: str) -> InlineKeyboardMarkup:
    d = pending_lyria_configs.get(request_id, {})
    cfg = d.get("cfg", {})
    instrumental = str(cfg.get("instrumental", "0")) == "1"
    temp = str(cfg.get("temp", "normal"))

    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Сгенерировать", callback_data=f"lyriagen:{request_id}", style="success", icon="play")],
        [
            ui_button("Стиль", callback_data=f"lyriainput:{request_id}:style", icon="pencil"),
            ui_button("Настроение", callback_data=f"lyriainput:{request_id}:mood", icon="pencil"),
        ],
        [ui_button("Структура / текст", callback_data=f"lyriainput:{request_id}:structure", icon="pencil")],
        [
            ui_button(
                "С вокалом",
                callback_data=f"lyriacfg:{request_id}:instrumental:0",
                style="primary" if not instrumental else None,
            ),
            ui_button(
                "Инструментал",
                callback_data=f"lyriacfg:{request_id}:instrumental:1",
                style="primary" if instrumental else None,
            ),
        ],
        [
            ui_button(
                label,
                callback_data=f"lyriacfg:{request_id}:temp:{val}",
                style="primary" if temp == val else None,
            )
            for label, val in _LYRIA_TEMPS[:2]
        ],
        [
            ui_button(
                label,
                callback_data=f"lyriacfg:{request_id}:temp:{val}",
                style="primary" if temp == val else None,
            )
            for label, val in _LYRIA_TEMPS[2:]
        ],
        [
            ui_button("← Модели", callback_data=f"lyriaback:{request_id}"),
            ui_button("Отмена", callback_data=f"lyriacancel:{request_id}"),
        ],
    ])


# ── Field input & multi-message lyrics collector ─────────────────────────

def field_input_text(prompt_text: str, current_value: str) -> str:
    val = (current_value or "").strip()
    if not val:
        return prompt_text
    return f"{prompt_text}\n\nСейчас: <code>{escape(_cut(val, 220))}</code>"


def field_input_keyboard(provider: str, request_id: str, field: str, has_value: bool) -> InlineKeyboardMarkup:
    row = [ui_button("← К настройкам", callback_data=f"{provider}inputback:{request_id}")]
    if has_value:
        row.append(ui_button("Очистить", callback_data=f"{provider}clearfield:{request_id}:{field}", style="danger"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def lyrics_collect_text(buffer: str) -> str:
    buf = buffer or ""
    count = len(buf)
    filled = min(10, int(round((count / max(1, SUNO_LYRICS_MAX_CHARS)) * 10))) if count else 0
    meter = "▓" * filled + "░" * (10 - filled)
    lines = [
        f"{tg_icon('pencil', '✎')} <b>Текст песни</b>",
        "Пришли текст одним или несколькими сообщениями, либо файлом <code>.txt</code>.",
        "",
        f"<code>[{meter}]</code> Собрано: {count} / {SUNO_LYRICS_MAX_CHARS}",
    ]
    if buf:
        preview = buf if len(buf) <= 500 else "…\n" + buf[-500:]
        lines.append(f"<blockquote expandable>{escape(preview)}</blockquote>")
    lines.append("Когда закончишь, нажми «Готово».")
    return "\n".join(lines)


def lyrics_collect_keyboard(request_id: str, has_text: bool) -> InlineKeyboardMarkup:
    row = [ui_button("Готово", callback_data=f"sunolyrdone:{request_id}", style="success")]
    if has_text:
        row.append(ui_button("Очистить", callback_data=f"sunolyrclear:{request_id}", style="danger"))
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [ui_button("← К настройкам", callback_data=f"sunolyrback:{request_id}")],
    ])


# ── Active generation & metadata wizard ──────────────────────────────────

def suno_progress_text(label: str, waited: int = 0, raw_status: str = "") -> str:
    if waited == 0 and not raw_status:
        stage = "Отправляю задачу в Suno"
    elif waited < 25:
        stage = "Собираю аранжировку"
    elif waited < 75:
        stage = "Пишу вокал и инструменты"
    elif waited < 140:
        stage = "Свожу дорожки"
    else:
        stage = "Финальный рендер"
    timer = f" · <code>{_fmt_duration(waited)}</code>" if waited > 0 else ""
    return (
        f"{tg_icon('working', '⏳')} <b>{escape(label)}</b>{timer}\n"
        f"{stage}\n\n"
        "<i>Обычно занимает 1–3 минуты, вернёт два варианта.</i>"
    )


def suno_cancel_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        ui_button("Остановить", callback_data=f"sunostop:{request_id}", style="danger"),
    ]])


def lyria_progress_text(label: str, prompt: str) -> str:
    return (
        f"{tg_icon('working', '⏳')} <b>{escape(label)}</b>\n"
        f"{_idea_quote(prompt, 240)}\n\n"
        "<i>Генерирую дорожку…</i>"
    )


def suno_meta_text(meta: dict, field: str) -> str:
    tracks = meta.get("tracks") or []
    step_num = {"title": 1, "author": 2, "lyrics": 3}.get(field, 1)
    header = f"{tg_icon('music', '♪')} <b>Треки готовы</b> · <i>теги {step_num}/3</i>"

    if field == "title":
        cur_titles = [t.get("title") or f"Suno {i}" for i, t in enumerate(tracks, 1)]
        preview = " / ".join(f"<code>{escape(str(t)[:40])}</code>" for t in cur_titles) if cur_titles else "<i>по умолчанию</i>"
        return f"{header}\nСейчас: {preview}\n\nИзменить название трека?"

    if field == "author":
        return f"{header}\nСейчас: <code>Suno</code>\n\nИзменить имя автора?"

    sample_lyrics = next(((t.get("prompt") or "").strip() for t in tracks if (t.get("prompt") or "").strip()), "")
    if sample_lyrics:
        quote = f"\n<blockquote expandable>{escape(sample_lyrics[:450])}</blockquote>"
    else:
        quote = "\nСейчас: <i>без текста</i>"
    return f"{header}{quote}\n\nИзменить метаданные текста?"


def suno_meta_keyboard(request_id: str, field: str) -> InlineKeyboardMarkup:
    rows = [[
        ui_button("Изменить", callback_data=f"sunometa:{request_id}:{field}:edit", style="primary", icon="pencil"),
        ui_button("Оставить", callback_data=f"sunometa:{request_id}:{field}:keep"),
    ]]
    if field == "title":
        rows.append([
            ui_button("Отправить как есть", callback_data=f"sunometa:{request_id}:title:skip", style="success", icon="play"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def track_tags_keyboard(tags: str) -> Optional[InlineKeyboardMarkup]:
    clean = " ".join(str(tags or "").split()).strip()
    if not clean or not _HAS_COPY_TEXT:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        ui_button("Скопировать теги стиля", copy_text=clean[:256]),
    ]])
