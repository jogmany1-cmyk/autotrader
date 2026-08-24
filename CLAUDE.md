# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository

This repo is a standalone Python trading system (스크리닝 → 전략 앙상블 → Risk Engine → 브로커). It was split out of a combined repo (`jogmany1-cmyk/diary`) that used to hold this code under an `autotrader/` subfolder alongside an unrelated diary web app; the git history was carried over with `git subtree split`, so old commits still say "autotrader v0.x" etc.

All commands below run from the **repo root** (there is no longer an `autotrader/` prefix on the path — the Python package itself is at `autotrader/` inside this repo, i.e. `./autotrader/autotrader/`).

## Target environment

The user runs **Windows**. Give Windows-first instructions: PowerShell (not
bash), `python` (not `python3`), `$env:VAR='...'` for environment variables,
`\` in paths, and Task Scheduler rather than cron.

The runtime code itself is Windows-clean — verified, not assumed: no POSIX-only
imports, no hardcoded POSIX paths, `os.path.join` throughout, every `open()`
names its encoding (so Windows' cp949 default never mangles Korean), and the CSV
writers pass `newline=""` so no blank rows creep in. `python -m autotrader ...`
runs as-is.

Two things do NOT work on Windows:

- `scripts/verify.sh` is bash — it needs Git Bash (bundled with Git for
  Windows), not PowerShell.
- `autotrader schedule` emits 5-field crontab lines. Task Scheduler cannot
  consume them; the jobs must be registered separately (`schtasks` or the GUI).

## Branch policy

Develop on `main`. This repo has no other special branch — unlike the old combined repo, there's no diary content to keep separate from.

## Validation state

This project is currently PRE-LIVE / UNDER VALIDATION.

Passing unit tests or synthetic-data tests does not establish trading validity or profitability.

Required promotion path:

Kiwoom real data → data integrity → historical universe → backtest → OOS → registry approval → paper trading → explicit human approval → live

Never skip a stage. AI agents may recommend promotion but may never grant live approval.

The data-integrity stage has an executable gate: `autotrader validate-data` exits
non-zero when the price data is internally inconsistent (`dataquality.py`). Run it
on every freshly collected cache before backtesting — `CsvProvider` silently drops
unparseable rows and sorts what it reads, so corrupt input otherwise reaches the
backtester without a single complaint.

For non-trivial changes:

inspect → plan → implement → test → verify → report

Never report unverified behaviour as verified.

When a meaningful failure is found, prefer:

failure → regression test → fix → executable gate

over adding more prose rules.

Detailed validation procedures belong in `.claude/skills/`, not in this file.

## Commands

```bash
# End-to-end sanity check with the built-in synthetic data provider
python -m autotrader screen --top 5
python -m autotrader --threshold 0.45 backtest
python -m autotrader --threshold 0.45 paper --cycles 3 --dry-run

# Real data path (CSV under a directory with {SYMBOL}.csv files)
python -m autotrader --csv data/kospi backtest --output out.json

# Auto-collect price history from Kiwoom REST (needs KIWOOM_APP_KEY etc.)
python -m autotrader fetch --cache ./data/kiwoom --symbol 005930 --limit 500
python -m autotrader fetch --cache ./data/kiwoom --minutes 5   # 분봉

# Data integrity gate (run BEFORE trusting any backtest on real data)
python -m autotrader --csv data/kiwoom validate-data
python -m autotrader --csv data/kiwoom validate-data --strict --output runs/quality.json

# Strategy registry gate (only run validated strategies live)
python -m autotrader validate --registry runs/registry.json
python -m autotrader paper --registry runs/registry.json --validated-only

# Print the standard cron schedule (paste into `crontab -e` on Mac/Linux)
python -m autotrader schedule --prefix "python -m autotrader run-job "

# Execute a scheduled job directly (this is what each cron line calls)
python -m autotrader run-job morning-entry --cache ./data/kiwoom
python -m autotrader run-job eod-flat
python -m autotrader run-job collect-daily
python -m autotrader run-job collect-5m
python -m autotrader run-job post-analysis
```

### Global-vs-subcommand flags

`--csv`, `--config`, `--threshold`, `--votes`, `--trail` are **parsed on the parser itself, before the subcommand name**:

```bash
# correct
python -m autotrader --threshold 0.45 backtest
# wrong — argparse will reject with 'unrecognized arguments'
python -m autotrader backtest --threshold 0.45
```

### Testing

```bash
./scripts/verify.sh            # everything at once — run this before pushing
pip install pytest             # optional dep
pytest -q                      # entire suite (currently ~160 tests)
pytest tests/test_backtest.py  # one file
pytest -q -k "cost_audit"      # keyword filter
```

`scripts/verify.sh` runs the unit tests, the stdlib-only guard, the synthetic
end-to-end smoke (screen → backtest → paper), and — when a price cache exists —
`validate-data`. It reports every failure at once and exits non-zero if any step
fails. Point it at another cache with `CACHE=data/kospi ./scripts/verify.sh`.
GitHub Actions (`.github/workflows/verify.yml`) runs this exact script on
Python 3.9 / 3.11 / 3.13 for every push, so local and CI never diverge. A second
job installs `.[live,dev]` and runs the full suite, because the vendor-adapter
tests skip themselves when `requests` is absent (`tests/_optional.py`) — without
that job they would never run anywhere. In a bare environment `pytest -q` is
130 passed + 10 skipped; with `requests` installed it is 140 passed.

The stdlib-only rule above is enforced by `scripts/check_stdlib_only.py`, not by
trust: it blocks `numpy`/`pandas`/`requests`/`websockets`/`yaml` at the import
hook, then imports every module and runs a backtest. That makes the check valid
even on a machine where those packages happen to be installed — this dev
container has `requests` and `yaml`, so a bare `pytest` run proves nothing about
the constraint. Optional vendor deps must stay lazily imported inside functions
(see `broker/kis.py`).

There is no separate lint/typecheck tooling wired up. Keep runtime code stdlib-only unless explicitly needed — the core (models, indicators, strategies, backtest, risk, portfolio, live, streaming) must work without `numpy`/`pandas`/`requests` because tests rely on that.

## Architecture (big-picture)

The system is a pipeline whose stages are strictly ordered so the model doesn't accidentally couple layers. Read the flow top-down when tracing behaviour:

```
DataProvider (CSV | Synthetic | Kiwoom REST)
   → Screener (tier1 price → tier2 indicator → tier3 ranking)
      → Ensemble of 5 strategies (DayBreakout, DayPullback, DayMomentum,
                                    SwingTrend, MeanReversion) — weighted vote
         → RiskEngine (position sizing + account limits + cooldown + chase
                        filter + daily cap + hard stop)   ← final authority
            → Broker (PaperBroker | KISBroker | KiwoomBroker)
               → Portfolio (trailing stop, EOD flat, target/stop exits)
                  → PredictionTracker + Metrics (CostAudit, win rate,
                                                 PF, Sortino, MDD, …)
```

Key invariants that anything you touch must preserve:

- **No look-ahead.** Strategies see only bars `[0..at]`. Backtester decides on today's close and fills on the next bar's open. `StrategyContext.at` is the boundary — never read past it.
- **RiskEngine has veto power over every entry.** Signals are advisory; `RiskEngine.evaluate_entry(...)` is the last gate. Wire new features (chase filter, daily trade cap, cooldown) here, not inside strategies.
- **Costs are always included.** `Costs` (commission bp + tax bp + slippage bp) is applied by `PaperBroker`, and `metrics.build_cost_audit` surfaces turnover and cost-to-capital in every `BacktestReport`. If a change silently bypasses `Fill.cost`, backtests overstate returns.
- **Strategies are pure and stateless** — configured in `__init__`, decisions in `evaluate(ctx)`. Add new ones by subclassing `Strategy` and giving them a `name` that matches a field on `StrategyWeights`. `Ensemble` finds them via the weight map.
- **`DataProvider` is the seam between backtest and live.** `CsvProvider` and `KiwoomProvider` write to the same CSV layout, so swapping providers keeps the rest of the pipeline unchanged. Never call vendor APIs from strategy/risk code — go through the provider.
- **LLMs never place orders.** The design deliberately keeps AI-shaped decisions (news sentiment, regime hints) as inputs *before* the ensemble score; execution stays deterministic. Do not add a code path where an LLM response short-circuits `RiskEngine`.

### Notable subsystems

- **`market.py`** — KRX holiday table + NXT extended sessions (`pre` 08:00–08:59, `regular` 09:00–15:30, `after` 15:30–20:00). `LiveTrader.cycle` bails out early on `session=closed`.
- **`reconciler.SourceReconciler`** — cross-source dedup for the "KRX-only vs KRX+NXT" leak problem. Runs the same predicate on two providers and reports `only_in_secondary` as the leak set.
- **`registry.StrategyRegistry`** — JSON-backed approval store. `paper --validated-only` filters the ensemble down to strategies whose latest OOS backtest passes (`profit_factor ≥ 1.2`, `trades ≥ 20`, `mdd ≥ -0.25`, ≤ 90 days old). Live trading should never bypass this gate.
- **`streaming/`** — abstract `StreamClient` (thread + queue). `LocalStream` for tests; `KiwoomConditionStream` is a WebSocket skeleton that emits `signal`/`heartbeat` events. `LiveTrader.stream` (optional) `drain()`s events at the end of each cycle and pushes hits through the same `Ensemble → Risk → Broker` pipeline as polled candidates.
- **`scheduler.JobRegistry` + `jobs.py`** — 5-field cron parser and the actual callables cron invokes (`morning-entry`, `eod-flat`, `collect-daily`, `collect-5m`, `post-analysis`). Jobs fall back to `CsvProvider` gracefully when Kiwoom credentials are missing.
- **`notify.Notifier`** — fan-out notification bus. `ConsoleChannel` is the default; add vendor channels (Slack/Telegram/…) by implementing `NotificationChannel`. A failing channel must not break trading — the notifier swallows per-channel exceptions on purpose.
- **`KiwoomProvider` cache format** is intentionally identical to `CsvProvider`. Daily bars go to `{cache_dir}/{symbol}.csv`; minute bars to `{cache_dir}/{symbol}_{interval}m.csv`. Merge is date-keyed with fresh > old.
- **`KrxUniverse`** — JSONL snapshots of historical listings for survivorship-bias defence. `union_between(start, end)` returns the *union* over the interval (i.e. includes delisted symbols).

### Config

`Config` (dataclass tree in `autotrader/config.py`) is the single source of numeric truth. `Config.load(path)` merges a YAML overlay onto defaults (YAML is optional — if PyYAML is missing, only defaults are used). Broker credentials come from environment: `KIS_APP_KEY` / `KIS_APP_SECRET` / `KIS_ACCOUNT_NUMBER` / `KIS_MODE` and the parallel `KIWOOM_*` set. `KISConfig.from_env()` / `KiwoomConfig.from_env()` are the only readers — do not scatter `os.getenv` elsewhere.
