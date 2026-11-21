"""Compact, clean UI screens and keyboards for Suno Studio & Track Actions."""

from html import escape
from typing import Optional

from aiogram.types import InlineKeyboardMarkup

from handlers.music_ui import (
    _cut,
    _fmt_duration,
    _idea_quote,
    tg_icon,
    ui_button,
)
from services.suno_service import (
    SUNO_MODELS,
    SUNO_SINGER_SKILLS,
    SUNO_SOUND_KEYS,
    SUNO_STEM_NAMES,
    SUNO_VOICE_LANGUAGES,
)

_MODEL_CYCLE = ('V6', 'V6_WILD', 'V6_MINI')
_MODEL_LABELS = {'V6': 'V6', 'V6_WILD': 'V6 Wild', 'V6_MINI': 'V6 Mini'}
_SKILL_MAP = dict(SUNO_SINGER_SKILLS)
_LANG_MAP = dict(SUNO_VOICE_LANGUAGES)


def _next_model(cur: str) -> str:
    try:
        idx = _MODEL_CYCLE.index(cur)
        return _MODEL_CYCLE[(idx + 1) % len(_MODEL_CYCLE)]
    except ValueError:
        return 'V6'


# ── Main Suno Studio Hub ─────────────────────────────────────────────────

def studio_hub_text() -> str:
    return (
        f"{tg_icon('music', '♪')} <b>Suno Студия</b>\n\n"
        "Все инструменты sunoapi.org в одном меню:\n"
        "тексты, звуки, речь, обработка аудио, стемы, MIDI, голоса и треки."
    )


def studio_hub_keyboard(back_rid: str = '') -> InlineKeyboardMarkup:
    rows = [
        [
            ui_button("Текст песни", callback_data="ss:lyrics", icon="pencil"),
            ui_button("Буст стиля", callback_data="ss:boost", icon="sparkle"),
        ],
        [
            ui_button("Звуки / SFX", callback_data="ss:sounds", icon="lightning"),
            ui_button("Речь (Speech)", callback_data="ss:speech", icon="microphone"),
        ],
        [
            ui_button("Аудио-студия", callback_data="ss:audio", icon="music"),
            ui_button("Стемы и MIDI", callback_data="ss:stems", icon="chart"),
        ],
        [
            ui_button("Голоса и персоны", callback_data="ss:voices", icon="microphone"),
            ui_button("Мои треки", callback_data="ss:tracks:0", icon="play"),
        ],
        [
            ui_button("Баланс и утилиты", callback_data="ss:utils", icon="info"),
            ui_button(
                "← Назад" if back_rid else "Закрыть",
                callback_data=f"musicprovback:{back_rid}" if back_rid else "ss:close",
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Audio Studio Sub-Hub ─────────────────────────────────────────────────

def studio_audio_text() -> str:
    return (
        f"{tg_icon('music', '♪')} <b>Аудио-студия</b>\n\n"
        "Принимает аудио/голосовое/документ из Telegram или прямую ссылку.\n"
        "Файлы загружаются автоматически через временный хостинг Suno."
    )


def studio_audio_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ui_button("Кавер аудио", callback_data="ss:act:ucover", icon="music"),
            ui_button("Продлить файл", callback_data="ss:act:uextend", icon="play"),
        ],
        [
            ui_button("+ Инструментал", callback_data="ss:act:addinst", icon="sparkle"),
            ui_button("+ Вокал", callback_data="ss:act:addvoc", icon="microphone"),
        ],
        [
            ui_button("Мэшап (2 аудио)", callback_data="ss:act:mashup", icon="lightning"),
            ui_button("Заменить фрагмент", callback_data="ss:act:replace", icon="pencil"),
        ],
        [ui_button("← Студия", callback_data="ss:hub")],
    ])


# ── Generic Studio Action Config Screen (Extend/Cover/AddInst/AddVoc/Mashup/Replace) ──

_ACTION_TITLES = {
    'extend': 'Продлить трек',
    'ucover': 'Кавер аудио',
    'uextend': 'Продлить файл',
    'addinst': 'Добавить инструментал',
    'addvoc': 'Добавить вокал',
    'mashup': 'Мэшап (2 аудио)',
    'replace': 'Заменить фрагмент',
}


def studio_action_text(sid: str, sess: dict) -> str:
    kind = sess['kind']
    title = _ACTION_TITLES.get(kind, 'Задача')
    model = _MODEL_LABELS.get(sess.get('model', 'V6'), sess.get('model', 'V6'))
    lines = [f"{tg_icon('music', '♪')} <b>{title}</b> · <code>{model}</code>"]

    # Source summary
    src_label = sess.get('source_label') or ''
    if kind == 'mashup':
        urls = sess.get('urls') or []
        lines.append(f"Аудио: <code>{len(urls)} / 2</code>" + (f" · {escape(_cut(src_label, 60))}" if src_label else ''))
    elif src_label:
        lines.append(f"Источник: <code>{escape(_cut(src_label, 80))}</code>")
    elif sess.get('upload_url'):
        lines.append("Источник: <code>файл загружен</code>")
    elif sess.get('task_id') and sess.get('audio_id'):
        lines.append(f"Трек: <code>{escape(sess['audio_id'][:14])}…</code>")
    else:
        lines.append("Источник: <i>не выбран</i>")

    lines.append("")
    if kind in ('extend', 'uextend'):
        cat = sess.get('continue_at')
        lines.append(f"Старт с: <code>{_fmt_duration(int(cat))}</code>" if cat else "Старт с: <i>авто (конец)</i>")
    if kind == 'replace':
        s0, s1 = sess.get('infill_start'), sess.get('infill_end')
        if s0 is not None and s1 is not None:
            lines.append(f"Интервал: <code>{s0:.1f}с – {s1:.1f}с</code>")
        else:
            lines.append("Интервал: <i>не задан (мин. 10с)</i>")
        seg = (sess.get('prompt') or '').strip()
        full = (sess.get('full_lyrics') or '').strip()
        lines.append(f"Текст фрагмента: <code>{escape(_cut(seg, 60))}</code>" if seg else "Текст фрагмента: <i>не задан</i>")
        lines.append(f"Полный текст: <code>{len(full)} симв.</code>" if full else "Полный текст: <i>не задан</i>")

    style = (sess.get('style') or '').strip()
    lines.append(f"Стиль: <code>{escape(_cut(style, 80))}</code>" if style else "Стиль: <i>не задан</i>")

    if kind not in ('replace', 'addinst'):
        inst = bool(sess.get('instrumental'))
        if kind not in ('addvoc', 'mashup'):
            lines.append(f"Режим: <code>{'инструментал' if inst else 'с вокалом'}</code>")
        lyr = (sess.get('lyrics') or '').strip()
        if not inst:
            lines.append(f"Текст: <code>{escape(_cut(lyr, 60))}</code>" if lyr else "Текст: <i>авто</i>")

    ttl = (sess.get('title') or '').strip()
    if ttl:
        lines.append(f"Название: <code>{escape(ttl)}</code>")
    neg = (sess.get('neg') or '').strip()
    if neg:
        lines.append(f"Исключить: <code>{escape(_cut(neg, 60))}</code>")
    if sess.get('persona_label'):
        lines.append(f"Персона/голос: <code>{escape(sess['persona_label'])}</code>")
    if kind in ('ucover', 'mashup') and sess.get('duration'):
        lines.append(f"Длительность: <code>{_fmt_duration(int(sess['duration']))}</code>")

    return "\n".join(lines)


def studio_action_keyboard(sid: str, sess: dict) -> InlineKeyboardMarkup:
    kind = sess['kind']
    model = sess.get('model', 'V6')
    next_m = _next_model(model)
    rows = [
        [ui_button("Запустить", callback_data=f"ssa:run:{sid}", style="success", icon="play")],
    ]

    if kind == 'mashup':
        n = len(sess.get('urls') or [])
        rows.append([ui_button(f"Загрузить аудио ({n}/2)", callback_data=f"ssa:inp:{sid}:upload", icon="attachment")])
    elif kind in ('ucover', 'uextend', 'addinst', 'addvoc', 'replace'):
        if not (sess.get('task_id') and sess.get('audio_id')):
            rows.append([ui_button("Загрузить аудио / URL", callback_data=f"ssa:inp:{sid}:upload", icon="attachment")])

    if kind in ('extend', 'uextend'):
        rows.append([
            ui_button("Точка старта (сек)", callback_data=f"ssa:inp:{sid}:continue_at"),
            ui_button(f"Модель · {_MODEL_LABELS.get(model, model)}", callback_data=f"ssa:set:{sid}:model:{next_m}"),
        ])
    elif kind == 'replace':
        rows.append([
            ui_button("Интервал (с–с)", callback_data=f"ssa:inp:{sid}:infill_range"),
            ui_button("Текст фрагмента", callback_data=f"ssa:inp:{sid}:prompt", icon="pencil"),
        ])
        rows.append([
            ui_button("Полный текст", callback_data=f"ssa:inp:{sid}:full_lyrics", icon="pencil"),
            ui_button("Стиль", callback_data=f"ssa:inp:{sid}:style", icon="pencil"),
        ])
        rows.append([
            ui_button("Название", callback_data=f"ssa:inp:{sid}:title"),
            ui_button("Исключить", callback_data=f"ssa:inp:{sid}:neg"),
        ])
    else:
        rows.append([
            ui_button("Стиль", callback_data=f"ssa:inp:{sid}:style", icon="pencil"),
            ui_button("Текст песни", callback_data=f"ssa:inp:{sid}:lyrics", icon="pencil"),
        ])
        rows.append([
            ui_button("Название", callback_data=f"ssa:inp:{sid}:title"),
            ui_button("Исключить", callback_data=f"ssa:inp:{sid}:neg"),
        ])

    if kind in ('extend', 'uextend'):
        rows.append([
            ui_button("Стиль", callback_data=f"ssa:inp:{sid}:style", icon="pencil"),
            ui_button("Текст песни", callback_data=f"ssa:inp:{sid}:lyrics", icon="pencil"),
        ])
        rows.append([
            ui_button("Название", callback_data=f"ssa:inp:{sid}:title"),
            ui_button("Исключить", callback_data=f"ssa:inp:{sid}:neg"),
        ])

    if kind not in ('replace', 'addinst', 'addvoc', 'mashup'):
        inst = bool(sess.get('instrumental'))
        row_t = [
            ui_button(
                f"Инструментал: {'вкл' if inst else 'выкл'}",
                callback_data=f"ssa:set:{sid}:instrumental:{'0' if inst else '1'}",
                style="primary" if inst else None,
            ),
        ]
        if kind != 'extend' and kind != 'uextend':
            row_t.append(ui_button(f"Модель · {_MODEL_LABELS.get(model, model)}", callback_data=f"ssa:set:{sid}:model:{next_m}"))
        rows.append(row_t)
    elif kind in ('addinst', 'addvoc', 'mashup'):
        rows.append([ui_button(f"Модель · {_MODEL_LABELS.get(model, model)}", callback_data=f"ssa:set:{sid}:model:{next_m}")])

    if kind in ('extend', 'ucover', 'uextend', 'mashup', 'replace'):
        rows.append([ui_button("Персона / голос", callback_data=f"ssa:persona:{sid}", icon="microphone")])

    back_cb = f"sst:open:{sess['track_db_id']}" if sess.get('track_db_id') else "ss:audio"
    rows.append([
        ui_button("← Назад", callback_data=back_cb),
        ui_button("Отмена", callback_data="ss:close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Sounds / SFX Screen ──────────────────────────────────────────────────

def studio_sounds_text(sid: str, sess: dict) -> str:
    prompt = (sess.get('prompt') or '').strip()
    model = _MODEL_LABELS.get(sess.get('model', 'V6'), 'V6')
    loop = bool(sess.get('sound_loop'))
    bpm = sess.get('sound_tempo')
    key = sess.get('sound_key', 'Any')
    grab = bool(sess.get('grab_lyrics'))
    return "\n".join([
        f"{tg_icon('lightning', '⚡')} <b>Звуки / SFX</b> · <code>{model}</code>",
        _idea_quote(prompt, 260) if prompt else "<blockquote><i>Опиши звук, луп или эмбиент (до 500 симв.)</i></blockquote>",
        "",
        f"Луп: <code>{'вкл' if loop else 'выкл'}</code> · BPM: <code>{bpm if bpm else 'авто'}</code>",
        f"Тональность: <code>{escape(key)}</code> · Субтитры: <code>{'вкл' if grab else 'выкл'}</code>",
    ])


def studio_sounds_keyboard(sid: str, sess: dict) -> InlineKeyboardMarkup:
    model = sess.get('model', 'V6')
    loop = bool(sess.get('sound_loop'))
    grab = bool(sess.get('grab_lyrics'))
    bpm = sess.get('sound_tempo')
    key = sess.get('sound_key', 'Any')
    next_key = SUNO_SOUND_KEYS[(SUNO_SOUND_KEYS.index(key) + 1) % len(SUNO_SOUND_KEYS)] if key in SUNO_SOUND_KEYS else 'Any'
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Сгенерировать", callback_data=f"sss:run:{sid}", style="success", icon="play")],
        [
            ui_button("Описание звука", callback_data=f"sss:inp:{sid}:prompt", icon="pencil"),
            ui_button(f"Модель · {_MODEL_LABELS.get(model, model)}", callback_data=f"sss:set:{sid}:model:{_next_model(model)}"),
        ],
        [
            ui_button(f"Луп: {'вкл' if loop else 'выкл'}", callback_data=f"sss:set:{sid}:sound_loop:{'0' if loop else '1'}", style="primary" if loop else None),
            ui_button(f"BPM: {bpm if bpm else 'авто'}", callback_data=f"sss:inp:{sid}:sound_tempo"),
        ],
        [
            ui_button(f"Тональность: {key}", callback_data=f"sss:set:{sid}:sound_key:{next_key}"),
            ui_button(f"Субтитры: {'вкл' if grab else 'выкл'}", callback_data=f"sss:set:{sid}:grab_lyrics:{'0' if grab else '1'}", style="primary" if grab else None),
        ],
        [
            ui_button("← Студия", callback_data="ss:hub"),
            ui_button("Отмена", callback_data="ss:close"),
        ],
    ])


# ── Speech Screen ────────────────────────────────────────────────────────

def studio_speech_text(sid: str, sess: dict) -> str:
    adv = bool(sess.get('custom_mode'))
    mode_label = "точный текст" if adv else "простой режим"
    lines = [f"{tg_icon('microphone', '🎙')} <b>Речь (Suno Speech)</b> · <i>{mode_label}</i> · <code>V6 Mini</code>"]
    if not adv:
        prompt = (sess.get('prompt') or '').strip()
        lines.append(_idea_quote(prompt, 280) if prompt else "<blockquote><i>Опиши сцену — модель сама напишет текст речи</i></blockquote>")
    else:
        script = (sess.get('script') or '').strip()
        tone = (sess.get('tone') or '').strip()
        vocal = {'m': 'мужской', 'f': 'женский'}.get(sess.get('vocal_gender', ''), 'любой')
        var = sess.get('variety', 1)
        bg = sess.get('background_music', False)
        lines.append(_idea_quote(script, 280) if script else "<blockquote><i>Укажи точный текст для озвучки (1–5000 симв.)</i></blockquote>")
        lines.append("")
        lines.append(f"Тон и подача: <code>{escape(_cut(tone, 80))}</code>" if tone else "Тон и подача: <i>по умолчанию</i>")
        lines.append(f"Голос: <code>{vocal}</code> · Вариации: <code>{var}/4</code> · Фон: <code>{'вкл' if bg else 'выкл'}</code>")
    return "\n".join(lines)


def studio_speech_keyboard(sid: str, sess: dict) -> InlineKeyboardMarkup:
    adv = bool(sess.get('custom_mode'))
    rows = [
        [ui_button("Сгенерировать речь", callback_data=f"ssp:run:{sid}", style="success", icon="play")],
        [
            ui_button("Простой", callback_data=f"ssp:set:{sid}:custom_mode:0", style="primary" if not adv else None),
            ui_button("Точный текст", callback_data=f"ssp:set:{sid}:custom_mode:1", style="primary" if adv else None),
        ],
    ]
    if not adv:
        rows.append([ui_button("Описание сцены", callback_data=f"ssp:inp:{sid}:prompt", icon="pencil")])
    else:
        vocal = sess.get('vocal_gender', '')
        next_v = {'': 'm', 'm': 'f', 'f': ''}.get(vocal, '')
        v_lbl = {'': 'Любой', 'm': 'Мужской', 'f': 'Женский'}.get(vocal, 'Любой')
        var = int(sess.get('variety', 1))
        bg = bool(sess.get('background_music'))
        rows.append([
            ui_button("Текст речи", callback_data=f"ssp:inp:{sid}:script", icon="pencil"),
            ui_button("Тон / эмоции", callback_data=f"ssp:inp:{sid}:tone", icon="pencil"),
        ])
        rows.append([
            ui_button(f"Голос: {v_lbl}", callback_data=f"ssp:set:{sid}:vocal_gender:{next_v}"),
            ui_button(f"Вариации: {var}/4", callback_data=f"ssp:set:{sid}:variety:{(var + 1) % 5}"),
        ])
        rows.append([
            ui_button(
                f"Фоновая музыка: {'вкл' if bg else 'выкл'}",
                callback_data=f"ssp:set:{sid}:background_music:{'0' if bg else '1'}",
                style="primary" if bg else None,
            ),
        ])
    rows.append([
        ui_button("← Студия", callback_data="ss:hub"),
        ui_button("Отмена", callback_data="ss:close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Stems & MIDI Screens ─────────────────────────────────────────────────

def studio_stems_hub_text(recent_stems: list[dict]) -> str:
    lines = [
        f"{tg_icon('chart', '🎛')} <b>Стемы и MIDI</b>",
        "",
        "• <b>Вокал + Минус</b> — 2 дорожки <code>10 кр.</code>",
        "• <b>12 стемов</b> — полный разбор <code>50 кр.</code>",
        "• <b>1 инструмент</b> — выбор из 98 <code>20 кр.</code>",
        "• <b>MIDI / WAV</b> — из любого готового разделения",
    ]
    if recent_stems:
        lines.append("")
        lines.append(f"<i>Недавние разделения: {len(recent_stems)}</i>")
    return "\n".join(lines)


def studio_stems_hub_keyboard(recent_stems: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            ui_button("Из моих треков", callback_data="ss:tracks:0:stems", icon="music"),
            ui_button("Загрузить аудио", callback_data="sssep:upload", icon="attachment"),
        ],
    ]
    for item in recent_stems[:4]:
        title = _cut(item.get('source_title') or item['task_id'][:10], 26)
        rows.append([ui_button(f"Стемы · {title}", callback_data=f"sssep:view:{item['id']}")])
    rows.append([ui_button("← Студия", callback_data="ss:hub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def studio_stem_mode_text(source_label: str) -> str:
    return (
        f"{tg_icon('chart', '🎛')} <b>Разделение на стемы</b>\n"
        f"Источник: <code>{escape(_cut(source_label, 80))}</code>\n\n"
        "Выбери режим:"
    )


def studio_stem_mode_keyboard(sid: str, back_cb: str = "ss:stems") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Вокал + Минус (2 стема · 10 кр.)", callback_data=f"sssep:go:{sid}:separate_vocal", style="primary")],
        [ui_button("Полный разбор (12 стемов · 50 кр.)", callback_data=f"sssep:go:{sid}:split_stem")],
        [ui_button("Выбрать инструмент (98 видов · 20 кр.)", callback_data=f"sssep:pick:{sid}:0")],
        [ui_button("← Назад", callback_data=back_cb)],
    ])


def studio_stem_picker_text(page: int, total_pages: int, query: str = '') -> str:
    q_line = f"\nФильтр: <code>{escape(query)}</code>" if query else ""
    return (
        f"{tg_icon('chart', '🎛')} <b>Выбор инструмента</b> · <i>стр. {page + 1}/{total_pages}</i>{q_line}\n\n"
        "Нажми на инструмент или напиши название в чат для поиска."
    )


def studio_stem_picker_keyboard(sid: str, page: int = 0, query: str = '') -> tuple[str, InlineKeyboardMarkup]:
    q = (query or '').strip().lower()
    items = [s for s in SUNO_STEM_NAMES if q in s.lower()] if q else list(SUNO_STEM_NAMES)
    per_page = 12
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * per_page:(page + 1) * per_page]

    rows = []
    for i in range(0, len(chunk), 2):
        row = []
        for stem in chunk[i:i + 2]:
            idx = SUNO_STEM_NAMES.index(stem)
            row.append(ui_button(stem, callback_data=f"sssep:adv:{sid}:{idx}"))
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(ui_button("←", callback_data=f"sssep:pick:{sid}:{page - 1}"))
    nav.append(ui_button(f"{page + 1}/{total_pages}", callback_data="ss:noop"))
    if page + 1 < total_pages:
        nav.append(ui_button("→", callback_data=f"sssep:pick:{sid}:{page + 1}"))
    rows.append(nav)
    rows.append([ui_button("← Режимы", callback_data=f"sssep:modes:{sid}")])
    return studio_stem_picker_text(page, total_pages, query), InlineKeyboardMarkup(inline_keyboard=rows)


def studio_stems_result_keyboard(stems_db_id: int, stems: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [ui_button("Сгенерировать MIDI (все стемы)", callback_data=f"ssmidi:all:{stems_db_id}", style="primary", icon="music")],
    ]
    for idx, st in enumerate(stems[:8]):
        if st.get('id'):
            name = _cut(st.get('name') or f'Stem {idx + 1}', 18)
            rows.append([
                ui_button(f"WAV · {name}", callback_data=f"ssswav:{stems_db_id}:{idx}"),
                ui_button(f"MIDI · {name}", callback_data=f"ssmidi:one:{stems_db_id}:{idx}"),
            ])
    rows.append([ui_button("← Стемы и MIDI", callback_data="ss:stems")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Track Library & Per-Track Action Card ────────────────────────────────

def track_compact_keyboard(track_db_id: Optional[int], tags: str = '') -> Optional[InlineKeyboardMarkup]:
    rows = []
    if track_db_id:
        rows.append([ui_button("Действия с треком", callback_data=f"sst:open:{track_db_id}", style="primary", icon="music")])
    clean = " ".join(str(tags or "").split()).strip()
    if clean:
        rows.append([ui_button("Скопировать теги", copy_text=clean[:256])])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def track_actions_text(track: dict) -> str:
    title = escape(track.get('title') or 'Без названия')
    dur = _fmt_duration(int(float(track.get('duration') or 0))) if track.get('duration') else '—'
    model = escape(track.get('model') or 'V6')
    tags = _cut(track.get('tags') or '', 120)
    lines = [
        f"{tg_icon('music', '♪')} <b>{title}</b>",
        f"<code>{dur}</code> · <code>{model}</code> · ID <code>{escape(track['audio_id'][:12])}…</code>",
    ]
    if tags:
        lines.append(f"<i>{escape(tags)}</i>")
    lines.append("")
    lines.append("Выбери действие с этим треком:")
    return "\n".join(lines)


def track_actions_keyboard(track_db_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ui_button("Продлить", callback_data=f"sst:extend:{track_db_id}", icon="play"),
            ui_button("Заменить фрагмент", callback_data=f"sst:replace:{track_db_id}", icon="pencil"),
        ],
        [
            ui_button("Кавер (новый стиль)", callback_data=f"sst:cover:{track_db_id}", icon="sparkle"),
            ui_button("Стемы / Вокал", callback_data=f"sst:stems:{track_db_id}", icon="chart"),
        ],
        [
            ui_button("В WAV (lossless)", callback_data=f"sst:wav:{track_db_id}", icon="download"),
            ui_button("Клип MP4", callback_data=f"sst:mp4:{track_db_id}", icon="play"),
        ],
        [
            ui_button("Обложка (Cover Art)", callback_data=f"sst:art:{track_db_id}", icon="sparkle"),
            ui_button("Таймкоды / LRC", callback_data=f"sst:lrc:{track_db_id}", icon="pencil"),
        ],
        [
            ui_button("Создать персону", callback_data=f"sst:persona:{track_db_id}", icon="microphone"),
            ui_button("Восстановить URL", callback_data=f"sst:recover:{track_db_id}", icon="link"),
        ],
        [
            ui_button("← Мои треки", callback_data="ss:tracks:0"),
            ui_button("Закрыть", callback_data="ss:close"),
        ],
    ])


def my_tracks_screen(tracks: list[dict], page: int, mode: str = 'open') -> tuple[str, InlineKeyboardMarkup]:
    per_page = 6
    total_pages = max(1, (len(tracks) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = tracks[page * per_page:(page + 1) * per_page]

    header = "Выбери трек для разделения" if mode == 'stems' else "Мои треки Suno"
    if not chunk:
        text = (
            f"{tg_icon('music', '♪')} <b>{header}</b>\n\n"
            "Пока нет сохранённых треков. Сгенерируй трек через /music или введи ID вручную."
        )
    else:
        text = f"{tg_icon('music', '♪')} <b>{header}</b> · <i>стр. {page + 1}/{total_pages}</i>\n\nВыбери трек:"

    rows = []
    for t in chunk:
        dur = _fmt_duration(int(float(t.get('duration') or 0))) if t.get('duration') else ''
        lbl = _cut(t.get('title') or 'Suno', 30)
        suffix = f" · {dur}" if dur else ""
        cb = f"sst:stems:{t['id']}" if mode == 'stems' else f"sst:open:{t['id']}"
        rows.append([ui_button(f"{lbl}{suffix}", callback_data=cb)])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(ui_button("←", callback_data=f"ss:tracks:{page - 1}:{mode}"))
        nav.append(ui_button(f"{page + 1}/{total_pages}", callback_data="ss:noop"))
        if page + 1 < total_pages:
            nav.append(ui_button("→", callback_data=f"ss:tracks:{page + 1}:{mode}"))
        rows.append(nav)

    rows.append([
        ui_button("По taskId / audioId", callback_data="sst:manual", icon="search"),
        ui_button("← Студия", callback_data="ss:hub"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ── Voices & Personas Hub ────────────────────────────────────────────────

def voices_hub_text(personas: list[dict], voices: list[dict]) -> str:
    return (
        f"{tg_icon('microphone', '🎙')} <b>Голоса и персоны</b>\n\n"
        f"• <b>Персоны стиля</b> (<code>style_persona</code>): <code>{len(personas)}</code>\n"
        f"• <b>Кастомные голоса</b> (<code>voice_persona</code>): <code>{len(voices)}</code>\n\n"
        "Персону можно создать из любого трека Suno, а кастомный голос — обучить по своей записи."
    )


def voices_hub_keyboard(personas: list[dict], voices: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [ui_button("Создать голос (Suno Voice)", callback_data="ssv:new", style="success", icon="microphone")],
    ]
    for v in voices[:4]:
        rows.append([
            ui_button(f"Голос · {_cut(v['name'], 22)}", callback_data=f"ssv:view:{v['id']}"),
        ])
    for p in personas[:4]:
        rows.append([
            ui_button(f"Персона · {_cut(p['name'], 22)}", callback_data=f"ssv:pview:{p['id']}"),
        ])
    rows.append([
        ui_button("Создать персону из трека", callback_data="ss:tracks:0", icon="sparkle"),
        ui_button("← Студия", callback_data="ss:hub"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def voice_detail_text(v: dict, avail: Optional[bool] = None) -> str:
    lines = [
        f"{tg_icon('microphone', '🎙')} <b>Голос · {escape(v['name'])}</b>",
        f"Voice ID: <code>{escape(v['voice_id'])}</code>",
        f"Task ID: <code>{escape(v['task_id'])}</code>",
        f"Навык: <code>{_SKILL_MAP.get(v.get('skill_level', ''), v.get('skill_level', '—'))}</code> · Язык: <code>{_LANG_MAP.get(v.get('language', ''), v.get('language', '—'))}</code>",
    ]
    if v.get('style'):
        lines.append(f"Стиль: <code>{escape(v['style'])}</code>")
    if avail is not None:
        lines.append(f"Статус API: <code>{'доступен' if avail else 'недоступен'}</code>")
    return "\n".join(lines)


def voice_detail_keyboard(v: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ui_button("Проверить доступность", callback_data=f"ssv:check:{v['id']}", style="primary"),
            ui_button("Удалить", callback_data=f"ssv:del:{v['id']}", style="danger"),
        ],
        [ui_button("← Голоса и персоны", callback_data="ss:voices")],
    ])


def persona_detail_text(p: dict) -> str:
    lines = [
        f"{tg_icon('sparkle', '✦')} <b>Персона · {escape(p['name'])}</b>",
        f"Persona ID: <code>{escape(p['persona_id'])}</code>",
    ]
    if p.get('style'):
        lines.append(f"Стиль: <code>{escape(p['style'])}</code>")
    if p.get('description'):
        lines.append(f"<blockquote expandable>{escape(p['description'])}</blockquote>")
    return "\n".join(lines)


def persona_detail_keyboard(p: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Удалить персону", callback_data=f"ssv:pdel:{p['id']}", style="danger")],
        [ui_button("← Голоса и персоны", callback_data="ss:voices")],
    ])


# ── Utilities & Credits ──────────────────────────────────────────────────

def studio_utils_text() -> str:
    return (
        f"{tg_icon('info', 'ℹ')} <b>Баланс и утилиты</b>\n\n"
        "Проверка кредитов на ключах, статус задач, восстановление протухших ссылок "
        "и временный хостинг файлов (3 дня)."
    )


def studio_utils_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ui_button("Баланс кредитов", callback_data="ssu:credits", style="primary", icon="chart"),
            ui_button("Статус задачи", callback_data="ssu:status", icon="search"),
        ],
        [
            ui_button("Восстановить ссылки", callback_data="ssu:recover", icon="link"),
            ui_button("Загрузить файл / URL", callback_data="ssu:upload", icon="attachment"),
        ],
        [ui_button("← Студия", callback_data="ss:hub")],
    ])


# ── Multimodal References Screen (Non-Custom Mode of /api/v1/generate) ───

def suno_refs_text(request_id: str, d: dict) -> str:
    cfg = d.get('cfg', {})
    imgs = cfg.get('image_urls') or []
    vids = cfg.get('video_urls') or []
    auds = cfg.get('audio_urls') or []
    mids = cfg.get('midi_urls') or []
    total = len(imgs) + len(vids) + len(auds) + len(mids) + (1 if cfg.get('style') else 0) + (1 if cfg.get('lyrics') else 0)
    return "\n".join([
        f"{tg_icon('attachment', '📎')} <b>Референсы (авто-режим)</b> · <code>{total}/10</code>",
        _idea_quote(d.get('prompt', ''), 200),
        "",
        f"Фото: <code>{len(imgs)}/5</code> · Видео: <code>{len(vids)}/1</code>",
        f"Аудио: <code>{len(auds)}</code> · MIDI: <code>{len(mids)}/1</code>",
        "",
        "<i>В авто-режиме (customMode=false) Suno опирается на идею и загруженные референсы.</i>",
    ])


def suno_refs_keyboard(request_id: str, d: dict) -> InlineKeyboardMarkup:
    cfg = d.get('cfg', {})
    has_refs = any(cfg.get(k) for k in ('image_urls', 'video_urls', 'audio_urls', 'midi_urls'))
    rows = [
        [ui_button("Сгенерировать", callback_data=f"sunogen:{request_id}", style="success", icon="play")],
        [
            ui_button("+ Фото (до 5)", callback_data=f"sunoref:{request_id}:image", icon="attachment"),
            ui_button("+ Видео (до 1)", callback_data=f"sunoref:{request_id}:video", icon="attachment"),
        ],
        [
            ui_button("+ Аудио", callback_data=f"sunoref:{request_id}:audio", icon="attachment"),
            ui_button("+ MIDI (.mid)", callback_data=f"sunoref:{request_id}:midi", icon="attachment"),
        ],
    ]
    if has_refs:
        rows.append([ui_button("Очистить референсы", callback_data=f"sunorefclear:{request_id}", style="danger")])
    rows.append([
        ui_button("← Все настройки", callback_data=f"sunoall:{request_id}"),
        ui_button("Отмена", callback_data=f"sunocancel:{request_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
