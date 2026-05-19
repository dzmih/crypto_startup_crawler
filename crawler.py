"""
crawler.py — Crypto Startup Radar v2.2
Одна браузерная сессия. Семафор ограничивает параллельность (нет бана от X.com).
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')

import asyncio
import json
import re
import random
from datetime import datetime
from pathlib import Path
import sqlite3

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import ollama

# ─────────────────────────────────────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────────────────────────────────────

SEED_ACCOUNTS = [
    # Крипто-VC фонды
    "Paradigm", "a16zcrypto", "Dragonfly_xyz", "multicoincap",
    "PanteraCapital", "polychain", "HaunVentures", "SpartanGroup_",
    "hashed_official", "BainCapCrypto", "electriccapital", "1confirmation",
    "GalaxyDigital", "animocabrands", "delphi_digital", "DCGco",
    "RibbonFinance", "banklessvc", "cbventures",
    # Builders / known founders
    "VitalikButerin", "balajis", "naval", "jessepollak", "gakonst",
    "stani", "rleshner", "haydenzadams",
]

SKIP_ACCOUNTS = {
    # Биржи и кошельки
    "binance", "coinbase", "kraken", "okx", "bybit", "kucoin", "gemini",
    "bitfinex", "coinbasewallet",
    # Крупные сети
    "ethereum", "bitcoin", "solana", "bnbchain", "polkadot", "avalancheavax",
    # Медиа
    "coindesk", "cointelegraph", "theblock__", "decrypt_co", "wublockchain",
    # Политики
    "senlummis", "senthomtillis", "senatortimscott", "realdonaldtrump",
    "jdvance", "keirstarmer",
    # Не-крипто и медийные персоны
    "google", "microsoft", "apple", "twitter", "x", "elonmusk", "coupang",
    "anthropicai", "joerogan", "saylor", "michael_saylor", "cobie",
    "unitedwayabc", "boysclubworld", "crypto_council", "remyblarenews",
    "consensus2026", "cbventures",
    # Все seed-аккаунты
    "paradigm", "a16zcrypto", "dragonfly_xyz", "multicoincap", "multicoin",
    "panteracapital", "polychain", "haunventures", "spartangroup_",
    "hashed_official", "baincapcrypto", "electriccapital", "1confirmation",
    "galaxydigital", "animocabrands", "delphi_digital", "dcgco",
    "ribbonfinance", "banklessvc", "vitalikbuterin", "balajis", "naval",
    "jessepollak", "gakonst", "stani", "rleshner", "haydenzadams",
}

MAX_DEPTH1     = 50    # кандидатов с уровня 1
MAX_DEPTH2     = 60    # кандидатов с уровня 2
SCROLL_ROUNDS  = 5     # прокруток на профиль (~25 твитов)
MAX_CONCURRENT = 6     # максимум одновременных вкладок (защита от бана)
HEADLESS       = True
OLLAMA_MODEL   = "gemma2:27b"
REPORT_FILE    = "report.md"
DB_NAME        = "startups.db"
CATEGORIES     = "DeFi | L1/L2 | Infrastructure | Dev Tooling | AI+Crypto | GameFi | RWA | ZK | DA Layer | Stablecoin | Other"

# ─────────────────────────────────────────────────────────────────────────────
# ПАРСИНГ
# ─────────────────────────────────────────────────────────────────────────────

BIO_SELECTORS = [
    '[data-testid="UserDescription"]',
    '[data-testid="UserBio"]',
    'div[data-testid*="Bio"]',
]

# Семафор создаём лениво, уже внутри event loop
_semaphore: asyncio.Semaphore | None = None

def get_sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore

def extract_mentions(texts: list[str]) -> list[str]:
    seen, result = set(), []
    for text in texts:
        for m in re.findall(r"@(\w+)", text):
            lo = m.lower()
            if lo not in seen and lo not in SKIP_ACCOUNTS and len(lo) > 1:
                seen.add(lo)
                result.append(m)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# БАЗА ДАННЫХ И ПРЕФИЛЬТР
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS startups (
                username TEXT PRIMARY KEY,
                url TEXT,
                bio TEXT,
                tweets TEXT,
                mentions TEXT,
                depth INTEGER,
                is_valuable INTEGER,
                category TEXT,
                stage TEXT,
                pitch TEXT,
                red_flags TEXT,
                pre_filtered INTEGER DEFAULT 0,
                filter_reason TEXT,
                parsed_at TEXT
            )
        """)
        conn.commit()

def get_profile_from_db(username: str) -> dict | None:
    init_db()
    with sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM startups WHERE username = ?", (username.lower(),))
        row = cursor.fetchone()
        if row:
            scraped = {
                "username": row["username"],
                "url": row["url"],
                "bio": row["bio"],
                "tweets": json.loads(row["tweets"]),
                "mentions": json.loads(row["mentions"]),
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
            }
    return None

def save_profile_to_db(username: str, scraped_data: dict, depth: int, ai_res: dict | None = None, pre_filtered: bool = False, filter_reason: str | None = None):
    init_db()
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        tweets_json = json.dumps(scraped_data.get("tweets", []))
        mentions_json = json.dumps(scraped_data.get("mentions", []))
        parsed_at = datetime.now().isoformat()
        
        is_valuable = None
        category = None
        stage = None
        pitch = None
        red_flags = None
        
        if ai_res:
            is_valuable = 1 if ai_res.get("is_valuable") else 0
            category = ai_res.get("category") or "Other"
            stage = ai_res.get("stage") or "unknown"
            pitch = ai_res.get("pitch") or ""
            red_flags = ai_res.get("red_flags") or ""
        elif pre_filtered:
            is_valuable = 0
            category = "Filtered"
            stage = "unknown"
            pitch = ""
            red_flags = f"Pre-filtered due to word: {filter_reason}"
        
        cursor.execute("""
            INSERT OR REPLACE INTO startups (
                username, url, bio, tweets, mentions, depth,
                is_valuable, category, stage, pitch, red_flags,
                pre_filtered, filter_reason, parsed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            username.lower(),
            scraped_data.get("url", f"https://x.com/{username}"),
            scraped_data.get("bio", ""),
            tweets_json,
            mentions_json,
            depth,
            is_valuable,
            category,
            stage,
            pitch,
            red_flags,
            1 if pre_filtered else 0,
            filter_reason,
            parsed_at
        ))
        conn.commit()

def load_all_valuable_startups() -> list[dict]:
    init_db()
    with sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM startups WHERE is_valuable = 1")
        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "scraped": {
                    "username": row["username"],
                    "url": row["url"],
                    "bio": row["bio"],
                    "tweets": json.loads(row["tweets"]),
                    "mentions": json.loads(row["mentions"]),
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

def pre_filter_check(bio: str, tweets: list[str]) -> tuple[bool, str | None]:
    combined = (bio + " " + " ".join(tweets)).lower()
    for word in ["exchange", "journalist"]:
        if re.search(rf"\b{word}\b", combined):
            return True, word
    return False, None

async def get_or_scrape_profile(context, username: str, depth: int) -> dict | None:
    cached = get_profile_from_db(username)
    if cached:
        print(f"  [БД Кэш] Используем сохранённый профиль @{username}")
        return cached["scraped"]
    
    res = await scrape_profile(context, username)
    if res:
        save_profile_to_db(username, res, depth=depth)
    return res

async def scrape_profile(context, username: str) -> dict | None:
    """Парсит профиль с ограничением параллельности через семафор."""
    async with get_sem():
        page = await context.new_page()
        result = {
            "username": username,
            "bio": "",
            "tweets": [],
            "mentions": [],
            "url": f"https://x.com/{username}",
        }
        try:
            resp = await page.goto(
                f"https://x.com/{username}",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            if resp and resp.status == 404:
                return None

            # Bio
            for sel in BIO_SELECTORS:
                try:
                    el = await page.wait_for_selector(sel, timeout=5000)
                    text = (await el.inner_text()).strip() if el else ""
                    if text:
                        result["bio"] = text
                        break
                except PlaywrightTimeoutError:
                    continue

            # Твиты + прокрутка
            try:
                await page.wait_for_selector('[data-testid="tweetText"]', timeout=15000)
                for _ in range(SCROLL_ROUNDS):
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
                    await asyncio.sleep(random.uniform(1.2, 2.0))
                tweet_els = await page.query_selector_all('[data-testid="tweetText"]')
                seen_t: set[str] = set()
                for el in tweet_els:
                    text = (await el.inner_text()).strip()
                    if text and text not in seen_t and len(text) > 10:
                        result["tweets"].append(text)
                        seen_t.add(text)
            except PlaywrightTimeoutError:
                pass

            result["mentions"] = extract_mentions([result["bio"]] + result["tweets"])

        except Exception as e:
            print(f"  [{username}] Ошибка: {e}")
        finally:
            await page.close()

        return result


# ─────────────────────────────────────────────────────────────────────────────
# AI-АНАЛИЗ
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_with_ai(username: str, bio: str, tweets: list[str]) -> dict:
    tweets_text = "\n".join(f"• {t[:300]}" for t in tweets[:15]) or "(no tweets)"

    prompt = f"""You are a sharp early-stage crypto investor analyst.

Evaluate this Twitter profile and decide if it represents an early-stage Web3 / crypto TECHNOLOGY STARTUP worth tracking for investment.

Profile: @{username}
Bio: {bio or '(no bio)'}
Recent tweets:
{tweets_text}

=== STRICT CLASSIFICATION RULES ===
Mark is_valuable=TRUE only when ALL are true:
1. Product-building team (protocol, app, tool, infrastructure)
2. Web3 / crypto / blockchain space
3. Early-to-growth stage (NOT Uniswap/Aave/Compound/MakerDAO level with billions in TVL)

Mark is_valuable=FALSE if ANY applies:
- VC fund, accelerator, or investor account
- Media, news, event, advocacy, or lobbying account
- Personal account of investor / journalist / politician
- Large established protocol or exchange
- Memecoin, pure NFT project, spam
- Non-crypto company or unrelated topic

=== SPECIAL RULE — VC-BACKED STARTUP ===
Bio phrases like "Backed by Paradigm", "Funded by a16z", "Supported by Dragonfly", "@[VC] investor" → STRONG evidence of a startup. Mark is_valuable=TRUE.
Bio phrases like "building", "the first X on-chain", "protocol", "we're building" → lean TRUE.

Categories: {CATEGORIES}

Return ONLY valid JSON:
{{
  "is_valuable": true/false,
  "category": "<category>",
  "stage": "<seed | early | growth | established | unknown>",
  "pitch": "<2-3 sentences: problem solved and why an investor would care>",
  "red_flags": "<key concerns or empty string>"
}}"""

    for attempt in range(3):
        try:
            client = ollama.AsyncClient()
            resp = await client.generate(
                model=OLLAMA_MODEL,
                prompt=prompt,
                format="json",
                options={"temperature": 0.05},
                stream=False,
            )
            data = json.loads(resp.get("response", "{}"))
            required = {"is_valuable", "category", "stage", "pitch", "red_flags"}
            if not required.issubset(data):
                raise ValueError(f"Missing keys: {required - data.keys()}")
            data["is_valuable"] = bool(data["is_valuable"])
            data["category"]    = str(data.get("category")  or "Other")
            data["stage"]       = str(data.get("stage")     or "unknown")
            data["pitch"]       = str(data.get("pitch")     or "")
            data["red_flags"]   = str(data.get("red_flags") or "")
            return data
        except Exception:
            if attempt < 2:
                await asyncio.sleep(1.5)

    return {"is_valuable": False, "category": "Unknown", "stage": "unknown",
            "pitch": "", "red_flags": "AI error"}


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
            u = data["username"]
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
            tweets = data.get("tweets", [])
            if tweets:
                lines += ["", "**Свежие твиты:**"]
                for t in tweets[:3]:
                    ex = re.sub(r'\s+', ' ', t).strip()[:280]
                    lines.append(f'> "{ex}"')
            lines += ["", "---", ""]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ГЛАВНЫЙ КРАУЛЕР
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    global _semaphore
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    init_db()

    print("=" * 60)
    print("  CRYPTO STARTUP RADAR v2.3 — краулер запущен")
    print(f"  Параллельность: {MAX_CONCURRENT} вкладок  |  Seed: {len(SEED_ACCOUNTS)}")
    print("=" * 60)

    global_visited: set[str] = {s.lower() for s in SEED_ACCOUNTS}

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir="twitter_profile",
            channel="chrome",
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )

        # ── ФАЗА 1: Парсим seed-аккаунты ─────────────────────────────────────
        print(f"\n[1/4] Парсинг {len(SEED_ACCOUNTS)} seed-аккаунтов...")
        seed_results = await asyncio.gather(
            *[get_or_scrape_profile(context, a, depth=0) for a in SEED_ACCOUNTS],
            return_exceptions=True,
        )
        depth1_set: set[str] = set()
        for res in seed_results:
            if isinstance(res, Exception) or not res:
                continue
            new = [m for m in res.get("mentions", []) if m.lower() not in global_visited]
            depth1_set.update(new)
            global_visited.update(m.lower() for m in res.get("mentions", []))
            tw, nd = len(res["tweets"]), len(new)
            print(f"  @{res['username']}: {tw} твитов -> {nd} кандидатов")

        depth1_list = list(depth1_set)[:MAX_DEPTH1]
        print(f"\n  -> Кандидатов глубины 1: {len(depth1_list)}")

        # ── ФАЗА 2: Парсим кандидатов глубины 1 ──────────────────────────────
        print(f"\n[2/4] Парсинг {len(depth1_list)} кандидатов (глубина 1)...")
        d1_scraped = await asyncio.gather(
            *[get_or_scrape_profile(context, u, depth=1) for u in depth1_list],
            return_exceptions=True,
        )
        ok1 = sum(1 for r in d1_scraped
                  if not isinstance(r, Exception) and r
                  and (r.get("bio") or r.get("tweets")))
        print(f"  Успешно спарсено: {ok1}/{len(depth1_list)}")

        # ── ФАЗА 3: AI-анализ глубины 1, определяем кандидатов глубины 2 ────
        print(f"\n[3/4] AI-анализ глубины 1...")
        d1_analyzed: list[dict] = []
        depth2_set: set[str] = set()

        for res in d1_scraped:
            if isinstance(res, Exception) or not res:
                continue
            if not res.get("bio") and not res.get("tweets"):
                continue
            u = res["username"]
            
            # Проверяем наличие кэшированного вердикта AI в БД
            cached_full = get_profile_from_db(u)
            if cached_full and cached_full.get("ai") is not None:
                ai = cached_full["ai"]
                pre_filtered = cached_full.get("pre_filtered", False)
                reason = cached_full.get("filter_reason")
                if pre_filtered:
                    print(f"  Анализ @{u}... [БД Кэш] skip (pre-filtered: {reason})")
                else:
                    verdict = "STARTUP" if ai["is_valuable"] else "skip"
                    print(f"  Анализ @{u}... [БД Кэш] {verdict} [{ai['category']}]")
            else:
                # Проводим префильтрацию и AI анализ
                print(f"  Анализ @{u}...", end=" ", flush=True)
                pre_filtered, reason = pre_filter_check(res.get("bio", ""), res.get("tweets", []))
                if pre_filtered:
                    print(f"skip (pre-filtered: {reason})")
                    ai = None
                    save_profile_to_db(u, res, depth=1, pre_filtered=True, filter_reason=reason)
                else:
                    ai = await analyze_with_ai(u, res.get("bio", ""), res.get("tweets", []))
                    verdict = "STARTUP" if ai["is_valuable"] else "skip"
                    print(f"{verdict} [{ai['category']}]")
                    save_profile_to_db(u, res, depth=1, ai_res=ai)

            if ai and ai["is_valuable"]:
                d1_analyzed.append({"scraped": res, "ai": ai, "depth": 1})
                new = [m for m in res.get("mentions", [])
                       if m.lower() not in global_visited]
                depth2_set.update(new)
                global_visited.update(m.lower() for m in res.get("mentions", []))

        depth2_list = list(depth2_set)[:MAX_DEPTH2]
        print(f"\n  -> Кандидатов глубины 2: {len(depth2_list)}")

        # ── ФАЗА 4: Парсим кандидатов глубины 2 (тот же браузер!) ───────────
        print(f"\n[4/4] Парсинг {len(depth2_list)} кандидатов (глубина 2)...")
        d2_scraped: list = []
        if depth2_list:
            d2_scraped = await asyncio.gather(
                *[get_or_scrape_profile(context, u, depth=2) for u in depth2_list],
                return_exceptions=True,
            )
            ok2 = sum(1 for r in d2_scraped
                      if not isinstance(r, Exception) and r
                      and (r.get("bio") or r.get("tweets")))
            print(f"  Успешно спарсено: {ok2}/{len(depth2_list)}")

        await context.close()  # ОДНО закрытие

    # ── AI-анализ глубины 2 (браузер уже закрыт) ────────────────────────────
    d2_analyzed: list[dict] = []
    if d2_scraped:
        print(f"\nAI-анализ {len(depth2_list)} кандидатов глубины 2...")
        for res in d2_scraped:
            if isinstance(res, Exception) or not res:
                continue
            if not res.get("bio") and not res.get("tweets"):
                continue
            u = res["username"]
            
            # Проверяем наличие кэшированного вердикта AI в БД
            cached_full = get_profile_from_db(u)
            if cached_full and cached_full.get("ai") is not None:
                ai = cached_full["ai"]
                pre_filtered = cached_full.get("pre_filtered", False)
                reason = cached_full.get("filter_reason")
                if pre_filtered:
                    print(f"  Анализ @{u}... [БД Кэш] skip (pre-filtered: {reason})")
                else:
                    verdict = "STARTUP" if ai["is_valuable"] else "skip"
                    print(f"  Анализ @{u}... [БД Кэш] {verdict} [{ai['category']}]")
            else:
                # Проводим префильтрацию и AI анализ
                print(f"  Анализ @{u}...", end=" ", flush=True)
                pre_filtered, reason = pre_filter_check(res.get("bio", ""), res.get("tweets", []))
                if pre_filtered:
                    print(f"skip (pre-filtered: {reason})")
                    ai = None
                    save_profile_to_db(u, res, depth=2, pre_filtered=True, filter_reason=reason)
                else:
                    ai = await analyze_with_ai(u, res.get("bio", ""), res.get("tweets", []))
                    verdict = "STARTUP" if ai["is_valuable"] else "skip"
                    print(f"{verdict} [{ai['category']}]")
                    save_profile_to_db(u, res, depth=2, ai_res=ai)

            if ai and ai["is_valuable"]:
                d2_analyzed.append({"scraped": res, "ai": ai, "depth": 2})

    # ── Отчёт ────────────────────────────────────────────────────────────────
    all_startups = load_all_valuable_startups()
    Path(REPORT_FILE).write_text(generate_report(all_startups), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  Готово! Стартапов найдено: {len(all_startups)}")
    print(f"  Глубина 1: {len(d1_analyzed)}  |  Глубина 2: {len(d2_analyzed)}")
    print(f"  Отчёт: {REPORT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
