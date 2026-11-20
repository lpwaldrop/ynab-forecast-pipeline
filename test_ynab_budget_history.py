import csv
import random
import ynab_budget_history
from ynab_budget_history import (
    MONTHS_TO_FETCH,
    SNAPSHOTS_FILENAME,
    audit_forecast_snapshots,
    avg_category_misses,
    compare_3m_projections,
    get_monthly_target,
    milliunits_to_currency,
    project_monthly_spend,
    save_forecast_snapshot,
    simulate_3m_spend,
)


# Helper
def make_rows(activities, goal_cadence=13):
    rows = []
    for month_number, activity in enumerate(activities, start=1):
        rows.append(
            {
                "month": f"2026-{month_number:02d}-01",
                "activity": activity,
                "goal_cadence": goal_cadence,
            }
        )
    return rows

# Tests
def test_milliunits_to_currency():
    assert milliunits_to_currency(12500) == 12.5
    assert milliunits_to_currency(-45670) == -45.67
    assert milliunits_to_currency(None) is None

def test_get_monthly_target():
    assert get_monthly_target(250.0, 1) == 250.0
    assert get_monthly_target(1200.0, 13) == 100.0
    assert get_monthly_target(0.0, 1) is None
    assert get_monthly_target(None, 13) is None

def test_project_monthly_spend_weights_and_hybrid():
    # 6 older months at $100, 6 recent months at $200
    activities = [-100.0] * 6 + [-200.0] * 6
    annual_rows = make_rows(activities, goal_cadence=13)
    monthly_rows = make_rows(activities, goal_cadence=1)

    assert project_monthly_spend(annual_rows, recent_weight=0.0) == 150.0
    assert project_monthly_spend(annual_rows, recent_weight=1.0) == 200.0
    # Hybrid mode uses 6m average for monthly goals (cadence 1) and full average for annual (cadence 13)
    assert project_monthly_spend(annual_rows) == 150.0
    assert project_monthly_spend(monthly_rows) == 200.0

def test_compare_3m_projections_and_avg_misses():
    # 6 sample months at $100/mo, 3 actual months at $200/mo
    rows_by_category = {
        ("Bills", "Internet"): make_rows([-100.0] * 6 + [-200.0] * 3, goal_cadence=13)
    }
    results, test_window_dates = compare_3m_projections(rows_by_category)

    assert test_window_dates == "2026-07 to 2026-09"
    assert len(results) == 1
    assert results[0]["actual_3m_monthly"] == 200.0
    assert results[0]["proj_long_monthly"] == 100.0
    assert results[0]["miss_model"] == 100.0

    avg_long, avg_recent, avg_model, median_model = avg_category_misses(results)
    assert (avg_long, avg_recent, avg_model, median_model) == (100.0, 100.0, 100.0, 100.0)


def test_avg_category_misses_median_vs_mean():
    # One large outlier ($300) shouldn't pull the median ($20) up like the mean ($110)
    results = [
        {"actual_3m_monthly": 100.0, "proj_long_monthly": 90.0, "proj_6m_monthly": 90.0, "miss_model": 10.0},
        {"actual_3m_monthly": 100.0, "proj_long_monthly": 80.0, "proj_6m_monthly": 80.0, "miss_model": 20.0},
        {"actual_3m_monthly": 400.0, "proj_long_monthly": 100.0, "proj_6m_monthly": 100.0, "miss_model": 300.0},
    ]
    avg_long, avg_recent, avg_model, median_model = avg_category_misses(results)
    assert (avg_long, avg_recent, avg_model, median_model) == (110.0, 110.0, 110.0, 20.0)


def test_simulate_3m_spend_constant_and_spiky():
    rows_by_category = {
        ("Bills", "Internet"): make_rows([-100.0] * 6),
        ("Home", "Repairs"): make_rows([-100.0] * 5 + [-500.0]),
        ("Unused", "Zero"): make_rows([0.0] * 6),
    }
    total_summary, category_risks = simulate_3m_spend(rows_by_category, runs=500, seed=42)

    assert len(category_risks) == 2
    assert total_summary["p10"] <= total_summary["p50"] <= total_summary["p90"]
    # Spiky category should rank first by upside_risk_3m; constant category has zero upside risk
    assert category_risks[0]["category_name"] == "Repairs"
    assert category_risks[0]["upside_risk_3m"] > 0
    assert category_risks[1]["category_name"] == "Internet"
    assert (category_risks[1]["p10_3m"], category_risks[1]["p50_3m"], category_risks[1]["p90_3m"]) == (300.0, 300.0, 300.0)
    assert category_risks[1]["upside_risk_3m"] == 0.0


def test_synthetic_budget_pipeline_end_to_end():
    # Self-contained synthetic 18-month budget (reproduces the README sample output)
    rng = random.Random(101)
    months = []
    year, month = 2025, 5
    for _ in range(MONTHS_TO_FETCH):
        months.append(f"{year:04d}-{month:02d}-01")
        month += 1
        if month > 12:
            year, month = year + 1, 1

    rows_by_category = {}
    synthetic_categories = [
        ("Bills", "Rent", 1, lambda i: -1650.0),
        ("Bills", "Utilities", 1, lambda i: -rng.uniform(140, 210)),
        ("Frequent", "Groceries", 1, lambda i: -(430 + i * 6 + rng.uniform(-35, 35))),
        ("Frequent", "Dining Out", 1, lambda i: -rng.uniform(170, 270)),
        ("Frequent", "Shopping", 1, lambda i: -rng.uniform(140, 420)),
        ("Non-Monthly", "Car Maintenance", 13, lambda i: -750.0 if i in (4, 11, 16) else -rng.uniform(0, 60)),
        ("Non-Monthly", "Annual Insurance", 13, lambda i: -1200.0 if i in (2, 14) else 0.0),
    ]
    for group_name, category_name, goal_cadence, get_monthly_spend in synthetic_categories:
        rows_by_category[(group_name, category_name)] = [
            {"month": month_str, "goal_cadence": goal_cadence, "activity": round(get_monthly_spend(i), 2)}
            for i, month_str in enumerate(months)
        ]

    backtest_results, _ = compare_3m_projections(rows_by_category)
    avg_long, avg_recent, avg_model, median_model = avg_category_misses(backtest_results)
    assert (avg_long, avg_recent, avg_model, median_model) == (59.11, 63.77, 60.06, 17.57)

    total_3m = sum(round(project_monthly_spend(rows) * 3, 2) for rows in rows_by_category.values())
    assert total_3m == 9408.18

    total_summary, _ = simulate_3m_spend(rows_by_category)
    assert (total_summary["p10"], total_summary["p50"], total_summary["p90"]) == (8339.57, 9092.8, 10370.88)


def test_save_forecast_snapshot_creates_appends_and_upserts(tmp_path, monkeypatch):
    monkeypatch.setattr(ynab_budget_history, "OUTPUT_FOLDER", tmp_path)
    snapshot_file = tmp_path / SNAPSHOTS_FILENAME

    first_forecast = {"run_date": "2026-10-15", "projected_monthly": 3000.0, "projected_next_3m": 9000.0}
    first_monte_carlo = {"runs": 2000, "p10": 8000.0, "p50": 8900.0, "p90": 10200.0, "mean": 9050.0}
    save_forecast_snapshot(first_forecast, first_monte_carlo, "2026-09", 2950.0)

    second_forecast = {"run_date": "2026-11-10", "projected_monthly": 3100.0, "projected_next_3m": 9300.0}
    second_monte_carlo = {"runs": 2000, "p10": 8200.0, "p50": 9100.0, "p90": 10500.0, "mean": 9250.0}
    save_forecast_snapshot(second_forecast, second_monte_carlo, "2026-10", 3050.0)

    # Re-running later in the same month should update the 2026-10 row rather than duplicating it
    updated_second = {"run_date": "2026-11-19", "projected_monthly": 3150.0, "projected_next_3m": 9450.0}
    save_forecast_snapshot(updated_second, second_monte_carlo, "2026-10", 3050.0)

    with snapshot_file.open("r", newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert [row["last_history_month"] for row in rows] == ["2026-09", "2026-10"]
    assert rows[0]["run_date"] == "2026-10-15"
    assert rows[0]["actual_monthly_spend"] == "2950.0"
    assert rows[0]["projected_next_3m"] == "9000.0"
    assert rows[1]["run_date"] == "2026-11-19"
    assert rows[1]["actual_monthly_spend"] == "3050.0"
    assert rows[1]["projected_monthly"] == "3150.0"
    assert rows[1]["monte_carlo_p50_3m"] == "9100.0"


def test_audit_forecast_snapshots_single_and_multi_month():
    single_snapshot = [
        {"last_history_month": "2026-10", "actual_monthly_spend": "1000.0", "projected_monthly": "1100.0", "monte_carlo_p50_3m": "3000.0"}
    ]
    assert audit_forecast_snapshots(single_snapshot) is None

    ordered_snapshots = [
        {"last_history_month": "2026-10", "actual_monthly_spend": "1000.0", "projected_monthly": "1100.0", "monte_carlo_p50_3m": "3000.0"},
        {"last_history_month": "2026-11", "actual_monthly_spend": "800.0", "projected_monthly": "1200.0", "monte_carlo_p50_3m": "3300.0"},
        {"last_history_month": "2026-12", "actual_monthly_spend": "1900.0", "projected_monthly": "1300.0", "monte_carlo_p50_3m": "3600.0"},
        {"last_history_month": "2027-01", "actual_monthly_spend": 1800.0, "projected_monthly": 1350.0, "monte_carlo_p50_3m": 3900.0},
    ]

    summary = audit_forecast_snapshots(ordered_snapshots)

    assert summary == {
        "months_evaluated": 3,
        "total_comparisons": 6,
        "avg_model_miss": 600.0,
        "avg_monte_carlo_miss": 666.67,
        "avg_model_bias": 500.0,
        "model_wins": 5,
    }