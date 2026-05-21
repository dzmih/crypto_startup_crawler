"""
db.py — Database abstraction layer for Crypto Startup Radar.
Auto-selects backend based on DATABASE_URL environment variable:
  - Not set / empty → SQLite (default, single machine)
  - postgresql://...  → asyncpg Postgres (multi-machine)

Usage:
    import db
    await db.init()
    await db.save_profile(username, scraped, depth, ai_res)
    profile = await db.get_profile(username)
"""
import asyncio
import json
import logging
import os
import sqlite3
import traceback
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ─────────────────────────────────────────────────────────────────────────────
# Определяем бэкенд
# ─────────────────────────────────────────────────────────────────────────────

_BACKEND = "postgres" if DATABASE_URL.startswith("postgresql") else "sqlite"
logging.info(f"[db] Бэкенд: {_BACKEND.upper()}")

# ─────────────────────────────────────────────────────────────────────────────
# SQLite бэкенд
# ─────────────────────────────────────────────────────────────────────────────

_DB_NAME  = os.environ.get("STARTUPS_DB", "startups.db")
_db_lock  = asyncio.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS startups (
    username         TEXT PRIMARY KEY,
    display_username TEXT,
    url              TEXT,
    bio              TEXT,
    depth            INTEGER,
    is_valuable      INTEGER,
    category         TEXT,
    stage            TEXT,
    pitch            TEXT,
    red_flags        TEXT,
    pre_filtered     INTEGER DEFAULT 0,
    filter_reason    TEXT,
    parsed_at        TEXT,
    ai_due           INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tweets (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    tweet    TEXT,
    FOREIGN KEY(username) REFERENCES startups(username) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS mentions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    mention  TEXT,
    FOREIGN KEY(username) REFERENCES startups(username) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_is_valuable ON startups(is_valuable);
CREATE INDEX IF NOT EXISTS idx_username    ON startups(username);
CREATE INDEX IF NOT EXISTS idx_ai_due      ON startups(ai_due);
"""

def _sqlite_init():
    with sqlite3.connect(_DB_NAME) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
        # Миграция старых таблиц — добавляем ai_due если нет
        try:
            conn.execute("ALTER TABLE startups ADD COLUMN ai_due INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        conn.commit()

def _sqlite_get(username: str) -> dict | None:
    canon = username.lower()
    with sqlite3.connect(_DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM startups WHERE username = ?", (canon,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT tweet FROM tweets WHERE username = ?", (canon,))
        tweets = [r["tweet"] for r in cur.fetchall()]
        cur.execute("SELECT mention FROM mentions WHERE username = ?", (canon,))
        mentions = [r["mention"] for r in cur.fetchall()]
        return _row_to_dict(row, tweets, mentions)

def _row_to_dict(row, tweets: list, mentions: list) -> dict:
    ai = None
    if row["is_valuable"] is not None or row["pre_filtered"]:
        ai = {
            "is_valuable": bool(row["is_valuable"]),
            "category": row["category"],
            "stage": row["stage"],
            "pitch": row["pitch"],
            "red_flags": row["red_flags"],
        }
    return {
        "scraped": {
            "username": row["username"],
            "display_username": row["display_username"],
            "url": row["url"],
            "bio": row["bio"],
            "tweets": tweets,
            "mentions": mentions,
        },
        "ai": ai,
        "depth": row["depth"],
        "pre_filtered": bool(row["pre_filtered"]),
        "filter_reason": row["filter_reason"],
        "parsed_at": row["parsed_at"],
        "ai_due": bool(row["ai_due"]),
    }

def _sqlite_save(username: str, scraped: dict, depth: int,
                 ai_res: dict | None, pre_filtered: bool,
                 filter_reason: str | None, ai_due: bool):
    canon    = username.lower()
    display  = scraped.get("display_username") or username
    url      = scraped.get("url", f"https://x.com/{username}")
    bio      = scraped.get("bio", "")
    tweets   = scraped.get("tweets", [])
    mentions = scraped.get("mentions", [])
    parsed_at = datetime.now().isoformat()

    is_valuable = category = stage = pitch = red_flags = None
    if ai_res:
        is_valuable = 1 if ai_res.get("is_valuable") else 0
        category    = ai_res.get("category") or "Other"
        stage       = ai_res.get("stage") or "unknown"
        pitch       = ai_res.get("pitch") or ""
        red_flags   = ai_res.get("red_flags") or ""
        ai_due      = False
    elif pre_filtered:
        is_valuable = 0
        category    = "Filtered"
        stage       = "unknown"
        pitch       = ""
        red_flags   = f"Pre-filtered: {filter_reason}"
        ai_due      = False

    with sqlite3.connect(_DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO startups
              (username,display_username,url,bio,depth,is_valuable,category,stage,
               pitch,red_flags,pre_filtered,filter_reason,parsed_at,ai_due)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (canon, display, url, bio, depth, is_valuable, category, stage,
              pitch, red_flags, int(pre_filtered), filter_reason, parsed_at, int(ai_due)))
        cur.execute("DELETE FROM tweets WHERE username = ?", (canon,))
        cur.execute("DELETE FROM mentions WHERE username = ?", (canon,))
        cur.executemany("INSERT INTO tweets (username,tweet) VALUES (?,?)",
                        [(canon, t) for t in tweets])
        cur.executemany("INSERT INTO mentions (username,mention) VALUES (?,?)",
                        [(canon, m) for m in mentions])
        conn.commit()

def _sqlite_load_valuable() -> list[dict]:
    with sqlite3.connect(_DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM startups WHERE is_valuable = 1")
        rows = cur.fetchall()
        result = []
        for row in rows:
            u = row["username"]
            cur.execute("SELECT tweet FROM tweets WHERE username = ?", (u,))
            tweets = [r["tweet"] for r in cur.fetchall()]
            cur.execute("SELECT mention FROM mentions WHERE username = ?", (u,))
            mentions = [r["mention"] for r in cur.fetchall()]
            result.append(_row_to_dict(row, tweets, mentions))
        return result

def _sqlite_count_ai_due() -> int:
    with sqlite3.connect(_DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM startups WHERE ai_due = 1")
        return cur.fetchone()[0]

# ─────────────────────────────────────────────────────────────────────────────
# Postgres бэкенд (asyncpg)
# ─────────────────────────────────────────────────────────────────────────────

_pg_pool = None

# DDL для Postgres (синтаксис отличается от SQLite)
_PG_DDL = """
CREATE TABLE IF NOT EXISTS startups (
    username         TEXT PRIMARY KEY,
    display_username TEXT,
    url              TEXT,
    bio              TEXT,
    depth            INTEGER,
    is_valuable      INTEGER,
    category         TEXT,
    stage            TEXT,
    pitch            TEXT,
    red_flags        TEXT,
    pre_filtered     INTEGER DEFAULT 0,
    filter_reason    TEXT,
    parsed_at        TEXT,
    ai_due           INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tweets (
    id       SERIAL PRIMARY KEY,
    username TEXT REFERENCES startups(username) ON DELETE CASCADE,
    tweet    TEXT
);
CREATE TABLE IF NOT EXISTS mentions (
    id       SERIAL PRIMARY KEY,
    username TEXT REFERENCES startups(username) ON DELETE CASCADE,
    mention  TEXT
);
CREATE INDEX IF NOT EXISTS idx_is_valuable ON startups(is_valuable);
CREATE INDEX IF NOT EXISTS idx_ai_due      ON startups(ai_due);
"""

async def _pg_get_pool():
    global _pg_pool
    if _pg_pool is None:
        try:
            import asyncpg
            _pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
            logging.info("[db] Postgres pool создан")
        except ImportError:
            raise RuntimeError("asyncpg не установлен: pip install asyncpg")
        except Exception as e:
            raise RuntimeError(f"Ошибка подключения к Postgres: {e}")
    return _pg_pool

async def _pg_init():
    pool = await _pg_get_pool()
    async with pool.acquire() as conn:
        for stmt in _PG_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await conn.execute(stmt)

async def _pg_get(username: str) -> dict | None:
    pool = await _pg_get_pool()
    canon = username.lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM startups WHERE username = $1", canon)
        if not row:
            return None
        tweets   = [r["tweet"]   for r in await conn.fetch(
            "SELECT tweet   FROM tweets   WHERE username = $1", canon)]
        mentions = [r["mention"] for r in await conn.fetch(
            "SELECT mention FROM mentions WHERE username = $1", canon)]
        return _row_to_dict(row, tweets, mentions)

async def _pg_save(username: str, scraped: dict, depth: int,
                   ai_res: dict | None, pre_filtered: bool,
                   filter_reason: str | None, ai_due: bool):
    pool     = await _pg_get_pool()
    canon    = username.lower()
    display  = scraped.get("display_username") or username
    url      = scraped.get("url", f"https://x.com/{username}")
    bio      = scraped.get("bio", "")
    tweets   = scraped.get("tweets", [])
    mentions = scraped.get("mentions", [])
    parsed_at = datetime.now().isoformat()

    is_valuable = category = stage = pitch = red_flags = None
    if ai_res:
        is_valuable = 1 if ai_res.get("is_valuable") else 0
        category    = ai_res.get("category") or "Other"
        stage       = ai_res.get("stage") or "unknown"
        pitch       = ai_res.get("pitch") or ""
        red_flags   = ai_res.get("red_flags") or ""
        ai_due      = False
    elif pre_filtered:
        is_valuable = 0
        category    = "Filtered"
        stage       = "unknown"
        pitch       = ""
        red_flags   = f"Pre-filtered: {filter_reason}"
        ai_due      = False

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO startups
                  (username,display_username,url,bio,depth,is_valuable,category,stage,
                   pitch,red_flags,pre_filtered,filter_reason,parsed_at,ai_due)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (username) DO UPDATE SET
                  display_username=EXCLUDED.display_username, url=EXCLUDED.url,
                  bio=EXCLUDED.bio, depth=EXCLUDED.depth, is_valuable=EXCLUDED.is_valuable,
                  category=EXCLUDED.category, stage=EXCLUDED.stage, pitch=EXCLUDED.pitch,
                  red_flags=EXCLUDED.red_flags, pre_filtered=EXCLUDED.pre_filtered,
                  filter_reason=EXCLUDED.filter_reason, parsed_at=EXCLUDED.parsed_at,
                  ai_due=EXCLUDED.ai_due
            """, canon, display, url, bio, depth, is_valuable, category, stage,
                 pitch, red_flags, int(pre_filtered), filter_reason, parsed_at, int(ai_due))
            await conn.execute("DELETE FROM tweets   WHERE username = $1", canon)
            await conn.execute("DELETE FROM mentions WHERE username = $1", canon)
            await conn.executemany(
                "INSERT INTO tweets (username,tweet) VALUES ($1,$2)",
                [(canon, t) for t in tweets])
            await conn.executemany(
                "INSERT INTO mentions (username,mention) VALUES ($1,$2)",
                [(canon, m) for m in mentions])

async def _pg_load_valuable() -> list[dict]:
    pool = await _pg_get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM startups WHERE is_valuable = 1")
        result = []
        for row in rows:
            u = row["username"]
            tweets   = [r["tweet"]   for r in await conn.fetch(
                "SELECT tweet   FROM tweets   WHERE username = $1", u)]
            mentions = [r["mention"] for r in await conn.fetch(
                "SELECT mention FROM mentions WHERE username = $1", u)]
            result.append(_row_to_dict(row, tweets, mentions))
        return result

async def _pg_count_ai_due() -> int:
    pool = await _pg_get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM startups WHERE ai_due = 1")

# ─────────────────────────────────────────────────────────────────────────────
# Публичный API — одинаковый для обоих бэкендов
# ─────────────────────────────────────────────────────────────────────────────

_lock = asyncio.Lock()   # для SQLite бэкенда (Postgres сам конкурентный)

async def init():
    """Инициализирует схему БД. Вызывается один раз при старте."""
    if _BACKEND == "postgres":
        await _pg_init()
    else:
        async with _lock:
            await asyncio.to_thread(_sqlite_init)

async def get_profile(username: str) -> dict | None:
    if _BACKEND == "postgres":
        return await _pg_get(username)
    async with _lock:
        return await asyncio.to_thread(_sqlite_get, username)

async def save_profile(username: str, scraped: dict, depth: int,
                       ai_res: dict | None = None,
                       pre_filtered: bool = False,
                       filter_reason: str | None = None,
                       ai_due: bool = False):
    if _BACKEND == "postgres":
        await _pg_save(username, scraped, depth, ai_res, pre_filtered, filter_reason, ai_due)
    else:
        async with _lock:
            await asyncio.to_thread(_sqlite_save, username, scraped, depth,
                                    ai_res, pre_filtered, filter_reason, ai_due)

async def load_valuable() -> list[dict]:
    if _BACKEND == "postgres":
        return await _pg_load_valuable()
    async with _lock:
        return await asyncio.to_thread(_sqlite_load_valuable)

async def count_ai_due() -> int:
    if _BACKEND == "postgres":
        return await _pg_count_ai_due()
    async with _lock:
        return await asyncio.to_thread(_sqlite_count_ai_due)

async def close():
    """Закрывает пул соединений (нужно для Postgres)."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None
        logging.info("[db] Postgres pool закрыт")
