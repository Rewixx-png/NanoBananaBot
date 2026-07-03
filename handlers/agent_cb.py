"""Extracted _agent_send_cb callback for agent tool dispatch.

This module handles every mtype the agent can emit: text, inline buttons, polls,
admin actions, media, stickers, dice, etc.  It was pulled out of handle_text_messages
in chat.py to keep that file manageable.
"""

import logging
from aiogram import types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from handlers.common import safe_send
from config import ADMIN_IDS

logger = logging.getLogger(__name__)


async def send_agent_callback(media: dict, /, *, message: types.Message, reply_kwargs: dict) -> None:
    """Dispatch a single agent `media` payload to the appropriate Telegram action.

    Parameters
    ----------
    media : dict
        Agent-side payload with at least a ``"type"`` key.
    message : types.Message
        The original incoming Telegram message (used for chat-id, bot, reply-to, etc.).
    reply_kwargs : dict
        Extra kwargs forwarded to every send (e.g. ``message_thread_id`` for forums).
    """
    mtype = media.get("type", "document")
    kw = {"reply_to_message_id": message.message_id, **reply_kwargs}

    if mtype == "text":
        text_body = (media.get("text") or "")[:4000]
        parse_mode = media.get("parse_mode")
        await safe_send(message.bot.send_message, chat_id=message.chat.id, text=text_body,
                        parse_mode=parse_mode, **kw)
        return

    if mtype == "inline_buttons":
        import bleach as _bleach
        _TG_TAGS = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
                    'code', 'pre', 'blockquote', 'tg-spoiler', 'tg-emoji']
        raw_text = (media.get("text") or "Выбери:")[:4000]
        text_body = _bleach.clean(raw_text, tags=_TG_TAGS,
            attributes={'pre': [], 'code': ['class'], 'tg-emoji': ['emoji-id'],
                        'blockquote': ['expandable']}, strip=True)
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

    if mtype == "tg_get_chat_info":
        try:
            chat = await message.bot.get_chat(message.chat.id)
            count = await message.bot.get_chat_member_count(message.chat.id)
            info = (f"<b>Чат:</b> {chat.title or 'N/A'}\n"
                    f"<b>ID:</b> <code>{chat.id}</code>\n"
                    f"<b>Тип:</b> {chat.type}\n"
                    f"<b>Участников:</b> {count}\n"
                    f"<b>Описание:</b> {chat.description or '—'}")
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=info, parse_mode='HTML', **kw)
        except Exception as _e:
            logger.warning(f"tg_get_chat_info failed: {_e}")
        return

    _ADMIN_MTYPES = {"tg_ban", "tg_unban", "tg_kick", "tg_restrict", "tg_pin", "tg_unpin",
                     "tg_set_chat_title", "tg_invite_link", "tg_promote"}
    if mtype in _ADMIN_MTYPES and message.chat.type != "private":
        try:
            _req = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
            if _req.status not in ("administrator", "creator"):
                await safe_send(message.bot.send_message, chat_id=message.chat.id,
                    text="❌ Эту команду могут выполнять только администраторы.", **kw)
                return
        except Exception:
            pass

    if mtype == "tg_ban":
        try:
            import datetime
            until = media.get("until_date")
            until_dt = datetime.datetime.fromtimestamp(until, tz=datetime.timezone.utc) if until else None
            await message.bot.ban_chat_member(chat_id=message.chat.id,
                user_id=media.get("user_id"), until_date=until_dt)
        except Exception as _e:
            logger.warning(f"tg_ban failed: {_e}")
        return

    if mtype == "tg_unban":
        try:
            await message.bot.unban_chat_member(chat_id=message.chat.id,
                user_id=media.get("user_id"), only_if_banned=True)
        except Exception as _e:
            logger.warning(f"tg_unban failed: {_e}")
        return

    if mtype == "tg_kick":
        try:
            import time as _t
            await message.bot.ban_chat_member(chat_id=message.chat.id,
                user_id=media.get("user_id"),
                until_date=int(_t.time()) + 35)
            await message.bot.unban_chat_member(chat_id=message.chat.id,
                user_id=media.get("user_id"), only_if_banned=True)
        except Exception as _e:
            logger.warning(f"tg_kick failed: {_e}")
        return

    if mtype == "tg_chat_action":
        try:
            await message.bot.send_chat_action(chat_id=message.chat.id,
                action=media.get("action", "typing"))
        except Exception as _e:
            logger.warning(f"tg_chat_action failed: {_e}")
        return

    if mtype == "tg_restrict":
        from aiogram.types import ChatPermissions
        try:
            until = media.get("until_date")
            import datetime
            until_dt = datetime.datetime.fromtimestamp(until, tz=datetime.timezone.utc) if until else None
            perms = ChatPermissions(can_send_messages=media.get("can_send_messages", True),
                can_send_media_messages=media.get("can_send_media", True))
            await message.bot.restrict_chat_member(chat_id=message.chat.id,
                user_id=media.get("user_id"), permissions=perms, until_date=until_dt)
        except Exception as _e:
            logger.warning(f"tg_restrict failed: {_e}")
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
                text=f"🔗 Пригласительная ссылка: {link.invite_link}", **kw)
        except Exception as _e:
            logger.warning(f"tg_invite_link failed: {_e}")
        return

    if mtype == "tg_set_bot_photo":
        try:
            photo_buf = BufferedInputFile(media.get("data", b""),
                                          filename=media.get("filename", "photo.jpg"))
            await message.bot.set_my_profile_photo(photo=photo_buf)
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text="✅ Аватарка бота обновлена!", **kw)
        except Exception as _e:
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text=f"❌ Не смог сменить аву бота: {_e}", **kw)
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

    if mtype == "tg_promote":
        try:
            await message.bot.promote_chat_member(
                chat_id=message.chat.id, user_id=media.get("user_id"),
                can_delete_messages=media.get("can_delete_messages", False),
                can_pin_messages=media.get("can_pin_messages", False),
                can_manage_chat=media.get("can_manage_chat", False),
                can_ban_members=media.get("can_ban_members", False))
            if media.get("custom_title"):
                await message.bot.set_chat_administrator_custom_title(
                    chat_id=message.chat.id, user_id=media.get("user_id"),
                    custom_title=media.get("custom_title","")[:16])
        except Exception as _e:
            logger.warning(f"tg_promote failed: {_e}")
        return

    if mtype == "tg_get_member":
        try:
            m = await message.bot.get_chat_member(message.chat.id, media.get("user_id"))
            u = m.user
            info = (f"<b>Пользователь:</b> {u.full_name}\n"
                    f"<b>ID:</b> <code>{u.id}</code>\n"
                    f"<b>Username:</b> @{u.username or '—'}\n"
                    f"<b>Статус:</b> {m.status}")
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=info, parse_mode='HTML', **kw)
        except Exception as _e:
            logger.warning(f"tg_get_member failed: {_e}")
        return

    if mtype == "tg_get_admins":
        try:
            admins = await message.bot.get_chat_administrators(message.chat.id)
            lines = [f"👑 <b>Администраторы чата</b> ({len(admins)}):"]
            for a in admins:
                title = getattr(a, 'custom_title', None) or a.status
                lines.append(f"• {a.user.full_name} (@{a.user.username or '—'}) — {title}")
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text='\n'.join(lines), parse_mode='HTML', **kw)
        except Exception as _e:
            logger.warning(f"tg_get_admins failed: {_e}")
        return

    if mtype == "tg_get_member_count":
        try:
            count = await message.bot.get_chat_member_count(message.chat.id)
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=f"👥 Участников в чате: <b>{count}</b>", parse_mode='HTML', **kw)
        except Exception as _e:
            logger.warning(f"tg_get_member_count failed: {_e}")
        return

    if mtype == "tg_create_forum_topic":
        try:
            t = await message.bot.create_forum_topic(chat_id=message.chat.id,
                name=media.get("name","Новый топик")[:128])
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=f"✅ Топик «{t.name}» создан (thread_id: {t.message_thread_id})", **kw)
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
                text=f"🔗 Ссылка: {link}", **kw)
        except Exception as _e:
            logger.warning(f"tg_export_link failed: {_e}")
        return

    if mtype == "tg_set_chat_photo":
        try:
            from aiogram.types import BufferedInputFile as _BIF
            photo_buf = _BIF(media.get("data", b""),
                             filename=media.get("filename", "photo.jpg"))
            await message.bot.set_chat_photo(chat_id=message.chat.id, photo=photo_buf)
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text="✅ Аватарка беседы обновлена!", **kw)
        except Exception as _e:
            logger.warning(f"tg_set_chat_photo failed: {_e}")
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                            text=f"❌ Не смог сменить аватарку: {_e}", **kw)
        return

    if mtype == "tg_read_logs":
        try:
            import subprocess
            log_path = '/root/Projects/NanoHatani/bot.log'
            n = min(int(media.get("lines", 50)), 200)
            filt = media.get("filter", "")
            if filt:
                out = subprocess.run(['grep', '-i', filt, log_path],
                    capture_output=True, text=True).stdout
                lines = out.strip().splitlines()[-n:]
            else:
                with open(log_path) as f:
                    lines = f.readlines()
                lines = [l.rstrip() for l in lines[-n:]]
            text = '\n'.join(lines) or '(лог пустой)'
            await safe_send(message.bot.send_message, chat_id=message.chat.id,
                text=f"<pre>{text[:3800]}</pre>", parse_mode='HTML', **kw)
        except Exception as _e:
            logger.warning(f"read_logs failed: {_e}")
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
                text=media.get("text","")[:4096], parse_mode='HTML')
        except Exception as _e:
            logger.warning(f"tg_edit_message failed: {_e}")
        return

    # Fallback: binary file send (photo / video / audio / document)
    data = media.get("data", b"")
    caption = (media.get("caption") or "")[:1024]
    filename = media.get("filename") or "file"
    buf = BufferedInputFile(data, filename=filename)
    if mtype == "photo":
        await safe_send(message.bot.send_photo, chat_id=message.chat.id, photo=buf, caption=caption, **kw)
    elif mtype == "video":
        await safe_send(message.bot.send_video, chat_id=message.chat.id, video=buf, caption=caption, **kw)
    elif mtype == "audio":
        await safe_send(message.bot.send_audio, chat_id=message.chat.id, audio=buf, caption=caption, **kw)
    else:
        await safe_send(message.bot.send_document, chat_id=message.chat.id, document=buf, caption=caption, **kw)
