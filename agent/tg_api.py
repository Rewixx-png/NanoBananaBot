"""Telegram Bot API tools and dispatch for NanoHatani agent."""

import logging
from typing import Callable, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

_PRIVILEGED = {
    "tg_ban_user", "tg_unban_user", "tg_kick_user", "tg_restrict_member", "tg_promote_member",
    "tg_delete_message", "tg_pin_message", "tg_unpin_message",
    "tg_set_chat_title", "tg_set_chat_description", "tg_set_chat_photo",
    "tg_set_bot_photo", "tg_create_invite_link", "read_bot_logs",
}


async def _tg_api(method: str, params: dict) -> dict:
    """Direct Telegram Bot API call — returns parsed JSON."""
    from config import BOT_TOKEN as _TK
    url = f"https://api.telegram.org/bot{_TK}/{method}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=params,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def handle_tg_tool(
    name: str, args: dict, ws, _send: Callable, chat_id: int, _st: Callable = None
) -> Tuple[str, Optional[dict]]:
    """Handle all tg_* tool dispatch. Returns (result_text, project_dict_or_None)."""

    # ── Telegram API tools ───────────────────────────────────────────
    if name == "tg_send_poll":
        await _send({"type": "tg_poll", "question": args.get("question", ""),
                     "options": args.get("options", []),
                     "is_anonymous": args.get("is_anonymous", True),
                     "allows_multiple_answers": args.get("allows_multiple_answers", False)})
        return "[ОТПРАВЛЕНО] Опрос создан.", None

    if name == "tg_send_location":
        await _send({"type": "tg_location", "latitude": args.get("latitude"),
                     "longitude": args.get("longitude"),
                     "title": args.get("title"), "address": args.get("address")})
        return "[ОТПРАВЛЕНО] Локация отправлена.", None

    if name == "tg_react":
        await _send({"type": "tg_react", "emoji": args.get("emoji", "👍"),
                     "message_id": args.get("message_id")})
        return "Реакция добавлена.", None

    if name == "tg_pin_message":
        await _send({"type": "tg_pin", "message_id": args.get("message_id"),
                     "disable_notification": args.get("disable_notification", False)})
        return "Сообщение закреплено.", None

    if name == "tg_delete_message":
        await _send({"type": "tg_delete", "message_id": args.get("message_id")})
        return "Сообщение удалено.", None

    if name == "tg_forward_message":
        await _send({"type": "tg_forward", "from_chat_id": args.get("from_chat_id"),
                     "message_id": args.get("message_id")})
        return "[ОТПРАВЛЕНО] Сообщение переслано.", None

    if name == "tg_get_chat_info":
        r = await _tg_api("getChat", {"chat_id": chat_id})
        rc = await _tg_api("getChatMemberCount", {"chat_id": chat_id})
        if not r.get("ok"):
            return f"Ошибка: {r.get('description','unknown')}", None
        c = r["result"]
        count = rc.get("result", "?")
        result = (f"Чат: {c.get('title','N/A')}\nID: {c.get('id')}\n"
                  f"Тип: {c.get('type')}\nУчастников: {count}\n"
                  f"Описание: {c.get('description','—')}\nUsername: @{c.get('username','—')}")
        import html as _html
        await _st(f"ℹ️ <b>Инфо о чате:</b>\n<pre>{_html.escape(result)}</pre>")
        return result, None

    if name == "tg_ban_user":
        await _send({"type": "tg_ban", "user_id": args.get("user_id"),
                     "reason": args.get("reason", ""), "until_date": args.get("until_date")})
        return f"Пользователь {args.get('user_id')} заблокирован.", None

    if name == "tg_unban_user":
        await _send({"type": "tg_unban", "user_id": args.get("user_id")})
        return f"Пользователь {args.get('user_id')} разбанен.", None

    if name == "tg_kick_user":
        await _send({"type": "tg_kick", "user_id": args.get("user_id"),
                     "reason": args.get("reason", "")})
        return f"Пользователь {args.get('user_id')} кикнут.", None

    if name == "tg_send_chat_action":
        await _send({"type": "tg_chat_action", "action": args.get("action", "typing")})
        return "Действие отправлено.", None
    if name == "tg_restrict_member":
        await _send({"type": "tg_restrict", "user_id": args.get("user_id"),
                     "can_send_messages": args.get("can_send_messages", False),
                     "can_send_media": args.get("can_send_media", False),
                     "until_date": args.get("until_date")})
        return f"Пользователь {args.get('user_id')} ограничен.", None
    if name == "tg_unpin_message":
        await _send({"type": "tg_unpin", "message_id": args.get("message_id")})
        return "Сообщение откреплено.", None
    if name == "tg_create_invite_link":
        await _send({"type": "tg_invite_link", "name": args.get("name"),
                     "expire_date": args.get("expire_date"),
                     "member_limit": args.get("member_limit")})
        return "[ОТПРАВЛЕНО] Ссылка создана.", None
    if name == "tg_set_bot_photo":
        path = args.get("path", "")
        try:
            full = ws._safe_path(path)
            with open(full, "rb") as f:
                data = f.read()
        except Exception as e:
            return f"Не смог прочитать файл {path}: {e}", None
        await _send({"type": "tg_set_bot_photo", "data": data, "filename": path})
        return "[ОТПРАВЛЕНО] Аватарка бота установлена.", None
    if name == "tg_set_chat_description":
        await _send({"type": "tg_set_chat_description", "description": args.get("description", "")})
        return "Описание чата изменено.", None
    if name == "tg_set_chat_title":
        await _send({"type": "tg_set_chat_title", "title": args.get("title", "")})
        return "Название чата изменено.", None
    if name == "tg_copy_message":
        await _send({"type": "tg_copy_message", "from_chat_id": args.get("from_chat_id"),
                     "message_id": args.get("message_id"), "caption": args.get("caption")})
        return "[ОТПРАВЛЕНО] Сообщение скопировано.", None
    if name == "tg_send_sticker":
        await _send({"type": "tg_send_sticker", "sticker": args.get("sticker", "")})
        return "[ОТПРАВЛЕНО] Стикер отправлен.", None
    if name == "tg_send_contact":
        await _send({"type": "tg_send_contact", "phone": args.get("phone", ""),
                     "name": args.get("first_name", ""), "last_name": args.get("last_name", "")})
        return "[ОТПРАВЛЕНО] Контакт отправлен.", None
    if name == "tg_send_dice":
        await _send({"type": "tg_send_dice", "emoji": args.get("emoji", "🎲")})
        return "[ОТПРАВЛЕНО] Кубик брошен.", None
    if name == "tg_edit_message":
        await _send({"type": "tg_edit_message", "message_id": args.get("message_id"),
                     "text": args.get("text", "")})
        return "Сообщение отредактировано.", None
    if name == "tg_send_animation":
        await _send({"type": "tg_send_animation", "url": args.get("url",""),
                     "caption": args.get("caption","")})
        return "[ОТПРАВЛЕНО] GIF отправлен.", None
    if name == "tg_send_video_note":
        await _send({"type": "tg_send_video_note", "file_id": args.get("file_id","")})
        return "[ОТПРАВЛЕНО] Кружок отправлен.", None
    if name == "tg_send_venue":
        await _send({"type": "tg_send_venue", "latitude": args.get("latitude"),
                     "longitude": args.get("longitude"), "title": args.get("title",""),
                     "address": args.get("address","")})
        return "[ОТПРАВЛЕНО] Место отправлено.", None
    if name == "tg_promote_member":
        await _send({"type": "tg_promote", "user_id": args.get("user_id"),
                     "can_delete_messages": args.get("can_delete_messages", False),
                     "can_pin_messages": args.get("can_pin_messages", False),
                     "can_manage_chat": args.get("can_manage_chat", False),
                     "can_ban_members": args.get("can_ban_members", False),
                     "custom_title": args.get("custom_title","")})
        return f"Пользователь {args.get('user_id')} обновлён.", None
    if name == "tg_get_chat_member":
        r = await _tg_api("getChatMember", {"chat_id": chat_id, "user_id": args.get("user_id")})
        if not r.get("ok"):
            return f"Ошибка: {r.get('description','unknown')}", None
        m = r["result"]
        u = m.get("user", {})
        result = (f"Пользователь: {u.get('first_name','')} {u.get('last_name','')} "
                  f"(@{u.get('username','—')})\nID: {u.get('id')}\nСтатус: {m.get('status')}")
        return result, None

    if name == "tg_get_admins":
        r = await _tg_api("getChatAdministrators", {"chat_id": chat_id})
        if not r.get("ok"):
            return f"Ошибка: {r.get('description','unknown')}", None
        admins = []
        for m in r["result"]:
            u = m.get("user", {})
            name_str = f"{u.get('first_name','')} (@{u.get('username','—')}) [{m.get('status')}]"
            admins.append(name_str)
        return "Администраторы:\n" + "\n".join(admins), None

    if name == "tg_get_member_count":
        r = await _tg_api("getChatMemberCount", {"chat_id": chat_id})
        if not r.get("ok"):
            return f"Ошибка: {r.get('description','unknown')}", None
        return f"Участников в чате: {r['result']}", None
    if name == "tg_create_forum_topic":
        await _send({"type": "tg_create_forum_topic", "name": args.get("name",""),
                     "icon_emoji": args.get("icon_emoji","")})
        return "Топик создан.", None
    if name == "tg_close_forum_topic":
        await _send({"type": "tg_close_forum_topic",
                     "message_thread_id": args.get("message_thread_id")})
        return "Топик закрыт.", None
    if name == "tg_get_sticker_set":
        await _send({"type": "tg_get_sticker_set", "name": args.get("name","")})
        return "Инфо о стикер-паке запрошено.", None
    if name == "tg_approve_join_request":
        await _send({"type": "tg_approve_join", "user_id": args.get("user_id"),
                     "approve": args.get("approve", True)})
        return "Заявка обработана.", None
    if name == "tg_export_invite_link":
        await _send({"type": "tg_export_link"})
        return "Ссылка запрошена.", None
    if name == "tg_set_chat_photo":
        path = args.get("path", "")
        try:
            full = ws._safe_path(path)
            with open(full, "rb") as f:
                data = f.read()
        except Exception as e:
            return f"Не смог прочитать файл {path}: {e}", None
        await _send({"type": "tg_set_chat_photo", "data": data, "filename": path})
        return "[ОТПРАВЛЕНО] Аватарка чата установлена.", None

    return f"Unknown tg tool: {name}", None
