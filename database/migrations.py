"""
Database migration system for NanoHatani.

Migrations are applied in order. Each migration is a dict with:
    version: int          — sequential version number
    description: str      — human-readable description
    sql: str              — SQL to execute (should be idempotent)

The _schema_version table tracks which migrations have been applied.
"""

import logging

logger = logging.getLogger(__name__)

MIGRATIONS = [
    {
        "version": 1,
        "description": "Initial schema — chat history, pending generations, users, limits, logs",
        "sql": """
            CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY);

            CREATE TABLE IF NOT EXISTS chat_history (
                chat_id INTEGER PRIMARY KEY,
                history TEXT
            );

            CREATE TABLE IF NOT EXISTS pending_generations (
                id TEXT PRIMARY KEY,
                gen_type TEXT,
                user_id INTEGER,
                chat_id INTEGER,
                source_message_id INTEGER,
                message_thread_id INTEGER,
                prompt TEXT,
                model TEXT,
                provider TEXT,
                file_ids TEXT,
                veo_operation_name TEXT,
                veo_api_key TEXT,
                model_label TEXT,
                created_at REAL
            );

            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                date_str TEXT,
                gen_type TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date_str, gen_type)
            );

            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS vip_users (
                user_id INTEGER PRIMARY KEY,
                paid_until REAL
            );

            CREATE TABLE IF NOT EXISTS daily_limits_usage (
                chat_id INTEGER,
                user_id INTEGER,
                period TEXT,
                count INTEGER,
                PRIMARY KEY (chat_id, user_id, period)
            );

            CREATE TABLE IF NOT EXISTS prompt_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                gen_type TEXT,
                prompt TEXT,
                created_at REAL
            );

            CREATE TABLE IF NOT EXISTS chat_limits (
                chat_id INTEGER PRIMARY KEY,
                req_limit INTEGER,
                days INTEGER
            );


            CREATE INDEX IF NOT EXISTS idx_user_stats_date ON user_stats(date_str);
            CREATE INDEX IF NOT EXISTS idx_prompt_logs_user ON prompt_logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_prompt_logs_created ON prompt_logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_pending_gens_created ON pending_generations(created_at);
        """,
    },
    {
        "version": 2,
        "description": "ElevenLabs voice cloning — voices table + voice_settings",
        "sql": """
            CREATE TABLE IF NOT EXISTS voices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                voice_id TEXT NOT NULL,
                tier TEXT NOT NULL,
                audio_duration_sec REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS voice_settings (
                user_id INTEGER PRIMARY KEY,
                tts_model TEXT DEFAULT 'eleven_v3',
                stability REAL DEFAULT 0.5,
                similarity_boost REAL DEFAULT 0.75,
                style REAL DEFAULT 0.0,
                speed REAL DEFAULT 1.0
            );
            CREATE INDEX IF NOT EXISTS idx_voices_user ON voices(user_id);
        """,
    },
    {
        "version": 3,
        "description": "Remove unused agent tasks table",
        "sql": "DROP TABLE IF EXISTS agent_tasks;",
    },
]


async def apply_migrations(db) -> int:
    """Apply pending migrations. Returns the number of new migrations applied."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY)"
    )
    cursor = await db.execute("SELECT COALESCE(MAX(version), 0) FROM _schema_version")
    row = await cursor.fetchone()
    current_version = row[0] if row else 0

    applied = 0
    for m in MIGRATIONS:
        if m["version"] <= current_version:
            continue
        logger.info(f"Applying migration {m['version']}: {m['description']}")
        await db.executescript(m["sql"])
        await db.execute(
            "INSERT OR REPLACE INTO _schema_version (version) VALUES (?)",
            (m["version"],),
        )
        await db.commit()
        applied += 1
        logger.info(f"Migration {m['version']} applied successfully")

    if applied:
        logger.info(f"Applied {applied} migration(s). Current version: {MIGRATIONS[-1]['version']}")
    return applied
