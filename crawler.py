"""
crawler.py — Crypto Startup Radar v3.3
Features: normalized SQLite/Postgres, TTL cache, ai_due flag, smart priority queue,
UA rotation, viewport jitter, mouse emulation, dynamic scroll rounds,
exponential backoff, metrics, circuit-breaker, rate limiter.
Standalone mode (py crawler.py) or Redis-queue mode (py worker.py).
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')

import asyncio
import json
import os
import re
import random
import logging
import traceback
import time
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from dataclasses import dataclass, field

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import ollama
from enrichment import enrich_project

# ─────────────────────────────────────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logging.warning("Invalid %s=%r, using %r", name, raw, default)
        return default

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logging.warning("Invalid %s=%r, using %r", name, raw, default)
        return default

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

SEED_ACCOUNTS = [
    # VC
    "Paradigm", "a16zcrypto", "Dragonfly_xyz", "multicoincap",
    "PanteraCapital", "polychain", "HaunVentures", "SpartanGroup_",
    "hashed_official", "BainCapCrypto", "electriccapital", "1confirmation",
    "GalaxyDigital", "animocabrands", "delphi_digital", "DCGco",
    "RibbonFinance", "banklessvc", "cbventures",
    # Founders
    "VitalikButerin", "balajis", "naval", "jessepollak", "gakonst",
    "stani", "rleshner", "haydenzadams",
]

SKIP_ACCOUNTS = {
    "binance", "coinbase", "kraken", "okx", "bybit", "kucoin", "gemini",
    "bitfinex", "coinbasewallet", "ethereum", "bitcoin", "solana", "bnbchain",
    "polkadot", "avalancheavax", "coindesk", "cointelegraph", "theblock__",
    "decrypt_co", "wublockchain", "senlummis", "senthomtillis",
    "senatortimscott", "realdonaldtrump", "jdvance", "keirstarmer",
    "google", "microsoft", "apple", "twitter", "x", "elonmusk", "coupang",
    "anthropicai", "joerogan", "saylor", "michael_saylor", "cobie",
    "unitedwayabc", "boysclubworld", "crypto_council", "remyblarenews",
    "consensus2026", "cbventures",
}
SKIP_ACCOUNTS.update([a.lower() for a in SEED_ACCOUNTS])

MAX_DEPTH1          = _env_int("CSR_MAX_DEPTH1", 50)
MAX_DEPTH2          = _env_int("CSR_MAX_DEPTH2", 60)
SCROLL_ROUNDS       = _env_int("CSR_SCROLL_ROUNDS", 5)       # прокруток для обычного профиля
SCROLL_ROUNDS_MIN   = _env_int("CSR_SCROLL_ROUNDS_MIN", 1)   # минимум для низкоприоритетных
MAX_CONCURRENT      = int(os.environ.get("CSR_CONCURRENT", "3"))
HEADLESS            = _env_bool("CSR_HEADLESS", True)
OLLAMA_MODEL        = os.environ.get("CSR_OLLAMA_MODEL", "llama3.1:8b")
REPORT_FILE         = os.environ.get("CSR_REPORT_FILE", "report.md")
# Имя БД можно переопределить через переменную окружения (для NAS/сетевых дисков)
DB_NAME             = os.environ.get("STARTUPS_DB", "startups.db")
# URL подключения к Redis (для worker.py); если не задан — standalone режим
REDIS_URL           = os.environ.get("REDIS_URL", "")     # пример: redis://192.168.1.10:6379/0
CATEGORIES          = "DeFi | L1/L2 | Infrastructure | Dev Tooling | AI+Crypto | GameFi | RWA | ZK | DA Layer | Stablecoin | Other"

# Кэш: время жизни в часах — профиль не будет пересканироваться раньше
CACHE_TTL_HOURS     = _env_int("CSR_CACHE_TTL_HOURS", 72)
# Кэш AI-вердиктов: через N часов пометим ai_due=1 для повторного анализа
AI_TTL_HOURS        = _env_int("CSR_AI_TTL_HOURS", 168)          # 7 дней

# Rate limiter
RATE_LIMIT_RPS      = _env_float("CSR_RATE_LIMIT_RPS", 1.5)

# Ollama circuit breaker
OLLAMA_TIMEOUT_SEC  = 120
OLLAMA_CB_THRESHOLD = 5
OLLAMA_CB_SLEEP_SEC = 30

# Exponential backoff на 429/403
BACKOFF_BASE_SEC    = 5.0          # начальная пауза
BACKOFF_MAX_SEC     = 300.0        # максимальная пауза (5 мин)

# Ротация User-Agent — имитация разных браузеров
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

# ─────────────────────────────────────────────────────────────────────────────
# МЕТРИКИ
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CrawlMetrics:
    scraped:        int = 0
    cache_hits:     int = 0
    ttl_expired:    int = 0    # профилей с истёкшим TTL (пересканировано)
    pre_filtered:   int = 0
    ai_ok:          int = 0
    ai_errors:      int = 0
    ai_cached:      int = 0    # вердиктов взято из БД
    ai_due_pending: int = 0    # профилей ждут повторного AI-анализа
    http_429:       int = 0
    http_403:       int = 0
    http_other_err: int = 0
    startups_found: int = 0

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "  МЕТРИКИ КРАУЛИНГА",
            f"  Спарсено профилей:      {self.scraped}",
            f"  Кэш-хиты (актуальные): {self.cache_hits}",
            f"  Истёкший TTL (ресканы): {self.ttl_expired}",
            f"  Пре-фильтр:             {self.pre_filtered}",
            f"  AI-анализ успешно:      {self.ai_ok}",
            f"  AI из кэша (БД):        {self.ai_cached}",
            f"  AI ошибки:              {self.ai_errors}",
            f"  Ждут повторного AI:     {self.ai_due_pending}",
            f"  HTTP 429:               {self.http_429}",
            f"  HTTP 403:               {self.http_403}",
            f"  Другие HTTP ошибки:     {self.http_other_err}",
            f"  Стартапов найдено:      {self.startups_found}",
            "=" * 60,
        ]
        return "\n".join(lines)

_metrics = CrawlMetrics()

# ─────────────────────────────────────────────────────────────────────────────
# СЕМАФОР / RATE LIMITER / BACKOFF / CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────────────────────

_semaphore: asyncio.Semaphore | None = None

def get_sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore

class RateLimiter:
    """Токен-бакет: не более rps запросов в секунду глобально."""
    def __init__(self, rps: float):
        self._interval = 1.0 / rps
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()

_rate_limiter: RateLimiter | None = None

def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(RATE_LIMIT_RPS)
    return _rate_limiter

# Экспоненциальный backoff — общий для всего краулера
_backoff_current = BACKOFF_BASE_SEC
_backoff_lock    = asyncio.Lock()

async def apply_backoff(status: int, username: str):
    """Применяет экспоненциальный backoff и обновляет глобальное состояние."""
    global _backoff_current
    async with _backoff_lock:
        wait = _backoff_current * random.uniform(0.8, 1.5)
        wait = min(wait, BACKOFF_MAX_SEC)
        if status == 429:
            _metrics.http_429 += 1
            # Агрессивно растём при 429
            _backoff_current = min(_backoff_current * 2.5, BACKOFF_MAX_SEC)
            logging.warning(f"  [{username}] 429 Rate Limited — backoff {wait:.0f}с (следующий: {_backoff_current:.0f}с)")
        elif status == 403:
            _metrics.http_403 += 1
            _backoff_current = min(_backoff_current * 1.5, BACKOFF_MAX_SEC)
            logging.warning(f"  [{username}] 403 Forbidden — backoff {wait:.0f}с")
        else:
            _metrics.http_other_err += 1
    await asyncio.sleep(wait)

def reset_backoff():
    """Сбрасываем backoff при успешном запросе."""
    global _backoff_current
    _backoff_current = max(BACKOFF_BASE_SEC, _backoff_current * 0.75)

class CircuitBreaker:
    def __init__(self, threshold: int, sleep_sec: float):
        self._threshold  = threshold
        self._sleep_sec  = sleep_sec
        self._failures   = 0
        self._open_until = 0.0

    def record_success(self):
        self._failures = 0

    def record_failure(self):
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = time.monotonic() + self._sleep_sec
            logging.warning(
                f"[CircuitBreaker] Ollama: {self._failures} сбоев подряд — "
                f"пауза {self._sleep_sec}с"
            )

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

_ollama_cb = CircuitBreaker(OLLAMA_CB_THRESHOLD, OLLAMA_CB_SLEEP_SEC)

# Переиспользуемый AI-клиент (не создаём заново на каждый вызов)
_ollama_client: ollama.AsyncClient | None = None

def get_ollama_client() -> ollama.AsyncClient:
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = ollama.AsyncClient()
    return _ollama_client

# ─────────────────────────────────────────────────────────────────────────────
# REGEX И ПРИОРИТИЗАЦИЯ
# ─────────────────────────────────────────────────────────────────────────────

BIO_SELECTORS = [
    '[data-testid="UserDescription"]',
    '[data-testid="UserBio"]',
    'div[data-testid*="Bio"]',
]

_TWEET_SELECTORS = [
    '[data-testid="tweetText"]',
    'article div[lang]',
    'div[data-block="true"]',
]

_MENTION_RE = re.compile(r"@([\w\-]{2,50})", re.UNICODE)

def extract_mentions(texts: list[str]) -> list[str]:
    seen, result = set(), []
    for text in texts:
        clean = re.sub(r'[\r\n\t]+', ' ', text)
        for m in _MENTION_RE.findall(clean):
            lo = m.lower()
            if lo.isdigit() or lo in seen or lo in SKIP_ACCOUNTS:
                continue
            seen.add(lo)
            result.append(m)
    return result

def extract_mentions_with_context(texts: list[str]) -> list[tuple[str, str]]:
    """Возвращает (mention, context_snippet) — предложение, где встретился @ник."""
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for text in texts:
        clean = re.sub(r'[\r\n\t]+', ' ', text)
        sentences = re.split(r'(?<=[.!?])\s+|\n', clean)
        for sentence in sentences:
            for m in _MENTION_RE.findall(sentence):
                lo = m.lower()
                if lo.isdigit() or lo in seen or lo in SKIP_ACCOUNTS:
                    continue
                seen.add(lo)
                result.append((m, sentence.strip()))
    return result

# Scoring signals
_POS_SIGNALS: list[tuple[re.Pattern, int]] = [
    (re.compile(r'\b(building|built|launched|shipping|shipped)\b', re.I), 4),
    (re.compile(r'\b(backed by|funded by|invested by|portfolio)\b', re.I), 4),
    (re.compile(r'\b(protocol|dapp|rollup|layer[\s\-]?2|l2|l1|mainnet|testnet)\b', re.I), 3),
    (re.compile(r'\b(sdk|api|open[\s\-]?source|github|devnet|smart contract)\b', re.I), 3),
    (re.compile(r'\b(seed|pre[\s\-]?seed|series[\s\-]?[ab]|raise|round)\b', re.I), 3),
    (re.compile(r'\b(defi|dex|amm|perp|yield|vault|staking|bridge|oracle)\b', re.I), 2),
    (re.compile(r'\b(zk|zkp|zkvm|zero[\s\-]?knowledge|proof[\s\-]?of)\b', re.I), 2),
    (re.compile(r'\b(stablecoin|synthetic|derivative|options|lending|borrow)\b', re.I), 2),
    (re.compile(r'\b(nft|gamefi|game|metaverse|rwa|tokenize)\b', re.I), 1),
    (re.compile(r'\b(we.re|our|join us|team|hiring|co[\s\-]?founder)\b', re.I), 1),
    (re.compile(r'\b(chain|blockchain|web3|crypto|dao|token|wallet)\b', re.I), 1),
]

_NEG_SIGNALS: list[tuple[re.Pattern, int]] = [
    (re.compile(r'\b(exchange|cex|journalist|reporter|media|newsletter)\b', re.I), -5),
    (re.compile(r'\b(fund|vc|venture|capital|investor|lp|portfolio[\s\-]?company)\b', re.I), -3),
    (re.compile(r'\b(podcast|host|anchor|influencer|commentator)\b', re.I), -3),
    (re.compile(r'\b(regulation|policy|senator|congress|government|law)\b', re.I), -4),
    (re.compile(r'\b(price|chart|ta|analysis|signal|pump|moon|airdrop)\b', re.I), -2),
]

def score_candidate(username: str, context: str) -> int:
    if not context:
        return 0
    score = 0
    for pattern, weight in _POS_SIGNALS:
        if pattern.search(context):
            score += weight
    for pattern, weight in _NEG_SIGNALS:
        if pattern.search(context):
            score += weight
    return score

def get_scroll_rounds(score: int) -> int:
    """Динамически задаёт глубину скролла по приоритету кандидата."""
    if score >= 8:
        return SCROLL_ROUNDS            # максимум для топ-кандидатов
    elif score >= 4:
        return max(SCROLL_ROUNDS_MIN, SCROLL_ROUNDS - 2)
    else:
        return SCROLL_ROUNDS_MIN        # минимальный трафик для слабых кандидатов

# ─────────────────────────────────────────────────────────────────────────────
# КЭШИРОВАНИЕ И TTL
# ─────────────────────────────────────────────────────────────────────────────

def is_cache_valid(parsed_at: str | None, ttl_hours: int = CACHE_TTL_HOURS) -> bool:
    """Возвращает True, если профиль был спарсен в пределах TTL."""
    if not parsed_at:
        return False
    try:
        parsed = datetime.fromisoformat(parsed_at)
        return (datetime.now() - parsed) < timedelta(hours=ttl_hours)
    except (ValueError, TypeError):
        return False

def is_ai_verdict_valid(parsed_at: str | None) -> bool:
    """Возвращает True, если AI-вердикт свежее AI_TTL_HOURS."""
    return is_cache_valid(parsed_at, ttl_hours=AI_TTL_HOURS)

# ─────────────────────────────────────────────────────────────────────────────
# БАЗА ДАННЫХ (NORMALIZED, THREAD-SAFE, WAL)
# ─────────────────────────────────────────────────────────────────────────────

_db_lock = asyncio.Lock()

def init_db_sync():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
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
                ai_due           INTEGER DEFAULT 0,
                external_url     TEXT,
                website_text     TEXT,
                github_stats     TEXT,
                is_seed          INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tweets (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                tweet    TEXT,
                FOREIGN KEY(username) REFERENCES startups(username) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mentions (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                mention  TEXT,
                FOREIGN KEY(username) REFERENCES startups(username) ON DELETE CASCADE
            )
        """)
        # Попытка добавить колонки если таблица уже существовала
        for col in ["ai_due INTEGER DEFAULT 0", "external_url TEXT", "website_text TEXT", "github_stats TEXT", "is_seed INTEGER DEFAULT 0"]:
            try:
                conn.execute(f"ALTER TABLE startups ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_is_valuable ON startups(is_valuable)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_username ON startups(username)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_due ON startups(ai_due)")
        conn.commit()

async def init_db():
    async with _db_lock:
        await asyncio.to_thread(init_db_sync)

def _get_profile_sync(username: str) -> dict | None:
    canon = username.lower()
    with sqlite3.connect(DB_NAME) as conn:
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
        scraped = {
            "username": canon,
            "display_username": row["display_username"],
            "url": row["url"],
            "bio": row["bio"],
            "tweets": tweets,
            "mentions": mentions,
            "external_url": dict(row).get("external_url", ""),
            "website_text": dict(row).get("website_text", ""),
            "github_stats": dict(row).get("github_stats", ""),
        }
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
            "scraped": scraped,
            "ai": ai,
            "depth": row["depth"],
            "pre_filtered": bool(row["pre_filtered"]),
            "filter_reason": row["filter_reason"],
            "parsed_at": row["parsed_at"],
            "ai_due": bool(row["ai_due"]),
        }

async def get_profile_from_db(username: str) -> dict | None:
    # Чтение не требует блокировки — SQLite WAL поддерживает конкурентные читатели
    return await asyncio.to_thread(_get_profile_sync, username)

def _save_profile_sync(username: str, scraped_data: dict, depth: int,
                       ai_res: dict | None = None, pre_filtered: bool = False,
                       filter_reason: str | None = None, ai_due: bool = False,
                       is_seed: bool = False):
    canon   = username.lower()
    display = scraped_data.get("display_username") or username
    url     = scraped_data.get("url", f"https://x.com/{username}")
    bio     = scraped_data.get("bio", "")
    tweets  = scraped_data.get("tweets", [])
    mentions = scraped_data.get("mentions", [])
    external_url = scraped_data.get("external_url", "")
    website_text = scraped_data.get("website_text", "")
    github_stats = scraped_data.get("github_stats", "")
    parsed_at = datetime.now().isoformat()

    is_valuable = None
    category = stage = pitch = red_flags = None
    db_pre_filtered = int(pre_filtered)
    db_filter_reason = filter_reason
    db_is_seed = int(is_seed)

    if ai_res:
        is_valuable = 1 if ai_res.get("is_valuable") else 0
        category    = ai_res.get("category") or "Other"
        stage       = ai_res.get("stage")    or "unknown"
        pitch       = ai_res.get("pitch")    or ""
        red_flags   = ai_res.get("red_flags") or ""
        ai_due      = False   # вердикт только что получен
    elif pre_filtered:
        is_valuable = 0
        category    = "Filtered"
        stage       = "unknown"
        pitch       = ""
        red_flags   = f"Pre-filtered: {filter_reason}"
        ai_due      = False

    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        if ai_res:
            cur.execute("SELECT parsed_at FROM startups WHERE username = ?", (canon,))
            row = cur.fetchone()
            if row and row[0]:
                parsed_at = row[0]

        if not ai_res and not pre_filtered:
            cur.execute("""
                SELECT is_valuable, category, stage, pitch, red_flags, pre_filtered, filter_reason
                FROM startups WHERE username = ?
            """, (canon,))
            row = cur.fetchone()
            if row:
                is_valuable, category, stage, pitch, red_flags, db_pre_filtered, db_filter_reason = row

        cur.execute("SELECT is_seed FROM startups WHERE username = ?", (canon,))
        row = cur.fetchone()
        if row:
            db_is_seed = max(db_is_seed, row[0] or 0)

        cur.execute("""
            INSERT OR REPLACE INTO startups (
                username, display_username, url, bio, depth,
                is_valuable, category, stage, pitch, red_flags,
                pre_filtered, filter_reason, parsed_at, ai_due,
                external_url, website_text, github_stats, is_seed
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (canon, display, url, bio, depth, is_valuable, category, stage,
              pitch, red_flags, int(db_pre_filtered), db_filter_reason, parsed_at, int(ai_due),
              external_url, website_text, github_stats, db_is_seed))
        cur.execute("DELETE FROM tweets WHERE username = ?", (canon,))
        cur.execute("DELETE FROM mentions WHERE username = ?", (canon,))
        cur.executemany("INSERT INTO tweets (username, tweet) VALUES (?,?)",
                        [(canon, t) for t in tweets])
        cur.executemany("INSERT INTO mentions (username, mention) VALUES (?,?)",
                        [(canon, m) for m in mentions])
        conn.commit()

async def save_profile_to_db(username: str, scraped_data: dict, depth: int,
                              ai_res: dict | None = None, pre_filtered: bool = False,
                              filter_reason: str | None = None, ai_due: bool = False,
                              is_seed: bool = False):
    async with _db_lock:
        await asyncio.to_thread(_save_profile_sync, username, scraped_data, depth,
                                ai_res, pre_filtered, filter_reason, ai_due, is_seed)

def _get_active_seeds_sync() -> list[str]:
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT username FROM startups WHERE is_seed = 1")
            return [r[0] for r in cur.fetchall()]
        except sqlite3.OperationalError:
            # Столбец может отсутствовать при первом запуске до миграции
            return []

async def get_active_seeds_from_db() -> list[str]:
    return await asyncio.to_thread(_get_active_seeds_sync)

def _load_all_valuable_sync() -> list[dict]:
    with sqlite3.connect(DB_NAME) as conn:
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
            result.append({
                "scraped": {
                    "username": u,
                    "display_username": row["display_username"],
                    "url": row["url"],
                    "bio": row["bio"],
                    "tweets": tweets,
                    "mentions": mentions,
                },
                "ai": {
                    "is_valuable": True,
                    "category": row["category"],
                    "stage": row["stage"],
                    "pitch": row["pitch"],
                    "red_flags": row["red_flags"],
                },
                "depth": row["depth"],
                "parsed_at": row["parsed_at"]
            })
        return result

def _count_ai_due_sync() -> int:
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM startups WHERE ai_due = 1")
        return cur.fetchone()[0]

# ─────────────────────────────────────────────────────────────────────────────
# SCRAPING LOGIC
# ─────────────────────────────────────────────────────────────────────────────

# Слова, которые однозначно означают «не стартап» — не тратим AI-токены
_PRE_FILTER_WORDS = [
    "exchange", "journalist", "reporter", "newsletter", "media outlet",
    "venture capital", "vc firm", "hedge fund", "news outlet",
    "podcast host", "influencer", "price alert", "signals channel",
    "airdrop hunter", "copy trading",
]

def pre_filter_check(bio: str, tweets: list[str]) -> tuple[bool, str | None]:
    combined = (bio + " " + " ".join(tweets[:3])).lower()  # только первые 3 твита
    for phrase in _PRE_FILTER_WORDS:
        pattern = rf"\b{re.escape(phrase)}\b"
        if re.search(pattern, combined):
            return True, phrase
    return False, None

async def get_or_scrape_profile(context, username: str, depth: int,
                                score: int = 0) -> dict | None:
    """Возвращает кэш если TTL актуален, иначе парсит страницу."""
    cached = await get_profile_from_db(username)
    if cached:
        has_tweets = len(cached.get("scraped", {}).get("tweets", [])) > 0
        effective_ttl = CACHE_TTL_HOURS if has_tweets else 4
        if is_cache_valid(cached.get("parsed_at"), ttl_hours=effective_ttl):
            _metrics.cache_hits += 1
            logging.info(f"  [Кэш ✓] @{username} — актуален ({'есть твиты' if has_tweets else '0 твитов, кулдаун'})")
            return cached["scraped"]
        else:
            _metrics.ttl_expired += 1
            logging.info(f"  [Кэш ↺] @{username} — TTL истёк ({'есть твиты' if has_tweets else '0 твитов, рескан'}), пересканируем")

    res = await scrape_profile(context, username, score=score)
    if res:
        _metrics.scraped += 1
        # Сохраняем как «нет вердикта AI» — пометим ai_due=True
        await save_profile_to_db(username, res, depth=depth, ai_due=True)
        return res
    elif cached:
        logging.warning(f"  [Рескан ⚠] Ошибка парсинга @{username}, используем кэшированную версию")
        return cached["scraped"]
    return None

async def _try_get_bio(page) -> str:
    for sel in BIO_SELECTORS:
        try:
            el = await page.wait_for_selector(sel, timeout=3500)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    logging.debug(f"Bio найдено: {sel}")
                    return text
        except (PlaywrightTimeoutError, Exception):
            continue
    for attr, prop in [('meta[name="description"]', 'content'),
                       ('meta[property="og:description"]', 'content')]:
        try:
            el = await page.query_selector(attr)
            if el:
                val = await el.get_attribute(prop)
                if val:
                    logging.debug(f"Bio fallback: {attr}")
                    return val.strip()
        except Exception:
            pass
    logging.warning("Bio не найдено")
    return ""

async def _try_get_tweets(page, scroll: int) -> list[str]:
    for sel in _TWEET_SELECTORS:
        try:
            await page.wait_for_selector(sel, timeout=8000)
            for _ in range(scroll):
                await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
                # Имитация человеческой прокрутки: случайная пауза + микро-джиттер
                await asyncio.sleep(random.uniform(1.2, 3.5))
                if random.random() < 0.2:
                    # Иногда скроллим чуть обратно — как человек
                    await page.evaluate("window.scrollBy(0, -window.innerHeight * 0.5)")
                    await asyncio.sleep(random.uniform(0.3, 0.8))
            els = await page.query_selector_all(sel)
            tweets, seen_t = [], set()
            for el in els:
                text = (await el.inner_text()).strip()
                if text and len(text) > 10 and text not in seen_t:
                    tweets.append(text)
                    seen_t.add(text)
            if tweets:
                logging.debug(f"Твиты ({len(tweets)} шт.) через: {sel}")
                return tweets
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    logging.warning("Твиты не найдены")
    return []

async def _try_get_external_url(page) -> str:
    try:
        el = await page.query_selector('[data-testid="UserUrl"]')
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return ""

async def scrape_profile(context, username: str, score: int = 0) -> dict | None:
    """Парсит профиль: rate limiter → семафор → viewport jitter → mouse emulation → dynamic scroll."""
    await get_rate_limiter().acquire()

    async with get_sem():
        try:
            page = await context.new_page()
            # Блокируем загрузку тяжелых ресурсов (ОУР.5)
            await page.route("**/*", lambda route: route.abort() 
                if route.request.resource_type in ("image", "media", "font", "stylesheet") 
                else route.continue_()
            )
        except Exception:
            logging.error(f"  [{username}] Ошибка создания вкладки:\n{traceback.format_exc()}")
            return None

        # ─ Случайный размер экрана (ОУР.4) ──────────────────────────────────────
        viewport_w = random.choice([1280, 1366, 1440, 1536, 1920])
        viewport_h = random.choice([720, 768, 900, 960, 1080])
        try:
            await page.set_viewport_size({"width": viewport_w, "height": viewport_h})
        except Exception:
            pass

        result = {
            "username": username.lower(),
            "display_username": username,
            "bio": "",
            "tweets": [],
            "mentions": [],
            "url": f"https://x.com/{username}",
            "external_url": "",
        }

        try:
            # ─ Случайная пауза перед загрузкой (ОУР.4) ────────────────────────────
            await asyncio.sleep(random.uniform(0.4, 1.8))

            resp = await page.goto(
                f"https://x.com/{username}",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            if resp is None:
                logging.warning(f"  [{username}] Нет ответа")
                return None

            status = resp.status
            if status == 404:
                logging.info(f"  [{username}] 404 — не найден")
                return None
            if status in (429, 403):
                await apply_backoff(status, username)
                return None
            if status >= 500:
                _metrics.http_other_err += 1
                logging.warning(f"  [{username}] Ошибка сервера {status}")
                return None

            reset_backoff()

            # ─ Эмуляция движений мыши после загрузки (ОУР.4) ──────────────────
            try:
                await page.mouse.move(
                    random.randint(100, viewport_w - 200),
                    random.randint(100, viewport_h // 2)
                )
                await asyncio.sleep(random.uniform(0.2, 0.6))
                await page.mouse.move(
                    random.randint(100, viewport_w - 200),
                    random.randint(100, viewport_h // 2)
                )
            except Exception:
                pass

            result["bio"]      = await _try_get_bio(page)
            result["external_url"] = await _try_get_external_url(page)
            scroll_n           = get_scroll_rounds(score)
            result["tweets"]   = await _try_get_tweets(page, scroll=scroll_n)
            result["mentions"] = extract_mentions([result["bio"]] + result["tweets"])

        except Exception:
            logging.error(f"  [{username}] Критическая ошибка:\n{traceback.format_exc()}")
        finally:
            try:
                await page.close()
            except Exception:
                pass

        return result

# ─────────────────────────────────────────────────────────────────────────────
# AI-АНАЛИЗ (batch-friendly: принимает один профиль, кэш в БД)
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_with_ai(username: str, bio: str, tweets: list[str], website_text: str = "", github_stats: str = "") -> dict:
    # Берём только 5 самых коротких/информативных твитов по 150 символов — достаточно для классификации
    tweets_text = "\n".join(f"• {t[:150]}" for t in tweets[:5]) or "(no tweets)"
    # Обрезаем bio до 300 символов
    bio_short = (bio or "(no bio)")[:300]
    web_context = f"\nWebsite Content: {website_text[:1000]}" if website_text else ""
    git_context = f"\nGitHub Stats: {github_stats}" if github_stats else ""

    prompt = f"""Crypto investor analyst. Is @{username} an early-stage Web3/crypto TECHNOLOGY STARTUP?

Bio: {bio_short}
Tweets: {tweets_text}{web_context}{git_context}

TRUE if: building a product (protocol/app/tool/infra) + web3/crypto + early stage.
(Pay high attention to GitHub Stats and Website Content if available - they are strong true signals).
FALSE if: VC fund, media, journalist, politician, large exchange, memecoin, spam.
VC-backed startups ("Backed by Paradigm", "Funded by a16z") → TRUE.

Categories: {CATEGORIES}

JSON only:
{{"is_valuable":true/false,"category":"<cat>","stage":"<seed|early|growth|established|unknown>","pitch":"<1-2 sentences>","red_flags":"<concerns or empty>"}}"""

    if _ollama_cb.is_open():
        logging.warning(f"  [{username}] Circuit breaker открыт — пропускаем")
        _metrics.ai_errors += 1
        return {"is_valuable": False, "category": "Unknown", "stage": "unknown",
                "pitch": "", "red_flags": "AI error"}

    for attempt in range(3):
        try:
            client = get_ollama_client()
            resp = await asyncio.wait_for(
                client.generate(
                    model=OLLAMA_MODEL,
                    prompt=prompt,
                    format="json",
                    options={"temperature": 0.05},
                    stream=False,
                ),
                timeout=OLLAMA_TIMEOUT_SEC,
            )
            raw_text = resp.get("response", "{}")
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as e:
                logging.error(f"  [{username}] Ollama невалидный JSON: {e}")
                raise ValueError("JSON Decode Failed")

            required = {"is_valuable", "category", "stage", "pitch", "red_flags"}
            if not required.issubset(data):
                raise ValueError(f"Missing keys: {required - data.keys()}")

            data["is_valuable"] = bool(data["is_valuable"])
            data["category"]    = str(data.get("category")  or "Other")
            data["stage"]       = str(data.get("stage")     or "unknown")
            data["pitch"]       = str(data.get("pitch")     or "")
            data["red_flags"]   = str(data.get("red_flags") or "")
            _ollama_cb.record_success()
            _metrics.ai_ok += 1
            return data

        except asyncio.TimeoutError:
            logging.warning(f"  [{username}] Ollama timeout, попытка {attempt+1}")
            _ollama_cb.record_failure()
            if attempt < 2:
                await asyncio.sleep(2.0 * (attempt + 1))
        except Exception as e:
            _ollama_cb.record_failure()
            if attempt < 2:
                logging.warning(f"  [{username}] AI ошибка, попытка {attempt+1}: {e}")
                await asyncio.sleep(2.0 * (attempt + 1))
            else:
                logging.error(f"  [{username}] Полный сбой AI:\n{traceback.format_exc()}")

    _metrics.ai_errors += 1
    return {"is_valuable": False, "category": "Unknown", "stage": "unknown",
            "pitch": "", "red_flags": "AI error"}

async def analyze_profile(u: str, res: dict, depth: int) -> dict | None:
    """
    Универсальный хелпер: проверяет кэш AI-вердикта (с TTL),
    делает пре-фильтр, запускает Ollama и сохраняет результат.
    Возвращает ai-dict или None.
    """
    cached_full = await get_profile_from_db(u)

    # Проверяем кэшированный вердикт
    if cached_full and cached_full.get("ai") is not None:
        # Проверяем TTL AI-вердикта
        if is_ai_verdict_valid(cached_full.get("parsed_at")):
            ai = cached_full["ai"]
            pre_filtered = cached_full.get("pre_filtered", False)
            reason = cached_full.get("filter_reason")
            _metrics.ai_cached += 1
            if pre_filtered:
                logging.info(f"  Анализ @{u}... [Кэш AI] skip (pre-filtered: {reason})")
            else:
                verdict = "STARTUP" if ai["is_valuable"] else "skip"
                logging.info(f"  Анализ @{u}... [Кэш AI] {verdict} [{ai['category']}]")
            ai["_is_fresh"] = False
            return ai
        else:
            logging.info(f"  Анализ @{u}... [Кэш AI истёк] повторный анализ")

    # Пре-фильтр
    pre_filtered, reason = pre_filter_check(res.get("bio", ""), res.get("tweets", []))
    if pre_filtered:
        _metrics.pre_filtered += 1
        is_seed = reason in ["venture capital", "vc firm", "hedge fund"]
        if is_seed:
            logging.info(f"  [Seed-Эволюция] Обнаружен новый фонд @{u}. Добавляем в Seeds!")
        else:
            logging.info(f"  Анализ @{u}... skip (pre-filtered: {reason})")
        await save_profile_to_db(u, res, depth=depth, pre_filtered=True, filter_reason=reason, is_seed=is_seed)
        return None

    # AI-анализ
    # Добавляем этап обогащения
    website_text, github_stats = await enrich_project(res.get("external_url", ""), res.get("bio", ""))
    res["website_text"] = website_text
    res["github_stats"] = github_stats
    
    ai = await analyze_with_ai(u, res.get("bio", ""), res.get("tweets", []), website_text, github_stats)
    if ai.get("red_flags") == "AI error":
        logging.warning(f"  Анализ @{u}... AI error (retry later, ai_due=True)")
        # Помечаем ai_due=True — при следующем запуске повторим
        await save_profile_to_db(u, res, depth=depth, ai_due=True)
        _metrics.ai_due_pending += 1
        return None

    verdict = "STARTUP" if ai["is_valuable"] else "skip"
    # Решаем, является ли это новым семенем (инфраструктура или L1/L2)
    is_seed = False
    if ai.get("is_valuable") and ai.get("category") in ["Infrastructure", "L1/L2"]:
        is_seed = True
        logging.info(f"  [Seed-Эволюция] Обнаружена экосистема/инфраструктура @{u}. Добавляем в Seeds!")

    logging.info(f"  Анализ @{u}... {verdict} [{ai['category']}]")
    await save_profile_to_db(u, res, depth=depth, ai_res=ai, is_seed=is_seed)
    ai["_is_fresh"] = True
    return ai

# ─────────────────────────────────────────────────────────────────────────────
# ГЕНЕРАЦИЯ ОТЧЁТА
# ─────────────────────────────────────────────────────────────────────────────

CAT_ICONS = {
    "DeFi": "💰", "L1/L2": "⛓️", "Infrastructure": "🏗️",
    "Dev Tooling": "🔧", "AI+Crypto": "🤖", "GameFi": "🎮",
    "RWA": "🏦", "ZK": "🔐", "DA Layer": "📦",
    "Stablecoin": "💵", "Other": "🔮",
}

def generate_report(startups: list[dict]) -> str:
    now = datetime.now().strftime("%d %B %Y, %H:%M")
    lines = [
        f"# Crypto Startup Radar — {now}",
        "",
        f"Найдено потенциально интересных проектов: **{len(startups)}**",
        "",
    ]
    by_cat: dict[str, list] = {}
    for s in startups:
        cat = s["ai"].get("category", "Other")
        by_cat.setdefault(cat, []).append(s)

    for cat, items in sorted(by_cat.items()):
        icon = CAT_ICONS.get(cat, "📌")
        lines += [f"## {icon} {cat} ({len(items)})", ""]
        for s in items:
            ai, data = s["ai"], s["scraped"]
            depth_tag = f" _(глубина {s['depth']})_" if s.get("depth", 1) > 1 else ""
            u   = data.get("display_username") or data["username"]
            bio = data.get("bio", "")
            lines.append(f"### @{u}{depth_tag}")
            lines.append(f"**Ссылка:** https://x.com/{u}  |  **Стадия:** {ai.get('stage', '?')}")
            if bio:
                clean = re.sub(r'\s+', ' ', bio.replace('\n', ' ')).strip()
                lines.append(f"**Bio:** {clean}")
            if ai.get("pitch"):
                lines += ["", "**Почему интересно:**", f"> {ai['pitch']}"]
            if ai.get("red_flags"):
                lines += ["", f"**Риски:** {ai['red_flags']}"]
                
            external_url = data.get("external_url")
            github_stats = data.get("github_stats")
            if external_url:
                lines += ["", f"**Сайт:** {external_url}"]
            if github_stats:
                try:
                    stats = json.loads(github_stats)
                    stars = stats.get("stars", 0)
                    lang = stats.get("language", "Unknown")
                    lines += [f"**GitHub:** {stars} ⭐ | {lang}"]
                except:
                    lines += [f"**GitHub Stats:** {github_stats}"]
            tweets = data.get("tweets", [])
            if tweets:
                lines += ["", "**Свежие твиты:**"]
                for t in tweets[:3]:
                    ex = re.sub(r'\s+', ' ', t).strip()[:280]
                    lines.append(f'> "{ex}"')
            lines += ["", "---", ""]

    return "\n".join(lines)

async def save_reports_live(d1: list[dict], d2: list[dict], history_report_path: Path, main_report_path: Path):
    try:
        all_startups = await asyncio.to_thread(_load_all_valuable_sync)
        report_content = generate_report(all_startups)
        await asyncio.to_thread(main_report_path.write_text, report_content, encoding="utf-8")
        
        run_startups = [s for s in (d1 + d2) if s.get("ai", {}).get("_is_fresh")]
        history_report_content = generate_report(run_startups)
        await asyncio.to_thread(history_report_path.write_text, history_report_content, encoding="utf-8")
    except Exception as exc:
        logging.error(f"Ошибка сохранения live-отчета: {exc}")

# ─────────────────────────────────────────────────────────────────────────────
# ГЛАВНЫЙ КРАУЛЕР
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    global _semaphore
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    await init_db()

    # Загружаем динамические семена из базы данных
    db_seeds = []
    try:
        db_seeds = await get_active_seeds_from_db()
    except Exception as e:
        logging.warning(f"Не удалось загрузить динамические семена из БД: {e}")

    dynamic_seeds = list(set(SEED_ACCOUNTS + db_seeds))
    SKIP_ACCOUNTS.update([s.lower() for s in dynamic_seeds])

    main_report = Path(REPORT_FILE)
    reports_dir = main_report.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    history_report = reports_dir / f"{main_report.stem}_{timestamp}{main_report.suffix}"
    
    # Write initial empty history report so it shows in the dropdown immediately
    await asyncio.to_thread(history_report.write_text, "# Отчет о запуске\n\nПоиск стартапов в процессе...\n", encoding="utf-8")

    logging.info("=" * 60)
    logging.info("  CRYPTO STARTUP RADAR v3.2 — краулер запущен")
    logging.info(f"  Параллельность: {MAX_CONCURRENT}  |  Seed: {len(dynamic_seeds)} (из них динамических: {len(db_seeds)})")
    logging.info(f"  Cache TTL: {CACHE_TTL_HOURS}ч  |  AI TTL: {AI_TTL_HOURS}ч")
    logging.info("=" * 60)

    global_visited: set[str] = {s.lower() for s in dynamic_seeds}
    score_map: dict[str, int] = {}

    d1_analyzed: list[dict] = []
    d2_analyzed: list[dict] = []

    p = await async_playwright().start()
    try:
        ua = random.choice(USER_AGENTS)
        logging.info(f"  UA: {ua[:60]}...")
        context = await p.chromium.launch_persistent_context(
            user_data_dir="twitter_profile",
            channel="chrome",
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-dev-shm-usage",
            ],
            user_agent=ua,
        )
        try:
            # ── ФАЗА 1: Seed-аккаунты ────────────────────────────────────────────
            logging.info(f"[1/4] Парсинг {len(dynamic_seeds)} seed-аккаунтов...")
            seed_results = await asyncio.gather(
                *[get_or_scrape_profile(context, a, depth=0, score=10) for a in dynamic_seeds],
                return_exceptions=True,
            )

            # Собираем кандидатов depth=1 с оценкой по контексту
            depth1_scored: dict[str, tuple[str, int]] = {}
            for res in seed_results:
                if isinstance(res, Exception) or not res:
                    continue
                all_texts = [res.get("bio", "")] + res.get("tweets", [])
                for mention, ctx in extract_mentions_with_context(all_texts):
                    lo = mention.lower()
                    if lo in global_visited:
                        continue
                    s = score_candidate(lo, ctx)
                    if lo not in depth1_scored or s > depth1_scored[lo][1]:
                        depth1_scored[lo] = (mention, s)
                global_visited.update(depth1_scored.keys())
                logging.info(f"  @{res['username']}: {len(res['tweets'])} твитов")

            sorted_d1   = sorted(depth1_scored.values(), key=lambda x: x[1], reverse=True)
            depth1_list = [name for name, s in sorted_d1[:MAX_DEPTH1]]
            score_map.update({name.lower(): s for name, s in sorted_d1[:MAX_DEPTH1]})

            top10 = sorted_d1[:10]
            logging.info(f"  → Кандидатов глубины 1: {len(depth1_list)}")
            logging.info(f"  → Топ-10 по score: {[(n, s) for n, s in top10]}")

            # ── ФАЗА 2: Парсим глубину 1 ─────────────────────────────────────────
            logging.info(f"[2/4] Парсинг {len(depth1_list)} кандидатов (глубина 1)...")
            d1_scraped = await asyncio.gather(
                *[get_or_scrape_profile(context, u, depth=1,
                                        score=score_map.get(u.lower(), 0))
                  for u in depth1_list],
                return_exceptions=True,
            )
            ok1 = sum(1 for r in d1_scraped
                      if not isinstance(r, Exception) and r
                      and (r.get("bio") or r.get("tweets")))
            logging.info(f"  Успешно: {ok1}/{len(depth1_list)}")

            # ── ФАЗА 3: AI-анализ глубины 1 ──────────────────────────────────────
            logging.info("[3/4] AI-анализ глубины 1...")
            depth2_scored: dict[str, tuple[str, int]] = {}

            for res in d1_scraped:
                if isinstance(res, Exception) or not res:
                    continue
                if not res.get("bio") and not res.get("tweets"):
                    continue
                u  = res["username"]
                ai = await analyze_profile(u, res, depth=1)

                if ai and ai["is_valuable"]:
                    d1_analyzed.append({"scraped": res, "ai": ai, "depth": 1})
                    await save_reports_live(d1_analyzed, d2_analyzed, history_report, main_report)
                    all_texts = [res.get("bio", "")] + res.get("tweets", [])
                    for mention, ctx in extract_mentions_with_context(all_texts):
                        lo = mention.lower()
                        if lo in global_visited:
                            continue
                        s = score_candidate(lo, ctx) + 5  # бонус за depth=2 источник
                        if lo not in depth2_scored or s > depth2_scored[lo][1]:
                            depth2_scored[lo] = (mention, s)
                    global_visited.update(depth2_scored.keys())

            sorted_d2   = sorted(depth2_scored.values(), key=lambda x: x[1], reverse=True)
            depth2_list = [name for name, s in sorted_d2[:MAX_DEPTH2]]
            score_map.update({name.lower(): s for name, s in sorted_d2[:MAX_DEPTH2]})
            logging.info(f"  → Кандидатов глубины 2: {len(depth2_list)}")
            if sorted_d2:
                logging.info(f"  → Топ-5 по score: {[(n, s) for n, s in sorted_d2[:5]]}")

            # ── ФАЗА 4: Парсим глубину 2 ─────────────────────────────────────────
            logging.info(f"[4/4] Парсинг {len(depth2_list)} кандидатов (глубина 2)...")
            d2_scraped: list = []
            if depth2_list:
                d2_scraped = await asyncio.gather(
                    *[get_or_scrape_profile(context, u, depth=2,
                                            score=score_map.get(u.lower(), 0))
                      for u in depth2_list],
                    return_exceptions=True,
                )
                ok2 = sum(1 for r in d2_scraped
                          if not isinstance(r, Exception) and r
                          and (r.get("bio") or r.get("tweets")))
                logging.info(f"  Успешно: {ok2}/{len(depth2_list)}")

        finally:
            logging.info("Закрытие браузера...")
            await context.close()
    finally:
        await p.stop()

    # ── AI-анализ глубины 2 ────────────────────────────────────────────────────
    if 'd2_scraped' in locals() and d2_scraped:
        logging.info(f"\nAI-анализ {len(depth2_list)} кандидатов глубины 2...")
        for res in d2_scraped:
            if isinstance(res, Exception) or not res:
                continue
            if not res.get("bio") and not res.get("tweets"):
                continue
            u  = res["username"]
            ai = await analyze_profile(u, res, depth=2)
            if ai and ai["is_valuable"]:
                d2_analyzed.append({"scraped": res, "ai": ai, "depth": 2})
                await save_reports_live(d1_analyzed, d2_analyzed, history_report, main_report)

    # ── Отчёт ─────────────────────────────────────────────────────────────────
    all_startups = await asyncio.to_thread(_load_all_valuable_sync)
    _metrics.startups_found = len(all_startups)
    _metrics.ai_due_pending += await asyncio.to_thread(_count_ai_due_sync)

    # Записываем финальные отчеты на всякий случай
    await save_reports_live(d1_analyzed, d2_analyzed, history_report, main_report)

    run_startups = [s for s in (d1_analyzed + d2_analyzed) if s.get("ai", {}).get("_is_fresh")]

    # Итоговые метрики
    logging.info(_metrics.summary())
    logging.info(f"  Глубина 1: {len(d1_analyzed)}  |  Глубина 2: {len(d2_analyzed)}")
    logging.info(f"  Отчёт: {main_report} (копия: {history_report} с {len(run_startups)} новыми)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Остановка (KeyboardInterrupt)")
    except Exception:
        logging.critical(f"Глобальная ошибка:\n{traceback.format_exc()}")
