# stock-mover-alert2

Slack alerts when watched stocks make big moves, or when broad-market indices swing sharply.

## What triggers an alert

**Per-stock** — any ticker in [`watchlist.txt`](watchlist.txt) that is 5%+ away from the previous session's close **and** either

- stayed beyond 5% for 90+ cumulative minutes during the regular session, or
- closed beyond 5%.

A brief spike past 5% that quickly retraces does not alert.

**Index** — S&P 500 (`^GSPC`) and Nasdaq Composite (`^IXIC`) both up more than +1.5%, or both down more than -1%.

Each (ticker, trigger) is posted once per trading day.

## How it runs

- One GitHub Actions run **watches the whole session**: it checks every 3 minutes from the open until 20 minutes after the close (early closes on half-days are handled).
- GitHub drops most cron slots, so the workflow has many start times; the first one that fires becomes the watcher and the rest exit within seconds (lock step).
- **Catch-up:** every run first re-checks the previous session with final data, so if a day's after-close check never ran, the alert still arrives the next morning, tagged `⏪补报`.
- **Self-monitoring:** a Slack warning is posted if the run crashes (once per day) or if market data is missing for many tickers for 5 loops in a row.
- The repo is public so Actions minutes are free; the Slack webhook lives only in the `SLACK_WEBHOOK_URL` secret.

## Changing the watchlist

Edit `watchlist.txt` (one ticker per line, Yahoo symbols). Keep it in sync with the 美投 tracked-stock list.

## Tweaking

Constants at the top of `alert.py`: `STOCK_THRESHOLD_PCT`, `SUSTAINED_MINUTES_REQUIRED`, `INDEX_UP_THRESHOLD_PCT`, `INDEX_DOWN_THRESHOLD_PCT`, `CHECK_INTERVAL_MIN`.

## Setup (already done)

Slack Incoming Webhook → repo secret `SLACK_WEBHOOK_URL`. Manual test: Actions → stock-mover-alert → Run workflow.
