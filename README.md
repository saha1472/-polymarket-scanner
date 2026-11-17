# Polymarket scanner (paper trading)

A bot that tests trading ideas on [Polymarket](https://polymarket.com) **on paper only**. It places no real orders and holds no keys or money. It runs on GitHub Actions every 15 minutes, saves its results back to this repo, and posts to Discord.

Nothing here is proven to make money. Most ideas tested so far lose or are within noise; `scorecard.py` shows the current state.

## What runs every 15 minutes

`.github/workflows/scan.yml`, in this order:

1. **Self-tests.** If any test fails, nothing below trades, notifies or saves.
2. **Arbitrage scanner** (`scanner.py`).
3. **Copy-trading scans** (`signals.py`: general, sports, esports, tech, weather, geopolitics) and the **weather desk** (`weather.py`), side by side, each with its own ledger.
4. **Shadow experiments** that only record what would have happened: the forecast-model log (`wx_ensemble.py`), its daily calibration check (`wx_calibrate.py`) and the politics consensus test (`shadow_politics.py`).
5. **Book watcher** (`watch_books.py`) and the **rewards tracker** (`rewards_tracker.py`), a hypothetical market-making record.
6. **Daily heartbeat** to Discord (`heartbeat.py`) and the **scorecard** on the run's summary page (`scorecard.py`).
7. **Save**: ledgers and logs are committed back to the repo.

## Files

| File | What it does |
|---|---|
| `simcore.py` | Shared rules: ledgers, safe writes, settlement, audit records, Discord, monthly logs, small helpers |
| `signals.py` | Copy-trading paper bot. `python signals.py SPORTS` runs one scan; no argument runs the general scan |
| `weather.py` | Weather desk strategies (DEAD, LATE, MORNING, AFTERNOON, SHIFT) |
| `fees.py` | Each market's own taker fee |
| `shadow.py`, `shadow_politics.py` | Shadow records and the automatic pause/resume switch |
| `wx_ensemble.py`, `wx_ensemble_eval.py`, `wx_calibrate.py` | Forecast-model shadow log, its scorer, and the daily model-versus-market check |
| `rewards_estimate.py`, `rewards_tracker.py`, `watch_books.py` | Liquidity-reward estimate, paper market-making record, 30-second book watcher |
| `heartbeat.py`, `scanner.py` | Daily summary, arbitrage scanner |
| `tests/` | Self-tests, run before every scan |

### Reports you can run by hand (all read-only)

| Command | Question it answers |
|---|---|
| `python scorecard.py` | Is any strategy proven yet, and how many more bets would it take? |
| `python shadow_report.py` | What would the skipped and paused signals have made? |
| `python copy_quality.py` | Which copied traders made money for us, and does chasing their price hurt? |
| `python clv_report.py` | Did the price move our way after we bought? |
| `python verify_settlements.py` | Do the ledgers agree with Polymarket's official results? |
| `python rewards_tracker.py --report` | The rewards tracker's record so far |
| `python wx_ensemble_eval.py` | The forecast model scored against settled results |

## Data files

- `ledger*.json`: one paper account per strategy ($50 start, $1 bets).
- `shadow_*.json`: "what would have happened" records. They never touch a ledger.
- `traders_*.json`: cached trader scores.
- `wx_shadow*.jsonl`, `rewards_log*.jsonl`: append-only logs, **one file per month** (for example `wx_shadow_2026-11.jsonl`). The original un-dated files are still read but no longer grow.
- `wx_state.json`, `wx_calibration.json`, `wx_winners.json`, `rewards_state.json`, `heartbeat.json`: working state.

## Switching a strategy off by hand

- A copy scan: add its name to `MANUAL_PAUSE` in `signals.py`.
- A weather strategy: add its name to `MANUAL_PAUSE` in `weather.py`.

A paused strategy places no new paper bets, still settles what is open, and keeps recording its signals as shadow bets. Delete the line to switch it back on.

## Running it yourself

```
pip install -r requirements.txt
python -m unittest discover -s tests
python signals.py TECH
```

Set `DISCORD_WEBHOOK_URL` to get the messages; without it everything is only printed. On GitHub it is a repository secret of the same name.

## Not for real money as it stands

GitHub Actions is not a place to run live orders (runs can be late or skipped, and this repo is public). The conditions for going live are tracked separately in the go/no-go notes.
