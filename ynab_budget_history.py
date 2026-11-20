import csv
import datetime
import random
import keyring
import plotly.graph_objects as go
import ynab
from pathlib import Path
from statistics import median

# Settings
MONTHS_TO_FETCH = 18
RECENT_MONTHS = 6
RECENT_WEIGHT = 0.0
SPLIT_BY_GOAL_CADENCE = True
SPIKE_THRESHOLD = 1.25
TARGET_BAND_PCT = 10
SIMULATION_RUNS = 2000
SIMULATION_SEED = 42
EXCLUDED_CATEGORIES = []
EXCLUDED_GROUPS = ["Internal Master Category", "Credit Card Payments", "Savings Goals"]

# YNAB goal cadence codes
MONTHLY_GOAL_CADENCE = 1
ANNUAL_GOAL_CADENCE = 13

# Output files
SCRIPT_FOLDER = Path(__file__).resolve().parent
OUTPUT_FOLDER = SCRIPT_FOLDER.joinpath("outputs")
HISTORY_FILENAME = f"ynab_budget_history_{MONTHS_TO_FETCH}m.csv"
SUMMARY_FILENAME = f"ynab_category_summary_{MONTHS_TO_FETCH}m.csv"
CHART_FILENAME = f"ynab_category_summary_{MONTHS_TO_FETCH}m.html"
BACKTEST_FILENAME = "ynab_backtest_3m.csv"
FORECAST_FILENAME = "ynab_forecast_3m.csv"
SIMULATION_FILENAME = "ynab_montecarlo_3m.csv"
SNAPSHOTS_FILENAME = "ynab_forecast_snapshots.csv"

# Helpers
def get_cutoff_month(months_back):
    today = datetime.date.today()

    # Subtract months using a 0-based month index so year boundaries wrap cleanly
    cutoff_month_index = today.year * 12 + (today.month - 1) - months_back
    year, month_zero_based = divmod(cutoff_month_index, 12)
    # YNAB identifies every budget month by the 1st of that month (YYYY-MM-01)
    return datetime.date(year, month_zero_based + 1, 1)

def milliunits_to_currency(value):
    # YNAB stores money in milliunits (1000 = $1.00)
    if value is None:
        return None
    return round(value / 1000.0, 2)

def write_csv(filename, rows):
    if not rows:
        return
    OUTPUT_FOLDER.mkdir(exist_ok=True)
    path = OUTPUT_FOLDER.joinpath(filename)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

def get_monthly_target(goal_target, goal_cadence):
    if goal_target:
        if goal_cadence == MONTHLY_GOAL_CADENCE:
            return round(goal_target, 2)
        if goal_cadence == ANNUAL_GOAL_CADENCE:
            return round(goal_target / 12.0, 2)
    return None

def project_monthly_spend(category_rows, recent_weight=None):
    # Use recent average for monthly goals when cadence splitting is enabled
    if recent_weight is None:
        if SPLIT_BY_GOAL_CADENCE and category_rows[-1]["goal_cadence"] == MONTHLY_GOAL_CADENCE:
            recent_weight = 1.0
        else:
            recent_weight = RECENT_WEIGHT

    # Blend the full-window average with the most recent N months
    # Flip activity sign so spending is positive and refunds reduce total spent
    long_avg = sum((row["activity"] * -1) for row in category_rows) / len(category_rows)
    recent_month_rows = category_rows[-RECENT_MONTHS:]
    recent_avg = sum((row["activity"] * -1) for row in recent_month_rows) / len(recent_month_rows)
    return round((1 - recent_weight) * long_avg + recent_weight * recent_avg, 2)

def compare_3m_projections(rows_by_category, train_months=None):
    # Score 3-month predictions for each category on one (sample -> actual) window
    results = []
    test_window_dates = ""

    for (category_group, category_name), category_rows in rows_by_category.items():
        if train_months is None:
            if len(category_rows) < 6:
                continue
            train_month_rows = category_rows[:-3]
            test_month_rows = category_rows[-3:]
        else:
            if len(category_rows) < train_months + 3:
                continue
            train_month_rows = category_rows[:train_months]
            test_month_rows = category_rows[train_months:train_months + 3]

        if not test_window_dates:
            test_window_dates = f"{test_month_rows[0]['month'][:7]} to {test_month_rows[-1]['month'][:7]}"

        # recent_weight=0.0 averages the full test_month_rows slice without weighting
        actual_monthly_spend = project_monthly_spend(test_month_rows, recent_weight=0.0)
        long_avg_projected = project_monthly_spend(train_month_rows, recent_weight=0.0)
        recent_avg_projected = project_monthly_spend(train_month_rows, recent_weight=1.0)
        model_projected = project_monthly_spend(train_month_rows)

        if actual_monthly_spend == 0 and long_avg_projected == 0 and recent_avg_projected == 0:
            continue

        candidates = {
            "Long": abs(actual_monthly_spend - long_avg_projected),
            "6m": abs(actual_monthly_spend - recent_avg_projected),
            "Model": abs(actual_monthly_spend - model_projected),
        }
        best_method = min(candidates, key=candidates.get)

        results.append(
            {
                "category_group": category_group,
                "category_name": category_name,
                "actual_3m_monthly": actual_monthly_spend,
                "proj_long_monthly": long_avg_projected,
                "proj_6m_monthly": recent_avg_projected,
                "proj_model_monthly": model_projected,
                "miss_model": round(actual_monthly_spend - model_projected, 2),
                "closest_method": best_method,
            }
        )

    return results, test_window_dates

def avg_category_misses(results):
    avg_miss_long = round(sum(abs(row["actual_3m_monthly"] - row["proj_long_monthly"]) for row in results) / len(results), 2)
    avg_miss_recent = round(sum(abs(row["actual_3m_monthly"] - row["proj_6m_monthly"]) for row in results) / len(results), 2)
    avg_miss_model = round(sum(abs(row["miss_model"]) for row in results) / len(results), 2)
    median_miss_model = round(median(abs(row["miss_model"]) for row in results), 2)
    return avg_miss_long, avg_miss_recent, avg_miss_model, median_miss_model

def percentile(sorted_values, pct):
    index = int((len(sorted_values) - 1) * pct)
    return round(sorted_values[index], 2)

def simulate_3m_spend(rows_by_category, runs=SIMULATION_RUNS, seed=SIMULATION_SEED):
    # Bootstrap-sample 3 months per category from its 18-month history to estimate total spend risk
    rng = random.Random(seed)
    total_runs = [0.0] * runs
    category_risks = []

    for (category_group, category_name), category_rows in rows_by_category.items():
        # Clamp net-refund months to $0 so bootstrap draws represent non-negative spend
        monthly_spends = [abs(min(0, row["activity"])) for row in category_rows]
        if sum(monthly_spends) == 0:
            continue

        category_runs = []
        for i in range(runs):
            spend_3m = sum(rng.choices(monthly_spends, k=3))
            category_runs.append(spend_3m)
            total_runs[i] += spend_3m

        category_runs.sort()
        p10 = percentile(category_runs, 0.10)
        p50 = percentile(category_runs, 0.50)
        p90 = percentile(category_runs, 0.90)
        category_risks.append(
            {
                "category_group": category_group,
                "category_name": category_name,
                "p10_3m": p10,
                "p50_3m": p50,
                "p90_3m": p90,
                "upside_risk_3m": round(p90 - p50, 2),
            }
        )

    category_risks.sort(key=lambda row: row["upside_risk_3m"], reverse=True)

    total_runs.sort()
    total_summary = {
        "runs": runs,
        "p10": percentile(total_runs, 0.10),
        "p50": percentile(total_runs, 0.50),
        "p90": percentile(total_runs, 0.90),
        "mean": round(sum(total_runs) / runs, 2),
    }
    return total_summary, category_risks

# Pipeline
def export_budget_history():
    token = keyring.get_password("ynab", "api_token")
    if not token:
        raise ValueError("No YNAB token found in Credential Manager (service='ynab', username='api_token').")
    plan_id = "last-used"

    cutoff_month = get_cutoff_month(MONTHS_TO_FETCH)
    current_month = datetime.date.today().replace(day=1)
    configuration = ynab.Configuration(access_token=token)

    rows = []
    with ynab.ApiClient(configuration) as api_client:
        # Fetch the full plan in 1 API call instead of 19 separate month calls
        plan_response = ynab.PlansApi(api_client).get_plan_by_id(plan_id)

        # YNAB leaves category_group_name blank on monthly categories, so look it up by group ID
        group_names = {}
        for group in plan_response.data.plan.category_groups:
            group_names[group.id] = group.name

        target_months = [
            month_detail for month_detail in plan_response.data.plan.months
            if cutoff_month <= month_detail.month < current_month
        ]
        target_months.sort(key=lambda month_detail: month_detail.month)

        for month_detail in target_months:
            for category in month_detail.categories:
                group_name = group_names.get(category.category_group_id, "")
                if category.deleted or category.hidden or category.name in EXCLUDED_CATEGORIES:
                    continue
                if group_name in EXCLUDED_GROUPS:
                    continue

                rows.append(
                    {
                        # YNAB represents each full-month rollup by the 1st of that month (YYYY-MM-01)
                        "month": month_detail.month.isoformat(),
                        "month_income": milliunits_to_currency(month_detail.income),
                        "month_budgeted": milliunits_to_currency(month_detail.budgeted),
                        "month_activity": milliunits_to_currency(month_detail.activity),
                        "month_to_be_budgeted": milliunits_to_currency(month_detail.to_be_budgeted),
                        "category_group": group_name,
                        "category_name": category.name,
                        "hidden": category.hidden,
                        "budgeted": milliunits_to_currency(category.budgeted),
                        "activity": milliunits_to_currency(category.activity),
                        "balance": milliunits_to_currency(category.balance),
                        "goal_type": category.goal_type or "",
                        "goal_cadence": category.goal_cadence,
                        "goal_target": milliunits_to_currency(category.goal_target),
                        "goal_under_funded": milliunits_to_currency(category.goal_under_funded),
                        "goal_overall_funded": milliunits_to_currency(category.goal_overall_funded),
                        "goal_overall_left": milliunits_to_currency(category.goal_overall_left),
                    }
                )

    write_csv(HISTORY_FILENAME, rows)
    return rows

def group_by_category(history_rows):
    rows_by_category = {}
    for row in history_rows:
        category_key = (row["category_group"], row["category_name"])
        if category_key not in rows_by_category:
            rows_by_category[category_key] = []
        rows_by_category[category_key].append(row)
    return rows_by_category

def summarize_categories(rows_by_category):
    category_summaries = []
    for category_key in sorted(rows_by_category.keys()):
        category_group, category_name = category_key
        category_rows = rows_by_category[category_key]
        months_count = len(category_rows)

        # Use the most recent month in the export for the category's current goal settings
        latest = category_rows[-1]
        goal_cadence = latest["goal_cadence"]
        goal_target = latest["goal_target"]
        monthly_target = get_monthly_target(goal_target, goal_cadence)

        total_spent = 0.0
        total_budgeted = 0.0
        overfunded_months = 0
        max_overfunded = 0.0
        budgeted_spikes = 0
        for row in category_rows:
            # YNAB stores spending as negative activity (and refunds as positive), so flip the sign
            total_spent += (row["activity"] * -1)
            total_budgeted += row["budgeted"]

            if row["goal_overall_funded"] is not None and row["goal_target"] is not None:
                overfunded = round((row["goal_overall_funded"] - row["goal_target"]), 2)
                if overfunded > 0:
                    overfunded_months += 1
                    if overfunded > max_overfunded:
                        max_overfunded = overfunded

            if monthly_target and row["budgeted"] > (SPIKE_THRESHOLD * monthly_target):
                budgeted_spikes += 1

        avg_monthly_spent = round((total_spent / months_count), 2)
        avg_monthly_budgeted = round((total_budgeted / months_count), 2)

        recent_month_rows = category_rows[-RECENT_MONTHS:]
        recent_months_spent = sum((row["activity"] * -1) for row in recent_month_rows)
        avg_recent_months_spent = round((recent_months_spent / len(recent_month_rows)), 2)

        trend_vs_long_pct = None
        if avg_monthly_spent > 0:
            trend_vs_long_pct = round(((avg_recent_months_spent - avg_monthly_spent) / avg_monthly_spent) * 100, 1)

        pct_vs_target = None
        if monthly_target:
            pct_vs_target = round(((avg_monthly_spent - monthly_target) / monthly_target) * 100, 1)

        category_summaries.append(
            {
                "category_group": category_group,
                "category_name": category_name,
                "hidden": latest["hidden"],
                "months_count": months_count,
                "goal_type": latest["goal_type"],
                "goal_cadence": goal_cadence,
                "goal_target": goal_target,
                "monthly_target": monthly_target,
                "avg_monthly_budgeted": avg_monthly_budgeted,
                "avg_monthly_spent": avg_monthly_spent,
                f"avg_recent_{RECENT_MONTHS}m_spent": avg_recent_months_spent,
                f"trend_vs_{MONTHS_TO_FETCH}m_pct": trend_vs_long_pct,
                "pct_vs_target": pct_vs_target,
                "overfunded_months": overfunded_months,
                "max_overfunded": max_overfunded,
                "budgeted_spikes": budgeted_spikes,
            }
        )

    write_csv(SUMMARY_FILENAME, category_summaries)
    return category_summaries

def plot_category_summary(category_summaries):
    # Keep only categories with a monthly or annual target
    targeted_categories = [row for row in category_summaries if row["pct_vs_target"] is not None]

    # Sort ascending so the biggest overspenders appear at the top of the horizontal bar chart
    targeted_categories.sort(key=lambda row: row["pct_vs_target"])

    category_names = []
    pct_values = []
    bar_colors = []
    hover_texts = []
    for row in targeted_categories:
        pct = row["pct_vs_target"]
        category_names.append(row["category_name"])
        pct_values.append(pct)
        if pct > TARGET_BAND_PCT:
            bar_colors.append("indianred")
        elif pct < -TARGET_BAND_PCT:
            bar_colors.append("skyblue")
        else:
            bar_colors.append("mediumseagreen")

        hover_texts.append(
            f"<b>{row['category_name']}</b> ({row['category_group']})<br>"
            f"Monthly Target: ${row['monthly_target']:.2f}<br>"
            f"Avg Monthly Spent ({MONTHS_TO_FETCH}m): ${row['avg_monthly_spent']:.2f}<br>"
            f"Last {RECENT_MONTHS}m Avg Spent: ${row[f'avg_recent_{RECENT_MONTHS}m_spent']:.2f}<br>"
            f"% vs. Overall {MONTHS_TO_FETCH}m Target: {pct:+.1f}%<br>"
            f"Budgeted Spikes: {row['budgeted_spikes']}<br>"
            f"Overfunded Months: {row['overfunded_months']}"
        )

    fig = go.Figure(
        go.Bar(
            x=pct_values,
            y=category_names,
            orientation="h",
            marker_color=bar_colors,
            hovertext=hover_texts,
            hoverinfo="text",
        )
    )

    for x in (-TARGET_BAND_PCT, TARGET_BAND_PCT):
        fig.add_vline(x, line_color="green", line_dash="dash", line_width=1, annotation_text=f"{x}%", annotation_position="bottom", annotation_font_color="green")
    fig.add_vline(x=0, line_color="black", line_width=1)

    fig.update_layout(
        title={
            "text": f"{MONTHS_TO_FETCH}-Month Average Spend vs. Goal Target (%)",
            "x": 0.5,
            "xref": "paper",
            "xanchor": "center",
        },
        xaxis_title="% Difference vs. Monthly Target",
        height=max(600, (len(targeted_categories) * 24)),
        template="plotly_white",
        showlegend=False,
    )

    OUTPUT_FOLDER.mkdir(exist_ok=True)
    chart_path = OUTPUT_FOLDER.joinpath(CHART_FILENAME)
    fig.write_html(chart_path)

def backtest_last_3_months(rows_by_category):
    # Test the projection formula using earlier months to predict the last 3 completed months
    results, _ = compare_3m_projections(rows_by_category)

    results.sort(key=lambda row: abs(row["miss_model"]), reverse=True)
    write_csv(BACKTEST_FILENAME, results)

    if results:
        avg_miss_long, avg_miss_recent, avg_miss_model, median_miss_model = avg_category_misses(results)
        print(
            f"Backtest avg miss/category -> Long Avg: ${avg_miss_long:.2f} | "
            f"{RECENT_MONTHS}m Avg: ${avg_miss_recent:.2f} | Model: ${avg_miss_model:.2f} (Median: ${median_miss_model:.2f})"
        )

def forecast_next_3_months(rows_by_category):
    # Apply the same projection formula to all 18 months to forecast the next 3 months
    run_date = datetime.date.today().isoformat()

    total_monthly = 0.0
    total_3m = 0.0
    forecasts = []
    for (category_group, category_name), category_rows in rows_by_category.items():
        monthly_spend = project_monthly_spend(category_rows)
        if monthly_spend == 0:
            continue
        total_monthly += monthly_spend
        projected_3m = round((monthly_spend * 3), 2)
        total_3m += projected_3m
        forecasts.append(
            {
                "run_date": run_date,
                "category_group": category_group,
                "category_name": category_name,
                "projected_monthly": monthly_spend,
                "projected_next_3m": projected_3m,
            }
        )

    forecasts.sort(key=lambda row: row["projected_next_3m"], reverse=True)
    write_csv(FORECAST_FILENAME, forecasts)

    print(f"Forecast total spend (next 3m): ${total_3m:.2f} (${total_monthly:.2f}/mo)")
    return {
        "run_date": run_date,
        "projected_monthly": round(total_monthly, 2),
        "projected_next_3m": round(total_3m, 2),
    }

def walk_forward_validate_3m(rows_by_category):
    # Test the projection formula across multiple 3-month periods (9m, 12m, and 15m training)
    periods_tested = 0
    total_long = 0.0
    total_recent = 0.0
    total_model = 0.0

    for train_months in range(9, (MONTHS_TO_FETCH - 2), 3):
        results, test_window_dates = compare_3m_projections(rows_by_category, train_months=train_months)
        if not results:
            continue

        avg_miss_long, avg_miss_recent, avg_miss_model, median_miss_model = avg_category_misses(results)
        periods_tested += 1
        total_long += avg_miss_long
        total_recent += avg_miss_recent
        total_model += avg_miss_model
        print(
            f"Walk-forward {test_window_dates} ({train_months:>2}m train) -> "
            f"Long: ${avg_miss_long:.2f} | {RECENT_MONTHS}m: ${avg_miss_recent:.2f} | Model: ${avg_miss_model:.2f} (Median: ${median_miss_model:.2f})"
        )

    if periods_tested:
        overall_long = total_long / periods_tested
        overall_recent = total_recent / periods_tested
        overall_model = total_model / periods_tested
        print(
            f"Walk-forward overall avg           -> "
            f"Long: ${overall_long:.2f} | {RECENT_MONTHS}m: ${overall_recent:.2f} | Model: ${overall_model:.2f}"
        )

def monte_carlo_forecast(rows_by_category):
    total_summary, category_risks = simulate_3m_spend(rows_by_category)

    write_csv(SIMULATION_FILENAME, category_risks)
    print(
        f"Monte Carlo 3m spend ({total_summary['runs']} runs) -> "
        f"P10: ${total_summary['p10']:.2f} | P50: ${total_summary['p50']:.2f} | P90: ${total_summary['p90']:.2f}"
    )
    return total_summary

def get_latest_month_spend(history_rows):
    """Return the latest closed month (YYYY-MM) and its total spending from history_rows."""
    monthly_totals = {}
    for row in history_rows:
        month_key = row["month"][:7]
        monthly_totals[month_key] = monthly_totals.get(month_key, 0.0) + (row["activity"] * -1)
    # YYYY-MM strings sort chronologically, so max() returns the latest closed month
    last_history_month = max(monthly_totals.keys())
    actual_monthly_spend = round(monthly_totals[last_history_month], 2)
    return last_history_month, actual_monthly_spend

def save_forecast_snapshot(forecast_summary, monte_carlo_summary, last_history_month, actual_monthly_spend):
    """Upsert one summary snapshot row per completed history month for future forecast audits."""
    path = OUTPUT_FOLDER.joinpath(SNAPSHOTS_FILENAME)

    snapshot = {
        "last_history_month": last_history_month,
        "run_date": forecast_summary["run_date"],
        "actual_monthly_spend": actual_monthly_spend,
        "projected_monthly": forecast_summary["projected_monthly"],
        "projected_next_3m": forecast_summary["projected_next_3m"],
        "monte_carlo_runs": monte_carlo_summary["runs"],
        "monte_carlo_p10_3m": monte_carlo_summary["p10"],
        "monte_carlo_p50_3m": monte_carlo_summary["p50"],
        "monte_carlo_p90_3m": monte_carlo_summary["p90"],
        "monte_carlo_mean_3m": monte_carlo_summary["mean"],
    }

    snapshots_by_month = {}
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as csv_file:
            for existing_row in csv.DictReader(csv_file):
                if existing_row.get("last_history_month"):
                    snapshots_by_month[existing_row["last_history_month"]] = existing_row

    snapshots_by_month[last_history_month] = snapshot
    ordered_snapshots = [snapshots_by_month[month] for month in sorted(snapshots_by_month)]
    write_csv(SNAPSHOTS_FILENAME, ordered_snapshots)
    print(f"Saved forecast snapshot ({last_history_month}) -> {SNAPSHOTS_FILENAME} ({len(ordered_snapshots)} total)")
    return ordered_snapshots

def audit_forecast_snapshots(ordered_snapshots):
    """Score each closed month against up to 3 prior snapshots and print latest + overall results."""
    months_evaluated = len(ordered_snapshots) - 1
    if months_evaluated < 1:
        print("Snapshot audit -> waiting for next closed month to compare (1 snapshot saved)")
        return None

    # Running totals accumulated across every evaluated month in ordered_snapshots
    cumulative_comparisons = 0
    model_wins = 0
    total_model_miss = 0.0
    total_monte_carlo_miss = 0.0
    total_model_bias = 0.0
    latest_month_comparisons = []

    for month_index in range(1, len(ordered_snapshots)):
        current_snapshot = ordered_snapshots[month_index]
        actual_monthly_spend = float(current_snapshot["actual_monthly_spend"])

        # Each snapshot forecasts the next 3 months, so only up to 3 prior rows cover this month
        for prior_snapshot in ordered_snapshots[:month_index][-3:]:
            model_miss = round(actual_monthly_spend - float(prior_snapshot["projected_monthly"]), 2)
            # Divide 3-month Monte Carlo median by 3 to compare on a monthly scale
            monte_carlo_miss = round(actual_monthly_spend - (float(prior_snapshot["monte_carlo_p50_3m"]) / 3.0), 2)

            cumulative_comparisons += 1
            total_model_miss += abs(model_miss)
            total_monte_carlo_miss += abs(monte_carlo_miss)
            total_model_bias += model_miss
            if abs(model_miss) < abs(monte_carlo_miss):
                model_wins += 1

            if month_index == months_evaluated:
                latest_month_comparisons.append(
                    f"{prior_snapshot['last_history_month']} (${model_miss:+.2f} vs Monte Carlo ${monte_carlo_miss:+.2f})"
                )

    latest_snapshot = ordered_snapshots[-1]
    print(
        f"Snapshot audit ({latest_snapshot['last_history_month']} actual ${float(latest_snapshot['actual_monthly_spend']):.2f}) -> "
        f"vs prior: {', '.join(latest_month_comparisons)}"
    )

    avg_model_miss = round(total_model_miss / cumulative_comparisons, 2)
    avg_monte_carlo_miss = round(total_monte_carlo_miss / cumulative_comparisons, 2)
    avg_model_bias = round(total_model_bias / cumulative_comparisons, 2)
    print(
        f"Snapshot audit overall ({cumulative_comparisons} total comparisons across {months_evaluated} months) -> "
        f"Avg miss Model: ${avg_model_miss:.2f}/mo | Monte Carlo P50: ${avg_monte_carlo_miss:.2f}/mo | "
        f"Bias: ${avg_model_bias:+.2f}/mo | Model wins: {model_wins}/{cumulative_comparisons}"
    )
    return {
        "months_evaluated": months_evaluated,
        "total_comparisons": cumulative_comparisons,
        "avg_model_miss": avg_model_miss,
        "avg_monte_carlo_miss": avg_monte_carlo_miss,
        "avg_model_bias": avg_model_bias,
        "model_wins": model_wins,
    }

if __name__ == "__main__":
    history_rows = export_budget_history()
    if not history_rows:
        print("No completed budget history rows found.")
        raise SystemExit
    rows_by_category = group_by_category(history_rows)
    category_summaries = summarize_categories(rows_by_category)
    plot_category_summary(category_summaries)
    backtest_last_3_months(rows_by_category)
    forecast_summary = forecast_next_3_months(rows_by_category)
    walk_forward_validate_3m(rows_by_category)
    monte_carlo_summary = monte_carlo_forecast(rows_by_category)
    last_history_month, actual_monthly_spend = get_latest_month_spend(history_rows)
    ordered_snapshots = save_forecast_snapshot(
        forecast_summary, monte_carlo_summary, last_history_month, actual_monthly_spend
    )
    audit_forecast_snapshots(ordered_snapshots)