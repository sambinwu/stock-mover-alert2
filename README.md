# stock-mover-alert2

Slack alerts when watched stocks make big intraday moves, or when broad-market indices swing sharply.

## What triggers an alert

The job runs every 15 minutes during US market hours (Mon-Fri) via GitHub Actions. It posts a single Slack message per (asset, trigger-type) per US trading day.

**Per-stock trigger** — any of these tickers moving more than 5% (up or down) vs the previous close:

`NVDA, META, TSLA, PLTR, MSFT, TEM, TSM, NFLX, GOOGL, AMZN, GS, COST, INTC, ORCL`

**Index trigger** — both indices moving the same direction past a threshold:
- Both **S&P 500 (^GSPC)** and **Nasdaq Composite (^IXIC)** up more than +1.5%, OR
- Both down more than -1%

## One-time setup (do this yourself)

1. **Create a Slack Incoming Webhook**
   - Go to https://api.slack.com/apps and click **Create New App** → *From scratch*.
   - Name it (e.g. "Stock Mover Alert"), pick your Slack workspace.
   - In the app settings, click **Incoming Webhooks** → toggle **Activate Incoming Webhooks** on.
   - Click **Add New Webhook to Workspace**, choose the Slack channel where you want alerts, and click **Allow**.
   - Copy the resulting URL (looks like `https://hooks.slack.com/services/T0.../B0.../xxxx`).

2. **Add the webhook as a GitHub Actions secret**
   - In this repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `SLACK_WEBHOOK_URL`
   - Value: paste the webhook URL.
   - Click **Add secret**.

3. **(Optional) Trigger a test run**
   - Go to **Actions → stock-mover-alert → Run workflow** and pick `main`.
   - The job will fetch quotes and only post if any trigger fires; otherwise it exits silently.

## How it works

- `alert.py` fetches quotes via `yfinance` (last price + previous close) for each ticker plus `^GSPC`/`^IXIC`, computes intraday %, and posts to Slack.
- `state/alerted-YYYY-MM-DD.json` tracks which alerts already fired today so you don't get spammed across the 15-minute runs.
- The state file is persisted between runs via `actions/cache` keyed on the ET date.

## Files

- `alert.py` — alerting logic
- `requirements.txt` — Python dependencies (`requests`, `yfinance`)
- `.github/workflows/alert.yml` — the GitHub Actions schedule and runner

## Tweaking

Edit the constants at the top of `alert.py`:

- `WATCHLIST` — tickers to monitor
- `STOCK_THRESHOLD_PCT` — per-stock move threshold (default 5.0)
- `INDEX_UP_THRESHOLD_PCT` / `INDEX_DOWN_THRESHOLD_PCT` — index thresholds
