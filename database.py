import aiosqlite
import json

# ==========================================
# База Данных (история чатов)
# ==========================================
async def init_db():
    async with aiosqlite.connect("bot_data.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS chat_history (chat_id INTEGER PRIMARY KEY, history TEXT)")
        await db.commit()

async def get_history(chat_id):
    async with aiosqlite.connect("bot_data.db") as db:
        async with db.execute("SELECT history FROM chat_history WHERE chat_id = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
            return []

async def save_history(chat_id, history):
    async with aiosqlite.connect("bot_data.db") as db:
        await db.execute("INSERT OR REPLACE INTO chat_history (chat_id, history) VALUES (?, ?)", (chat_id, json.dumps(history)))
        await db.commit()
