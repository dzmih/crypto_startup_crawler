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

Notes

- Browser profile and large files are ignored by `.gitignore` (`twitter_profile/`, `startups.db`, etc.).
- Output report is written to `report.md`.

"As-is" project — adjust settings at the top of `crawler.py` (seeds, concurrency, model).
