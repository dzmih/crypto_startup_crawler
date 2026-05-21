# Архитектура масштабирования: Crypto Startup Radar

> [!NOTE]
> Этот документ описывает путь от одной машины к распределённой системе.
> Все шаги строго опциональны и последовательны — каждый уровень независим.

## Текущее состояние (v3.2 — одна машина)

```
crawler.py
├── Rate limiter (токен-бакет, 1.5 req/s)
├── Семафор (6 вкладок параллельно)
├── SQLite + WAL (один процесс)
├── TTL-кэш (72ч профили, 168ч AI-вердикты)
├── ai_due флаг (отложенный анализ)
├── Exponential backoff (429/403)
├── Circuit breaker (Ollama)
└── Метрики в конце запуска
```

---

## Уровень 1: Несколько запусков на одной машине (уже работает)

Запускай краулер несколько раз в день — TTL-кэш гарантирует, что уже обработанные
профили не парсятся снова. Смысл: **без лишнего трафика получаешь свежий AI-анализ**
для профилей, у которых истёк `AI_TTL_HOURS`.

```powershell
# Просто повторный запуск — crawler сам разберётся с кэшем
py crawler.py
```

---

## Уровень 2: Несколько ПК без платных сервисов

### Вариант A: Shared SQLite через сетевой диск (наименьшие изменения)

> [!WARNING]
> SQLite + сетевой диск даёт конкурентные блокировки. Работает только если **один
> process пишет, остальные читают**. Для нескольких пишущих — нужен Postgres.

```
PC1 (crawler) ──┐
PC2 (crawler) ──┼──► NAS / OneDrive / Samba share / startups.db (WAL режим)
PC3 (viewer)  ──┘
```

**Шаг минимальной настройки:**
1. Перенести `startups.db` на общую папку (OneDrive, Samba)
2. В каждом `crawler.py` изменить: `DB_NAME = "\\\\NAS\\share\\startups.db"`
3. Распределить `SEED_ACCOUNTS` по машинам (разные seed'ы на каждой)

### Вариант B: Redis-очередь (рекомендуется)

Redis разворачивается локально на одной машине, все воркеры на других ПК
подключаются по локальной сети. **Полностью бесплатно.**

```
                  ┌─────────────────┐
                  │  Redis (PC1)    │  ← очередь заданий
                  │  port 6379      │
                  └────────┬────────┘
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    Worker (PC1)     Worker (PC2)     Worker (PC3)
    playwright       playwright       playwright
    + Ollama         + Ollama         + Ollama
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                    Postgres (PC1)
                    port 5432
```

**Установка Redis на Windows (без платных сервисов):**
```powershell
# через WSL2 (рекомендуется)
wsl --install
wsl sudo apt install redis-server
wsl redis-server

# или через Docker Desktop
docker run -p 6379:6379 redis:alpine
```

**Минимальный код воркера с Redis:**
```python
import redis
import asyncio

r = redis.Redis(host='192.168.1.100', port=6379, decode_responses=True)

def enqueue_candidates(candidates: list[str]):
    """Добавляет кандидатов в очередь (атомарно, без дубликатов)."""
    for username in candidates:
        r.sadd("visited", username.lower())   # глобальный set уже посещённых
        r.lpush("queue:depth1", username)

def dequeue_candidate() -> str | None:
    """Воркер берёт следующее задание."""
    item = r.rpop("queue:depth1")
    return item

# В scrape-воркере:
while True:
    username = dequeue_candidate()
    if not username:
        await asyncio.sleep(5)
        continue
    result = await scrape_profile(context, username, score=0)
    # сохранение в Postgres...
```

---

## Уровень 3: Postgres вместо SQLite

Postgres нужен только при **множественном параллельном доступе** (несколько машин
одновременно пишут). Устанавливается бесплатно локально.

```powershell
# Windows: установка через winget
winget install PostgreSQL.PostgreSQL

# или Docker
docker run -e POSTGRES_PASSWORD=secret -p 5432:5432 postgres:16
```

**Замена SQLite → Postgres в crawler.py:**
```python
# pip install asyncpg
import asyncpg

_pg_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = await asyncpg.create_pool(
            "postgresql://postgres:secret@localhost/startups",
            min_size=2, max_size=10
        )
    return _pg_pool

# Вместо asyncio.to_thread + sqlite3:
async def save_profile_pg(username: str, scraped: dict, ...):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO startups (username, bio, ...) VALUES ($1, $2, ...)
            ON CONFLICT (username) DO UPDATE SET ...
        """, username.lower(), scraped["bio"], ...)
```

Схема таблиц идентична SQLite (те же `startups`, `tweets`, `mentions`).
`PRAGMA journal_mode=WAL` заменяется на дефолтный Postgres MVCC — он уже конкурентный.

---

## Уровень 4: Ротация поведения (анти-бан без прокси)

Уже реализовано в v3.2:
- ✅ Рандомизированный User-Agent (`USER_AGENTS` список, выбирается при каждом запуске)
- ✅ Рандомизированные задержки скролла (`random.uniform(1.2, 3.5)`)
- ✅ Иногда скроллим назад (имитация человека, 20% вероятность)
- ✅ Динамический `SCROLL_ROUNDS` по score

**Дополнительно можно добавить:**
```python
# Случайная задержка перед goto()
await asyncio.sleep(random.uniform(0.5, 2.0))

# Случайные размеры viewport
await page.set_viewport_size({
    "width":  random.choice([1280, 1366, 1440, 1920]),
    "height": random.choice([720, 768, 900, 1080])
})

# Эмуляция движений мыши перед кликом
await page.mouse.move(random.randint(100, 500), random.randint(100, 400))
```

---

## Уровень 5: Tor (осторожно)

> [!CAUTION]
> Tor медленный (~1-3 Мбит/с), X.com часто его блокирует. Используй только
> для коротких сессий при уже сработавшем 429, не как основной транспорт.

```python
# pip install aiohttp[socks]
# Запуск Tor Browser или tor service

context = await p.chromium.launch_persistent_context(
    user_data_dir="twitter_profile_tor",
    proxy={"server": "socks5://127.0.0.1:9050"},  # порт Tor SOCKS
    headless=HEADLESS,
)
```

---

## Рекомендуемая дорожная карта

| Шаг | Когда делать | Усилие |
|-----|-------------|--------|
| Повторные запуски (TTL) | Сейчас | ✅ Готово |
| Несколько seed-наборов на разных ПК → общий SQLite на NAS | 2+ машины | 1ч |
| Redis очередь + разделение seed'ов | Хочется параллельного старта | 4ч |
| Postgres | 3+ машин пишут одновременно | 6ч |
| asyncpg вместо sqlite3 в crawler.py | После Postgres | 3ч |

> [!TIP]
> Самый дешёвый способ масштабирования без Postgres и Redis:
> **Запускай несколько копий с разными `SEED_ACCOUNTS` на разных ПК,
> раз в ночь синхронизируй базы скриптом `rsync`/`xcopy`**. Никаких серверов.
