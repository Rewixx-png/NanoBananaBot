"""Extracted _agent_send_cb callback for agent tool dispatch.

This module handles every mtype the agent can emit: text, inline buttons, polls,
admin actions, media, stickers, dice, etc.  It was pulled out of handle_text_messages
in chat.py to keep that file manageable.
"""

import logging
from aiogram import types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMIN_IDS
from handlers.common import _clean_plain_reply, format_telegram_html, safe_send, send_rich_message

logger = logging.getLogger(__name__)

_ADMIN_MTYPES = {
    "tg_pin", "tg_unpin", "tg_delete",
    "tg_set_chat_title", "tg_set_chat_description", "tg_set_chat_photo",
    "tg_invite_link", "tg_export_link",
    "tg_create_forum_topic", "tg_close_forum_topic", "tg_approve_join",
}


async def send_agent_callback(media: dict, /, *, message: types.Message, reply_kwargs: dict) -> None:
    """Dispatch a single agent `media` payload to the appropriate Telegram action."""
    mtype = media.get("type", "document")
    kw = {"reply_to_message_id": message.message_id, **reply_kwargs}

    if mtype in _ADMIN_MTYPES and message.chat.type != "private":
        user_id = message.from_user.id if message.from_user else 0
        if user_id not in ADMIN_IDS:
            try:
                _req = await message.bot.get_chat_member(message.chat.id, user_id)
                is_chat_admin = _req.status in ("administrator", "creator")
            except Exception as _e:
                logger.warning(f"admin check failed for {mtype}: {_e}")
                is_chat_admin = False
            if not is_chat_admin:
                await safe_send(message.bot.send_message, chat_id=message.chat.id,
                    text="Эту команду могут выполнять только администраторы.", **kw)
                return

    if mtype == "text":
        text_body = (media.get("text") or "")[:4000]
        parse_mode = media.get("parse_mode")
        if parse_mode is None:
            sent = await send_rich_message(
                message.bot,
                chat_id=message.chat.id,
                text=text_body,
                message_thread_id=reply_kwargs.get("message_thread_id"),
                reply_parameters={"message_id": message.message_id, "allow_sending_without_reply": True},
            )
            if sent is not None:
                return
        await safe_send(message.bot.send_message, chat_id=message.chat.id, text=text_body,
                        parse_mode=parse_mode, **kw)
        return

    if mtype == "inline_buttons":
        raw_text = (media.get("text") or "Выбери:")[:4000]
        text_body = format_telegram_html(raw_text)[:4000]
        rows = media.get("buttons", [])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn.get("text", "?")[:64], url=btn.get("url", ""))
             for btn in row if btn.get("url")]
            for row in rows if row
        ])
        await safe_send(message.bot.send_message, chat_id=message.chat.id,
                        text=text_body, reply_markup=keyboard, parse_mode='HTML', **kw)
        return

    if mtype == "tg_poll":
        from aiogram.types import InputPollOption
        opts = [InputPollOption(text=o[:100]) for o in (media.get("options") or [])[:10]]
        await safe_send(message.bot.send_poll, chat_id=message.chat.id,
            question=(media.get("question") or "?")[:300], options=opts,
            is_anonymous=media.get("is_anonymous", True),
            allows_multiple_answers=media.get("allows_multiple_answers", False), **kw)
        return

    if mtype == "tg_location":
        lat, lon = media.get("latitude"), media.get("longitude")
        if lat is not None and lon is not None:
            if media.get("title"):
                await safe_send(message.bot.send_venue, chat_id=message.chat.id,
                    latitude=float(lat), longitude=float(lon),
                    title=media.get("title","")[:64], address=media.get("address","")[:256], **kw)
            else:
                await safe_send(message.bot.send_location, chat_id=message.chat.id,
                    latitude=float(lat), longitude=float(lon), **kw)
        return

    if mtype == "tg_react":
        from aiogram.types import ReactionTypeEmoji
        msg_id = media.get("message_id") or message.message_id
        try:
            await message.bot.set_message_reaction(chat_id=message.chat.id,
                message_id=msg_id, reaction=[ReactionTypeEmoji(emoji=media.get("emoji","👍"))])
        except Exception as _e:
            logger.warning(f"tg_react failed: {_e}")
        return

    if mtype == "tg_pin":
        try:
            msg_id = media.get("message_id") or message.message_id
            await message.bot.pin_chat_message(chat_id=message.chat.id,
                message_id=msg_id, disable_notification=media.get("disable_notification", False))
        except Exception as _e:
            logger.warning(f"tg_pin failed: {_e}")
        return

    if mtype == "tg_delete":
        try:
            await message.bot.delete_message(chat_id=message.chat.id,
                message_id=media.get("message_id"))
        except Exception as _e:
            logger.warning(f"tg_delete failed: {_e}")
        return

    if mtype == "tg_forward":
        from_cid = int(media.get("from_chat_id") or message.chat.id)
        if from_cid != message.chat.id:
            logger.warning(f"tg_forward blocked: cross-chat source {from_cid}")
            return
        try:
            await message.bot.forward_message(chat_id=message.chat.id,
                from_chat_id=message.chat.id,
                message_id=media.get("message_id"))
        except Exception as _e:
            logger.warning(f"tg_forward failed: {_e}")
        return

    if mtype == "tg_chat_action":
        try:
            await message.bot.send_chat_action(chat_id=message.chat.id,
                action=media.get("action", "typing"))
        except Exception as _e:
            logger.warning(f"tg_chat_action failed: {_e}")
        return

    if mtype == "tg_unpin":
        try:
            if media.get("message_id"):
                await message.bot.unpin_chat_message(chat_id=message.chat.id,
                    message_id=media.get("message_id"))
            else:
                await message.bot.unpin_all_chat_messages(chat_id=message.chat.id)
        except Exception as _e:
            logger.warning(f"tg_unpin failed: {_e}")
        return

    if mtype == "tg_invite_link":
        try:
            link = await message.bot.create_chat_invite_link(chat_id=message.chat.id,
                name=media.get("name"), expire_date=media.get("expire_date"),
                member_limit=media.get("member_limit"))
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=f"Пригласительная ссылка: {link.invite_link}", **kw)
        except Exception as _e:
            logger.warning(f"tg_invite_link failed: {_e}")
        return

    if mtype == "tg_set_bot_photo":
        try:
            photo_buf = BufferedInputFile(media.get("data", b""),
                                          filename=media.get("filename", "photo.jpg"))
            await message.bot.set_my_profile_photo(photo=photo_buf)
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text="Аватарка бота обновлена.", **kw)
        except Exception as _e:
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text="Не удалось сменить аватар бота.", **kw)
        return

    if mtype == "tg_set_chat_description":
        try:
            await message.bot.set_chat_description(chat_id=message.chat.id,
                description=media.get("description","")[:255])
        except Exception as _e:
            logger.warning(f"tg_set_chat_description failed: {_e}")
        return

    if mtype == "tg_set_chat_title":
        try:
            await message.bot.set_chat_title(chat_id=message.chat.id,
                title=media.get("title","")[:255])
        except Exception as _e:
            logger.warning(f"tg_set_chat_title failed: {_e}")
        return

    if mtype == "tg_copy_message":
        try:
            await message.bot.copy_message(chat_id=message.chat.id,
                from_chat_id=message.chat.id,
                message_id=media.get("message_id"), caption=media.get("caption"))
        except Exception as _e:
            logger.warning(f"tg_copy_message failed: {_e}")
        return

    if mtype == "tg_send_animation":
        try:
            await safe_send(message.bot.send_animation, chat_id=message.chat.id,
                animation=media.get("url",""), caption=(media.get("caption",""))[:1024], **kw)
        except Exception as _e:
            logger.warning(f"tg_send_animation failed: {_e}")
        return

    if mtype == "tg_send_video_note":
        try:
            await safe_send(message.bot.send_video_note, chat_id=message.chat.id,
                video_note=media.get("file_id",""), **kw)
        except Exception as _e:
            logger.warning(f"tg_send_video_note failed: {_e}")
        return

    if mtype == "tg_send_venue":
        try:
            await safe_send(message.bot.send_venue, chat_id=message.chat.id,
                latitude=float(media.get("latitude",0)),
                longitude=float(media.get("longitude",0)),
                title=media.get("title","")[:64],
                address=media.get("address","")[:256], **kw)
        except Exception as _e:
            logger.warning(f"tg_send_venue failed: {_e}")
        return

    if mtype == "tg_create_forum_topic":
        try:
            t = await message.bot.create_forum_topic(chat_id=message.chat.id,
                name=media.get("name","Новый топик")[:128])
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=f"Топик «{t.name}» создан (thread_id: {t.message_thread_id})", **kw)
        except Exception as _e:
            logger.warning(f"tg_create_forum_topic failed: {_e}")
        return

    if mtype == "tg_close_forum_topic":
        try:
            await message.bot.close_forum_topic(chat_id=message.chat.id,
                message_thread_id=media.get("message_thread_id"))
        except Exception as _e:
            logger.warning(f"tg_close_forum_topic failed: {_e}")
        return

    if mtype == "tg_get_sticker_set":
        try:
            ss = await message.bot.get_sticker_set(name=media.get("name",""))
            info = f"📦 <b>{ss.title}</b> (@{ss.name})\nСтикеров: {len(ss.stickers)}"
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=info, parse_mode='HTML', **kw)
        except Exception as _e:
            logger.warning(f"tg_get_sticker_set failed: {_e}")
        return

    if mtype == "tg_approve_join":
        try:
            if media.get("approve", True):
                await message.bot.approve_chat_join_request(chat_id=message.chat.id,
                    user_id=media.get("user_id"))
            else:
                await message.bot.decline_chat_join_request(chat_id=message.chat.id,
                    user_id=media.get("user_id"))
        except Exception as _e:
            logger.warning(f"tg_approve_join failed: {_e}")
        return

    if mtype == "tg_export_link":
        try:
            link = await message.bot.export_chat_invite_link(chat_id=message.chat.id)
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=f"Ссылка: {link}", **kw)
        except Exception as _e:
            logger.warning(f"tg_export_link failed: {_e}")
        return

    if mtype == "tg_set_chat_photo":
        try:
            photo_buf = BufferedInputFile(media.get("data", b""),
                                          filename=media.get("filename", "photo.jpg"))
            await message.bot.set_chat_photo(chat_id=message.chat.id, photo=photo_buf)
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text="Аватарка беседы обновлена.", **kw)
        except Exception as _e:
            logger.warning(f"tg_set_chat_photo failed: {_e}")
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text="Не удалось сменить аватарку.", **kw)
        return

    if mtype == "tg_send_sticker":
        try:
            await safe_send(message.bot.send_sticker, chat_id=message.chat.id,
                sticker=media.get("sticker"), **kw)
        except Exception as _e:
            logger.warning(f"tg_send_sticker failed: {_e}")
        return

    if mtype == "tg_send_contact":
        try:
            await safe_send(message.bot.send_contact, chat_id=message.chat.id,
                phone_number=media.get("phone",""), first_name=media.get("name",""),
                last_name=media.get("last_name",""), **kw)
        except Exception as _e:
            logger.warning(f"tg_send_contact failed: {_e}")
        return

    if mtype == "tg_send_dice":
        try:
            await safe_send(message.bot.send_dice, chat_id=message.chat.id,
                emoji=media.get("emoji","🎲"), **kw)
        except Exception as _e:
            logger.warning(f"tg_send_dice failed: {_e}")
        return

    if mtype == "tg_edit_message":
        try:
            await message.bot.edit_message_text(chat_id=message.chat.id,
                message_id=media.get("message_id"),
                text=format_telegram_html(media.get("text", ""))[:4096], parse_mode='HTML')
        except Exception as _e:
            logger.warning(f"tg_edit_message failed: {_e}")
        return

    # Fallback: binary file send (photo / video / animation / audio / voice / document)
    data = media.get("data", b"")
    raw_caption = media.get("caption") or ""
    formatted_caption = format_telegram_html(raw_caption) if raw_caption else ""
    use_html_caption = bool(formatted_caption and len(formatted_caption) <= 1024)
    caption = formatted_caption if use_html_caption else _clean_plain_reply(raw_caption)[:1024]
    cap_kw = {**kw, "parse_mode": "HTML"} if use_html_caption else kw
    filename = media.get("filename") or "file"

    async def _send_binary(send_fn, media_field: str):
        buf = BufferedInputFile(data, filename=filename)
        try:
            await safe_send(send_fn, chat_id=message.chat.id, caption=caption, **{media_field: buf, **cap_kw})
        except Exception as cap_err:
            if not use_html_caption:
                raise
            logger.warning(f"HTML caption failed for {mtype}, retrying plain: {cap_err}")
            plain_cap = _clean_plain_reply(raw_caption)[:1024]
            buf_retry = BufferedInputFile(data, filename=filename)
            await safe_send(send_fn, chat_id=message.chat.id, caption=plain_cap, **{media_field: buf_retry, **kw})

    if mtype == "photo":
        await _send_binary(message.bot.send_photo, "photo")
    elif mtype == "video":
        await _send_binary(message.bot.send_video, "video")
    elif mtype == "animation":
        await _send_binary(message.bot.send_animation, "animation")
    elif mtype == "audio":
        await _send_binary(message.bot.send_audio, "audio")
    elif mtype == "voice":
        await _send_binary(message.bot.send_voice, "voice")
    else:
        await _send_binary(message.bot.send_document, "document")
