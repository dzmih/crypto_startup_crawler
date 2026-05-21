"""
worker.py — Crypto Startup Radar: Redis-queue worker
=====================================================
Запуск на ЛЮБОЙ машине в сети (PC1, PC2, PC3 ...):

    py worker.py

Режимы:
  1. REDIS_URL задан в окружении → читает задания из Redis очереди.
     Несколько копий могут работать параллельно на разных машинах.
     Используй DB через DATABASE_URL (Postgres) для общего хранилища.

  2. REDIS_URL не задан → standalone (как старый crawler.py),
     обрабатывает собственный список SEED_ACCOUNTS из crawler.py.

Настройка (переменные окружения):
  REDIS_URL      redis://192.168.1.10:6379/0        (Redis сервер)
  DATABASE_URL   postgresql://user:pass@host/db      (Postgres; без него SQLite)
  STARTUPS_DB    \\\\NAS\\share\\startups.db            (путь к SQLite; для NAS)
  WORKER_ID      pc1 / pc2 / ...                    (для логирования)

Запуск Redis (WSL2 или Docker):
  wsl sudo service redis-server start
  docker run -p 6379:6379 redis:alpine
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("worker")

# ─── Импорт из основного модуля ───────────────────────────────────────────────
from crawler import (
    SEED_ACCOUNTS, SKIP_ACCOUNTS, MAX_CONCURRENT, HEADLESS,
    MAX_DEPTH1, MAX_DEPTH2, OLLAMA_MODEL, REPORT_FILE, USER_AGENTS,
    RATE_LIMIT_RPS, CACHE_TTL_HOURS, AI_TTL_HOURS,
    _semaphore, get_sem, get_rate_limiter, _ollama_cb,
    is_cache_valid, score_candidate, get_scroll_rounds,
    extract_mentions, extract_mentions_with_context,
    pre_filter_check, scrape_profile,
    analyze_profile, generate_report,
    _metrics,
)
import db  # абстрактный слой БД (SQLite или Postgres)

from playwright.async_api import async_playwright

# ─── Конфигурация воркера ─────────────────────────────────────────────────────
REDIS_URL  = os.environ.get("REDIS_URL", "")
WORKER_ID  = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")

# Redis-ключи очереди
Q_DEPTH1  = "csr:queue:depth1"   # List[username]
Q_DEPTH2  = "csr:queue:depth2"   # List[username]
Q_SCORES  = "csr:scores"         # Hash[username → score]
VISITED   = "csr:visited"        # Set[username_lower]

# ─────────────────────────────────────────────────────────────────────────────
# Redis-слой (подключается только если REDIS_URL задан)
# ─────────────────────────────────────────────────────────────────────────────

class RedisQueue:
    """Обёртка над redis-py для атомарных операций с очередью кандидатов."""

    def __init__(self, url: str):
        try:
            import redis as redis_lib
            self._r = redis_lib.from_url(url, decode_responses=True, socket_connect_timeout=5)
            self._r.ping()
            log.info(f"[Redis] Подключён: {url}")
        except ImportError:
            raise RuntimeError("redis не установлен: pip install redis")
        except Exception as e:
            raise RuntimeError(f"[Redis] Ошибка подключения: {e}")

    # ── Очередь ────────────────────────────────────────────────────────────────

    def enqueue(self, queue: str, username: str, score: int = 0):
        """Добавляет кандидата в очередь только если ещё не посещался."""
        lo = username.lower()
        if self._r.sadd(VISITED, lo) == 0:
            return  # уже в множестве посещённых
        self._r.hset(Q_SCORES, lo, score)
        # Вставляем в отсортированное множество (ZADD) по score
        self._r.zadd(queue, {lo: score})

    def dequeue(self, queue: str) -> tuple[str, int] | None:
        """Атомарно извлекает кандидата с наибольшим score."""
        # ZPOPMAX возвращает элемент с наибольшим score
        items = self._r.zpopmax(queue, count=1)
        if not items:
            return None
        username, score = items[0]
        return username, int(score)

    def queue_size(self, queue: str) -> int:
        return self._r.zcard(queue)

    def enqueue_many(self, queue: str, scored: dict[str, int]):
        """Пакетно добавляет кандидатов в очередь."""
        pipeline = self._r.pipeline()
        for lo, score in scored.items():
            if self._r.sadd(VISITED, lo):
                self._r.hset(Q_SCORES, lo, score)
                pipeline.zadd(queue, {lo: score})
        pipeline.execute()

    def mark_visited(self, username: str):
        self._r.sadd(VISITED, username.lower())

    def is_visited(self, username: str) -> bool:
        return self._r.sismember(VISITED, username.lower())

    def flush_all(self):
        """Очищает все очереди (для сброса состояния)."""
        self._r.delete(Q_DEPTH1, Q_DEPTH2, Q_SCORES, VISITED)
        log.warning("[Redis] Очереди сброшены")

# ─────────────────────────────────────────────────────────────────────────────
# Режим 1: Распределённый Redis-воркер
# ─────────────────────────────────────────────────────────────────────────────

async def redis_worker_main():
    """
    Режим нескольких машин:
    1. Seed-аккаунты добавляются в Redis один раз (обычно с PC1-координатора).
    2. Каждый воркер берёт задания из Redis и скрапит.
    3. Все пишут в одну общую БД (Postgres или NAS-SQLite).
    """
    rq = RedisQueue(REDIS_URL)

    # Инициализируем БД
    await db.init()

    log.info(f"[{WORKER_ID}] Redis-режим запущен")
    log.info(f"[{WORKER_ID}] Depth1 queue: {rq.queue_size(Q_DEPTH1)} заданий")
    log.info(f"[{WORKER_ID}] Depth2 queue: {rq.queue_size(Q_DEPTH2)} заданий")

    # Если обе очереди пусты — заполняем из наших seed'ов
    if rq.queue_size(Q_DEPTH1) == 0 and rq.queue_size(Q_DEPTH2) == 0:
        log.info(f"[{WORKER_ID}] Очереди пусты — заполняем из SEED_ACCOUNTS")
        # Добавляем seed'ы как начальные задания глубины 0
        seeds_scored = {s.lower(): 10 for s in SEED_ACCOUNTS}
        rq.enqueue_many(Q_DEPTH1, seeds_scored)

    ua = random.choice(USER_AGENTS)
    p = await async_playwright().start()
    try:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=f"twitter_profile_{WORKER_ID}",
            channel="chrome",
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=ua,
        )
        log.info(f"[{WORKER_ID}] Браузер запущен, UA: {ua[:50]}...")

        try:
            await _process_queue(context, rq, Q_DEPTH1, depth=1)
            await _process_queue(context, rq, Q_DEPTH2, depth=2)
        finally:
            await context.close()
    finally:
        await p.stop()

    # Генерируем отчёт из общей БД
    all_startups = await db.load_valuable()
    _metrics.startups_found = len(all_startups)
    report = generate_report(all_startups)
    
    main_report = Path(REPORT_FILE)
    main_report.write_text(report, encoding="utf-8")
    
    reports_dir = main_report.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    history_report = reports_dir / f"{main_report.stem}_{timestamp}{main_report.suffix}"
    history_report.write_text(report, encoding="utf-8")
    
    log.info(f"Отчет обновлен: {main_report} (копия: {history_report})")
    log.info(_metrics.summary())
    await db.close()

async def _process_queue(context, rq: RedisQueue, queue: str, depth: int):
    """Обрабатывает очередь: берёт задания пачками по MAX_CONCURRENT."""
    batch_size = MAX_CONCURRENT * 3  # Берём больше за раз для эффективности
    processed  = 0

    while True:
        # Собираем пачку заданий
        batch: list[tuple[str, int]] = []
        for _ in range(batch_size):
            item = rq.dequeue(queue)
            if item is None:
                break
            batch.append(item)

        if not batch:
            log.info(f"[{WORKER_ID}] Очередь {queue} пуста")
            break

        log.info(f"[{WORKER_ID}] Обрабатываем {len(batch)} кандидатов из {queue}...")

        # Параллельный скрапинг пачки
        tasks = [
            scrape_and_analyze(context, username, score, depth, rq)
            for username, score in batch
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        processed += len(batch)
        log.info(f"[{WORKER_ID}] Обработано: {processed} из {queue}")

async def scrape_and_analyze(context, username: str, score: int,
                              depth: int, rq: RedisQueue | None = None):
    """Скрапит + анализирует один профиль и добавляет новых кандидатов в очередь."""
    # Проверяем кэш
    cached = await db.get_profile(username)
    if cached and is_cache_valid(cached.get("parsed_at")):
        _metrics.cache_hits += 1
        log.info(f"  [Кэш ✓] @{username}")
        res = cached["scraped"]
    else:
        res = await scrape_profile(context, username, score=score)
        if not res:
            return
        _metrics.scraped += 1
        await db.save_profile(username, res, depth=depth, ai_due=True)

    # AI-анализ
    ai = await analyze_profile(username, res, depth=depth)

    # Если это стартап — добавляем его упоминания как кандидатов depth+1
    if ai and ai.get("is_valuable") and rq and depth < 2:
        all_texts = [res.get("bio", "")] + res.get("tweets", [])
        next_queue = Q_DEPTH2 if depth == 1 else None
        if next_queue:
            for mention, ctx in extract_mentions_with_context(all_texts):
                lo = mention.lower()
                if lo in SKIP_ACCOUNTS:
                    continue
                s = score_candidate(lo, ctx) + 5  # бонус за подтверждённый источник
                rq.enqueue(next_queue, lo, score=s)

# ─────────────────────────────────────────────────────────────────────────────
# Режим 2: Standalone (без Redis, как старый crawler.py)
# ─────────────────────────────────────────────────────────────────────────────

async def standalone_main():
    """Fallback: запускает краулер на одной машине без Redis."""
    log.info("Redis не настроен — запуск в standalone режиме")
    log.info("Для распределённого режима: export REDIS_URL=redis://HOST:6379/0")

    # Импортируем и запускаем обычный main из crawler.py
    import crawler
    await crawler.main()

# ─────────────────────────────────────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────────────────────────────────────

async def run():
    if REDIS_URL:
        await redis_worker_main()
    else:
        await standalone_main()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Остановка (KeyboardInterrupt)")
    except Exception:
        log.critical(f"Глобальная ошибка:\n{traceback.format_exc()}")
