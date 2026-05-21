# Crypto Startup Radar

Minimal crawler that scans X.com (Twitter) profiles to surface early-stage crypto/web3 startups.

Requirements

- Python 3.11+
- Playwright and Ollama client (used for AI analysis)

Quick setup

```bash
python -m pip install --upgrade pip
python -m pip install playwright ollama
python -m playwright install chromium
```

Run

```bash
python crawler.py
```

GUI

```bash
python gui.py
```

On Windows, use `py gui.py` if `python` is not in PATH.

The GUI can start/stop `crawler.py` or `worker.py`, stream and persist logs,
show a parsing overview, browse every parsed profile from SQLite, inspect tweets,
mentions, filters, AI status, and read `report.md` as a formatted report.

GUI tabs:

- `Обзор`: parsing totals, category/depth/filter breakdowns, latest profiles.
- `Профили`: every parsed profile, not only valuable startups.
- `Логи`: live process logs plus persisted `logs/crawler_gui.log`.
- `Отчет`: formatted Markdown report with an optional raw Markdown toggle.
- `Данные`: full SQLite contents exported as JSON for inspection.

Runtime settings can also be passed through environment variables:

- `CSR_MAX_DEPTH1`, `CSR_MAX_DEPTH2`
- `CSR_MAX_CONCURRENT`
- `CSR_SCROLL_ROUNDS`, `CSR_SCROLL_ROUNDS_MIN`
- `CSR_CACHE_TTL_HOURS`, `CSR_AI_TTL_HOURS`
- `CSR_RATE_LIMIT_RPS`
- `CSR_HEADLESS`
- `CSR_OLLAMA_MODEL`
- `CSR_REPORT_FILE`
- `CSR_GUI_LOG_FILE`
- `STARTUPS_DB`

Notes

- Browser profile and large files are ignored by `.gitignore` (`twitter_profile/`, `startups.db`, etc.).
- Output report is written to `report.md`.

"As-is" project — adjust settings at the top of `crawler.py` (seeds, concurrency, model).
