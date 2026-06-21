import time
from state import chat_custom_limits, daily_gen_limits
from config import DAILY_GEN_LIMIT

def _check_daily_limit(user_id: int, chat_id: int) -> tuple:
    (req_limit, days) = chat_custom_limits.get(chat_id, (DAILY_GEN_LIMIT, 1))
    period_id = str(int(time.time() // (86400 * days))) if days > 0 else str(time.time())
    entry = daily_gen_limits.get((chat_id, user_id), {})
    if entry.get('period') != period_id:
        entry = {'period': period_id, 'count': 0}
    count = entry.get('count', 0)
    if count >= req_limit:
        return (False, 0)
    entry['count'] = count + 1
    daily_gen_limits[chat_id, user_id] = entry
    return (True, req_limit - entry['count'])
