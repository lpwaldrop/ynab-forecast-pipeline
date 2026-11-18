# YNAB Forecast Pipeline

A Python pipeline that pulls 18 completed months of YNAB budget history in a single API call, compares actual spending against category goal targets, validates a 3-month forecasting model using rolling walk-forward backtesting, and exports a timestamped forward forecast.

## Setup

```powershell
pip install -r requirements.txt
```

## Usage

Save your YNAB personal access token in your OS credential store (Windows Credential Manager, macOS Keychain, or Linux SecretService) under service `"ynab"` and username `"api_token"`:

```powershell
python -m keyring set ynab api_token
```

Then run the pipeline:

```powershell
python ynab_budget_history.py
```

Run the unit tests:

```powershell
python -m pytest --verbose
```

## Sample Output (Synthetic Example)

Generated from the self-contained fake 18-month budget in `test_synthetic_budget_pipeline_end_to_end` ([test_ynab_budget_history.py](test_ynab_budget_history.py)):

```text
Backtest avg miss/category -> Long Avg: $59.11 | 6m Avg: $63.77 | Model: $60.06 (Median: $17.57)
Forecast total spend (next 3m): $9408.18 ($3136.06/mo)
Walk-forward 2026-02 to 2026-04 ( 9m train) -> Long: $54.08 | 6m: $32.79 | Model: $57.07 (Median: $42.48)
Walk-forward 2026-05 to 2026-07 (12m train) -> Long: $85.66 | 6m: $93.74 | Model: $80.44 (Median: $36.95)
Walk-forward 2026-08 to 2026-10 (15m train) -> Long: $59.11 | 6m: $63.77 | Model: $60.06 (Median: $17.57)
Walk-forward overall avg           -> Long: $66.28 | 6m: $63.43 | Model: $65.86
Monte Carlo 3m spend (2000 runs) -> P10: $8339.57 | P50: $9092.80 | P90: $10370.88
```

## Outputs (`outputs/`)

- `ynab_budget_history_18m.csv` — Monthly category-level history (active, non-hidden spending categories)
- `ynab_category_summary_18m.csv` — 18-month vs. 6-month averages, target comparisons, and funding spike counts
- `ynab_category_summary_18m.html` — Interactive Plotly bar chart of `% Difference vs. Monthly Target`
- `ynab_backtest_3m.csv` — Category-level 3-month backtest results
- `ynab_forecast_3m.csv` — Timestamped forward 3-month spend forecast
- `ynab_walkforward_3m.csv` — Rolling walk-forward accuracy across multiple 3-month seasons
- `ynab_montecarlo_3m.csv` — 2,000-run bootstrap Monte Carlo simulation showing P10/P50/P90 3-month spend and upside risk per category

## Methodology & Key Findings

- **Single-call API fetch:** Uses `PlansApi.get_plan_by_id` to retrieve 18 months of history in 1 request (respecting YNAB's 200 req/hr limit).
- **Domain-aware cleaning:** Filtering out hidden/retired categories, income inflows (`Internal Master Category`), `Credit Card Payments` (to avoid double-counting), and `Savings Goals` reduced mean backtest error by ~63%.
- **Simple cadence-hybrid model:** Rolling walk-forward validation across 3 seasons showed that using the full history for annual/irregular categories and the recent 6-month average for monthly goals (`goal_cadence == 1`) outperformed a pure 6-month average across every season.
- **Monte Carlo risk bands:** Bootstrap resampling (2,000 simulated 3-month futures) quantifies P10/P50/P90 total spend ranges and ranks categories by upside variance (`P90 - P50`).
