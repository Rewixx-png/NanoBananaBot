"""Loop safety hooks — debounce and per-tool rate limiting."""

import hashlib
from collections import deque
from typing import Optional


class _DebounceHook:
    """Prevents the agent from repeating identical tool calls in a loop."""

    def __init__(self, window: int = 6, max_repeats: int = 2):
        self._win: deque[str] = deque(maxlen=window)
        self._max = max_repeats

    def check(self, name: str, args: dict) -> Optional[str]:
        fp = hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()
        if sum(1 for f in self._win if f == fp) >= self._max:
            return (
                f"LOOP: '{name}' called with same args {self._max}+ times. "
                "Change your approach."
            )
        self._win.append(fp)
        return None


class _ToolBudget:
    """Per-tool rate limiting — prevents tool spam within a single agent session."""

    LIMITS = {
        "web_search": 16, "scrape_url": 20, "generate_project": 4,
        "think": 30, "reply": 6, "generate_image": 6,
        "search_and_send_image": 6, "download_image": 10,
        "search_and_send_video": 4, "download_video": 4, "text_to_speech": 6,
        "run_python": 12, "run_shell": 16,
        "write_file": 20, "read_file": 20,
        "fetch_json": 16, "calculate": 40,
        "qr_code": 6, "create_chart": 6,
        "translate": 10, "create_file": 10, "send_workspace_file": 10, "send_with_buttons": 5,
        "tg_send_poll": 3, "tg_send_location": 5, "tg_react": 10, "tg_pin_message": 3,
        "tg_delete_message": 10, "tg_forward_message": 5, "tg_get_chat_info": 5,
        "tg_ban_user": 3, "tg_kick_user": 3, "tg_send_chat_action": 10,
        "tg_restrict_member": 3, "tg_unpin_message": 5, "tg_create_invite_link": 3,
        "tg_set_chat_title": 2, "tg_copy_message": 10, "tg_send_sticker": 5,
        "tg_send_contact": 5, "tg_send_dice": 5, "tg_edit_message": 10,
        "fetch_tiktok_profile": 5, "fetch_with_cookies": 8,
        "tg_set_chat_photo": 2, "tg_send_animation": 5, "tg_send_video_note": 3, "tg_send_venue": 5,
        "tg_promote_member": 2, "tg_get_chat_member": 10, "tg_get_admins": 5,
        "tg_get_member_count": 10, "tg_create_forum_topic": 3, "tg_close_forum_topic": 3,
        "tg_get_sticker_set": 5, "tg_approve_join_request": 5, "tg_export_invite_link": 3,
        "read_bot_logs": 5, "list_image_models": 5,
        "analyze_audio": 5, "analyze_image": 10, "playwright_browse": 5,
        "tg_unban_user": 5, "tg_set_bot_photo": 2, "tg_set_chat_description": 3,
    }

    def __init__(self):
        self._counts: dict[str, int] = {}

    def charge(self, name: str) -> Optional[str]:
        limit = self.LIMITS.get(name, 20)
        count = self._counts.get(name, 0) + 1
        self._counts[name] = count
        if count > limit:
            return f"BUDGET: '{name}' exceeded limit {limit}."
        return None
