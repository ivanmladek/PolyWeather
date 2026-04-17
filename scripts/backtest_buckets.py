"""
backtest_buckets.py — Replay probability snapshots against settlement outcomes.

For each city-day, traces how the model's top bucket prediction evolved
throughout the day and identifies the optimal entry window (hours before peak)
where the model was most accurate at picking the correct settlement bucket.

Outputs:
  - Per city-day: warmup curve (mu, top_bucket, max_so_far, peak_status over time)
  - Accuracy by hours-before-peak: at each time window, what % of top-bucket picks matched settlement
  - Simulated P&L: if you bought YES on the model's top bucket at each time window
"""

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "polyweather.db"

# UTC peak hours per city (same as scan_alpha.py)
CITY_UTC_PEAK = {
    "ankara": 13, "istanbul": 12, "moscow": 10, "london": 14, "paris": 15,
    "seoul": 4, "busan": 5, "hong kong": 6, "taipei": 5, "shanghai": 6,
    "singapore": 7, "kuala lumpur": 7, "jakarta": 6, "tokyo": 6,
    "tel aviv": 12, "toronto": 18, "buenos aires": 17, "wellington": 2,
    "new york": 19, "los angeles": 20, "san francisco": 20, "denver": 19,
    "austin": 22, "houston": 21, "mexico city": 21, "chicago": 22,
    "dallas": 23, "miami": 17, "atlanta": 21, "seattle": 23,
    "lucknow": 9, "sao paulo": 18, "munich": 14, "milan": 15,
    "warsaw": 13, "helsinki": 12, "amsterdam": 14, "madrid": 16,
    "chengdu": 10, "chongqing": 10, "shenzhen": 6, "beijing": 9,
    "wuhan": 8, "panama city": 19, "lagos": 14, "cape town": 12,
    "jeddah": 11, "lau fau shan": 6,
}


def wu_round(temp_c):
    """Weather Underground settlement rounding: round to nearest integer."""
    if temp_c is None:
        return None
    return round(temp_c)


def load_data():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    # 1. Load all settled daily records
    settled = {}
    rows = db.execute("""
        SELECT city, target_date, actual_high, deb_prediction, mu, payload_json
        FROM daily_records_store
        WHERE actual_high IS NOT NULL
        ORDER BY city, target_date
    """).fetchall()
    for r in rows:
        key = (r["city"], r["target_date"])
        payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        settled[key] = {
            "actual_high": r["actual_high"],
            "settlement_bucket": wu_round(r["actual_high"]),
            "deb_prediction": r["deb_prediction"],
            "mu": r["mu"],
            "forecasts": payload.get("forecasts", {}),
        }

    # 2. Load all probability snapshots (deduplicated to ~1 per 20-min slot)
    snapshots = defaultdict(list)
    rows = db.execute("""
        SELECT city, target_date, timestamp, raw_mu, raw_sigma,
               max_so_far, peak_status, legacy_top_bucket, payload_json
        FROM probability_training_snapshots_store
        ORDER BY city, target_date, id
    """).fetchall()

    # Deduplicate: keep first snapshot per (city, date, timestamp)
    seen = set()
    for r in rows:
        key = (r["city"], r["target_date"], r["timestamp"])
        if key in seen:
            continue
        seen.add(key)

        payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        snapshots[(r["city"], r["target_date"])].append({
            "timestamp": r["timestamp"],
            "raw_mu": r["raw_mu"],
            "raw_sigma": r["raw_sigma"],
            "max_so_far": r["max_so_far"],
            "peak_status": r["peak_status"],
            "top_bucket": r["legacy_top_bucket"],
            "prob_snapshot": payload.get("prob_snapshot", []),
        })

    db.close()
    return settled, snapshots


def hours_before_peak(timestamp_str, city):
    """Compute hours from snapshot time to city's typical UTC peak."""
    peak_h = CITY_UTC_PEAK.get(city.lower())
    if peak_h is None:
        return None
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        snap_h = ts.hour + ts.minute / 60.0
        diff = peak_h - snap_h
        if diff < -12:
            diff += 24
        return diff
    except Exception:
        return None


def top_bucket_prob(prob_snapshot, bucket_val):
    """Get the probability assigned to a specific bucket value."""
    for entry in prob_snapshot:
        v = entry.get("v") or entry.get("value")
        p = entry.get("p") or entry.get("probability", 0)
        if v == bucket_val:
            return p
    return 0.0


def run_backtest():
    settled, snapshots = load_data()

    print(f"Loaded {len(settled)} settled city-days")
    print(f"Loaded {sum(len(v) for v in snapshots.values())} deduped snapshots across {len(snapshots)} city-days\n")

    # For each time window (hours before peak), track:
    # - how many times the top_bucket matched the settlement bucket
    # - simulated P&L assuming we buy YES at various hypothetical market prices
    TIME_BINS = [
        (12, 10),   # 10-12h before peak (overnight / early morning)
        (10, 8),    # 8-10h
        (8, 6),     # 6-8h (pre-dawn / dawn)
        (6, 5),     # 5-6h (morning warmup starts)
        (5, 4),     # 4-5h (warmup accelerating)
        (4, 3),     # 3-4h (IDEAL ENTRY WINDOW?)
        (3, 2),     # 2-3h (approaching peak)
        (2, 1),     # 1-2h (near peak)
        (1, 0),     # 0-1h (at peak)
        (0, -2),    # past peak
    ]

    bin_stats = {f"{hi}-{lo}h": {"total": 0, "hit": 0, "sigma_sum": 0, "mae_sum": 0}
                 for hi, lo in TIME_BINS}

    city_results = []

    for (city, date), settlement in settled.items():
        snaps = snapshots.get((city, date), [])
        if not snaps:
            continue

        actual_bucket = settlement["settlement_bucket"]
        if actual_bucket is None:
            continue

        # Trace this city-day
        day_trace = []
        for s in snaps:
            hbp = hours_before_peak(s["timestamp"], city)
            if hbp is None:
                continue

            predicted_bucket = s["top_bucket"]
            hit = (predicted_bucket == actual_bucket)
            prob_for_top = 0.0
            for entry in s.get("prob_snapshot", []):
                v = entry.get("v") or entry.get("value")
                p = entry.get("p") or entry.get("probability", 0)
                if v == predicted_bucket:
                    prob_for_top = p
                    break

            day_trace.append({
                "timestamp": s["timestamp"],
                "hours_before_peak": hbp,
                "mu": s["raw_mu"],
                "sigma": s["raw_sigma"],
                "max_so_far": s["max_so_far"],
                "peak_status": s["peak_status"],
                "predicted_bucket": predicted_bucket,
                "prob_for_top": prob_for_top,
                "actual_bucket": actual_bucket,
                "hit": hit,
            })

            # Assign to time bin
            for hi, lo in TIME_BINS:
                if lo <= hbp < hi:
                    bk = f"{hi}-{lo}h"
                    bin_stats[bk]["total"] += 1
                    if hit:
                        bin_stats[bk]["hit"] += 1
                    bin_stats[bk]["sigma_sum"] += (s["raw_sigma"] or 0)
                    bin_stats[bk]["mae_sum"] += abs((s["raw_mu"] or 0) - settlement["actual_high"])
                    break

        if day_trace:
            city_results.append({
                "city": city,
                "date": date,
                "actual_bucket": actual_bucket,
                "actual_high": settlement["actual_high"],
                "deb_prediction": settlement["deb_prediction"],
                "n_snapshots": len(day_trace),
                "trace": day_trace,
            })

    # === OUTPUT ===

    print("=" * 100)
    print("BUCKET ACCURACY BY HOURS BEFORE PEAK")
    print("=" * 100)
    print(f"{'Window':<12} {'Snapshots':>10} {'Hits':>8} {'Accuracy':>10} {'Avg σ':>8} {'Avg MAE':>8} {'Would-win':>12}")
    print("-" * 100)

    for hi, lo in TIME_BINS:
        bk = f"{hi}-{lo}h"
        s = bin_stats[bk]
        n = s["total"]
        if n == 0:
            continue
        acc = s["hit"] / n
        avg_sigma = s["sigma_sum"] / n
        avg_mae = s["mae_sum"] / n
        # Simulated return: if top_bucket is correct, YES pays $1. We buy at (1 - model_prob).
        # Approx: if accuracy = 40%, and we pay 60c avg, we lose. Need accuracy > buy_price.
        # Assume market prices YES at roughly (1 - accuracy) when uninformed.
        # Edge = accuracy - market_price. If we assume market = 50% for simplicity:
        hypothetical_roi = (acc - 0.5) / 0.5 * 100  # % return vs 50c market
        print(f"  {bk:<10} {n:>10} {s['hit']:>8} {acc:>9.1%} {avg_sigma:>8.2f} {avg_mae:>8.2f}°C {hypothetical_roi:>+10.1f}%")

    # Per-city breakdown for the latest date
    print(f"\n{'='*100}")
    print("PER-CITY RESULTS (latest 2 dates)")
    print(f"{'='*100}")
    print(f"{'City':<16} {'Date':<12} {'DEB':>6} {'Actual':>8} {'Settle':>8} {'Top@3-4h':>10} {'Hit@3-4h':>10} {'Top@peak':>10} {'Hit@peak':>10}")
    print("-" * 100)

    for cr in sorted(city_results, key=lambda x: (x["date"], x["city"])):
        # Find the snapshot at 3-4h before peak
        snap_3_4 = None
        snap_peak = None
        for t in cr["trace"]:
            if 3 <= t["hours_before_peak"] < 4:
                snap_3_4 = t
            if 0 <= t["hours_before_peak"] < 1:
                snap_peak = t

        top_34 = snap_3_4["predicted_bucket"] if snap_3_4 else "?"
        hit_34 = "HIT" if snap_3_4 and snap_3_4["hit"] else "MISS" if snap_3_4 else "?"
        top_pk = snap_peak["predicted_bucket"] if snap_peak else "?"
        hit_pk = "HIT" if snap_peak and snap_peak["hit"] else "MISS" if snap_peak else "?"

        print(f"  {cr['city']:<14} {cr['date']:<12} {cr['deb_prediction']:>6.1f} {cr['actual_high']:>8.1f} {cr['actual_bucket']:>8} {str(top_34):>10} {hit_34:>10} {str(top_pk):>10} {hit_pk:>10}")

    # Summary stats
    total_city_days = len(city_results)
    hits_at_peak = sum(1 for cr in city_results
                       for t in cr["trace"]
                       if 0 <= t["hours_before_peak"] < 1 and t["hit"])
    n_at_peak = sum(1 for cr in city_results
                    for t in cr["trace"]
                    if 0 <= t["hours_before_peak"] < 1)

    hits_at_34 = sum(1 for cr in city_results
                     for t in cr["trace"]
                     if 3 <= t["hours_before_peak"] < 4 and t["hit"])
    n_at_34 = sum(1 for cr in city_results
                  for t in cr["trace"]
                  if 3 <= t["hours_before_peak"] < 4)

    print(f"\n{'='*100}")
    print("SUMMARY")
    print(f"{'='*100}")
    print(f"  City-days analyzed: {total_city_days}")
    print(f"  Bucket accuracy at 3-4h before peak: {hits_at_34}/{n_at_34} = {hits_at_34/n_at_34:.1%}" if n_at_34 else "  No data at 3-4h window")
    print(f"  Bucket accuracy at peak (0-1h):      {hits_at_peak}/{n_at_peak} = {hits_at_peak/n_at_peak:.1%}" if n_at_peak else "  No data at peak window")

    # Optimal entry analysis
    print(f"\n{'='*100}")
    print("OPTIMAL ENTRY TIMING ANALYSIS")
    print(f"{'='*100}")
    print("""
The key question: WHEN during the day should you enter a position?

If you buy YES on the model's top bucket:
- Too early (>6h before peak): model sigma is wide, top bucket changes frequently
- Sweet spot (3-5h before peak): warming trend is visible in METAR, model has
  incorporated morning observations, sigma is tightening
- Near peak (0-2h): most accurate but market has likely priced in the outcome
- Past peak: settlement is almost certain but no edge left

For a profitable strategy:
1. ENTER at 3-5 hours before peak when model confidence is rising
2. The model's top bucket should be STABLE (same for 2+ consecutive snapshots)
3. Sigma should be < 1.5 (tightening uncertainty)
4. Max_so_far should be > 60% of the predicted bucket value (warming is on track)
""")

    # Find stable-bucket windows
    print(f"\n{'='*100}")
    print("STABLE BUCKET ANALYSIS")
    print(f"{'='*100}")
    print("Cities where the top bucket was STABLE (unchanged) from 5h to 2h before peak:\n")

    stable_hit = 0
    stable_total = 0
    unstable_hit = 0
    unstable_total = 0

    for cr in city_results:
        buckets_in_window = []
        for t in cr["trace"]:
            if 2 <= t["hours_before_peak"] <= 5:
                buckets_in_window.append(t["predicted_bucket"])

        if len(buckets_in_window) < 3:
            continue

        is_stable = len(set(buckets_in_window)) == 1
        final_bucket = buckets_in_window[-1]
        hit = (final_bucket == cr["actual_bucket"])

        if is_stable:
            stable_total += 1
            if hit:
                stable_hit += 1
        else:
            unstable_total += 1
            if hit:
                unstable_hit += 1

    if stable_total:
        print(f"  Stable bucket (2-5h window): {stable_hit}/{stable_total} = {stable_hit/stable_total:.1%} accuracy")
    if unstable_total:
        print(f"  Unstable bucket (changed):   {unstable_hit}/{unstable_total} = {unstable_hit/unstable_total:.1%} accuracy")
    print(f"\n  Takeaway: {'Stable buckets are MORE accurate — filter for bucket stability before entering' if stable_total and stable_hit/max(stable_total,1) > unstable_hit/max(unstable_total,1) else 'Insufficient data to determine stability edge'}")


if __name__ == "__main__":
    run_backtest()
