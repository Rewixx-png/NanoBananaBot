
from services.deepseek_service import deepseek_text


async def explain_generation_error(prompt: str, error_msg: str, image_bytes: bytes=None) -> str:
    # Fast-path: don't waste an API call explaining obvious rate-limit errors
    if error_msg and any(kw in error_msg.lower() for kw in (
        'все api ключи исчерпали лимит', 'все ключи проебаны', 'нет ключей',
        'rate limit', 'quota exceeded', '429', 'resource has been exhausted',
    )):
        return 'Ключи перегружены — слишком много запросов. Подожди минуту и попробуй снова.'
    user_text = (
        f"Пользователь пытался сгенерировать {('видео' if image_bytes else 'картинку')}.\n"
        f"Промпт: {prompt or '<без текста>'}\n"
        f"Ошибка API: {error_msg[:400]}\n"
        f"{('К фото прикреплено изображение — причина бана может быть в нём.' if image_bytes else '')}\n"
        f"Объясни ОЧЕНЬ коротко и агрессивно (1-2 предложения) почему получил бан. "
        f"Если причина в фото — укажи что именно на нём нарушает правила. "
        f"Если в промпте — скажи на что триггернуло."
    )
    result = await deepseek_text(
        user_text, system_prompt='',
        model='deepseek-chat', temperature=0.5, max_tokens=200, timeout=25,
    )
    return (result or '').strip()
