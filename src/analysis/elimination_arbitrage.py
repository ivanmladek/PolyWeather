"""
HF Elimination Arbitrage Detector
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Temperature is monotonic. Once HF data shows the observed daily max has
crossed into a higher bucket, every lower bucket is mathematically
eliminated from settling YES — it cannot retroactively become the daily
max. This is a pure latency arbitrage: the Polymarket NO side for those
dead buckets is still priced at 90-97c because the market is waiting for
the next hourly METAR.

We detect elimination at 5-minute cadence (weather.gov) or 1-minute
cadence (ASOS 1-min when available), typically 10-50 minutes before the
next METAR.

See docs/HF_ELIMINATION_ARBITRAGE.md for the full strategy.
"""

from __future__ import annotations

import json
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from src.analysis.settlement_rounding import apply_city_settlement


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum safety margin above the bucket upper bound to call it eliminated.
# Protects against sensor-rounding edge cases and NOAA QC revisions.
MIN_MARGIN_F = float(os.getenv("ELIM_MIN_MARGIN_F", "0.3"))
MIN_MARGIN_C = float(os.getenv("ELIM_MIN_MARGIN_C", "0.2"))

# Minimum edge (1 - no_price) required to fire a trade, as fraction.
# Below this the Polymarket fees + slippage eat the profit.
MIN_EDGE_FRACTION = float(os.getenv("ELIM_MIN_EDGE_FRACTION", "0.015"))  # 1.5%

# Only consider markets where NO is priced strictly below this (above it
# there is essentially no edge anyway).
MAX_NO_PRICE = float(os.getenv("ELIM_MAX_NO_PRICE", "0.98"))

# Minimum NO-side liquidity to avoid walking the book on a thin market.
# Weather markets often have $200-500 per bucket; keep this low to catch trades.
MIN_NO_LIQUIDITY_USD = float(os.getenv("ELIM_MIN_NO_LIQUIDITY_USD", "100"))

# Minimum number of HF observations required before we trust the HF max.
# One outlier reading shouldn't trigger a trade.
MIN_HF_OBSERVATIONS = int(os.getenv("ELIM_MIN_HF_OBSERVATIONS", "3"))

# Maximum median gap between HF observations. If the station is reporting
# erratically (> 15 min between readings) we don't trust it.
MAX_HF_MEDIAN_GAP_MIN = float(os.getenv("ELIM_MAX_HF_MEDIAN_GAP_MIN", "15"))


# ---------------------------------------------------------------------------
# Prior-scan state (for newly_eliminated_this_tick detection)
# ---------------------------------------------------------------------------

# Key: (city_normalized, date_str) -> set of already-eliminated bucket labels
# This is keyed on the scan-side so that when we re-run analysis on the same
# day the diff is correct. Cleared automatically when the date key rolls over.
_prior_elim_state: Dict[Tuple[str, str], set] = {}
_prior_elim_lock = threading.Lock()


def _prior_state_key(city: str, target_date: str) -> Tuple[str, str]:
    return (str(city or "").lower().strip(), str(target_date or "").strip())


def _get_prior_eliminated(city: str, target_date: str) -> set:
    with _prior_elim_lock:
        return set(_prior_elim_state.get(_prior_state_key(city, target_date), set()))


def _update_prior_eliminated(city: str, target_date: str, labels: List[str]) -> None:
    with _prior_elim_lock:
        key = _prior_state_key(city, target_date)
        existing = _prior_elim_state.get(key, set())
        existing.update(labels)
        _prior_elim_state[key] = existing


def reset_prior_state() -> None:
    """Testing helper — clear the in-memory prior state."""
    with _prior_elim_lock:
        _prior_elim_state.clear()


# ---------------------------------------------------------------------------
# Bucket geometry
# ---------------------------------------------------------------------------

def bucket_upper_bound(
    city: str, bucket_temp: float, direction: str
) -> Optional[float]:
    """Return the maximum temperature that still settles INTO `bucket_temp`.

    For an exact bucket with wu_round (half-up), the upper bound is
    bucket_temp + 0.499. For floor-rounded cities (HKO), the upper bound is
    bucket_temp + 0.999.

    For "77F+" (above), the bucket has no elimination logic here (always live
    once reached). For "<=77F" (below), the upper bound is the bucket temp.

    Uses apply_city_settlement to probe the actual boundary by walking UP
    from bucket_temp and finding the highest value that still settles into
    bucket_temp. This handles wu_round (most cities), floor (HKO), and any
    other city-specific rule.
    """
    if direction == "above":
        # "77F+" is eliminated only if max < 77F (temp never reaches threshold).
        # We don't handle this here; elimination is for below-bucket only.
        return None
    if direction == "below":
        # "<=77F" is eliminated if max > 77F.
        return float(bucket_temp)
    # exact bucket: probe for the actual upper boundary by walking UP from
    # bucket_temp and finding the highest value that still settles into
    # bucket_temp. Step 0.01°F precision, up to +0.999 (covers floor rounding).
    target_bucket = int(round(bucket_temp))
    try:
        best_upper = None
        for delta_cents in range(0, 100):
            cand = float(bucket_temp) + delta_cents / 100.0
            rounded = apply_city_settlement(city, cand)
            if rounded == target_bucket:
                best_upper = cand
            elif best_upper is not None:
                # Once we leave the target bucket on the way up, we're done.
                break
        if best_upper is not None:
            return best_upper
    except Exception:
        pass
    # Last-resort fallback: assume wu_round half-up
    return float(bucket_temp) + 0.499


def is_bucket_eliminated_by_hf(
    city: str,
    bucket_info: Dict[str, Any],
    hf_max: float,
    use_fahrenheit: bool,
) -> Tuple[bool, Optional[float]]:
    """Return (is_eliminated, bucket_upper).

    A bucket is eliminated if `hf_max > bucket_upper + safety_margin`.
    """
    direction = str(bucket_info.get("direction") or "exact")
    bucket_temp = bucket_info.get("temp") or bucket_info.get("value")
    if bucket_temp is None:
        return False, None

    upper = bucket_upper_bound(city, float(bucket_temp), direction)
    if upper is None:
        return False, None

    margin = MIN_MARGIN_F if use_fahrenheit else MIN_MARGIN_C
    eliminated = float(hf_max) > (upper + margin)
    return eliminated, upper


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------

def analyze_elimination(
    *,
    city: str,
    target_date: str,
    hf_max: Optional[float],
    hf_max_time: Optional[str],
    hf_source_kind: Optional[str],
    hf_icao: Optional[str],
    hf_observation_count: int,
    hf_median_gap_min: Optional[float],
    all_buckets: List[Dict[str, Any]],
    use_fahrenheit: bool,
) -> Optional[Dict[str, Any]]:
    """Compute `elimination_analysis` block for a city.

    Returns None when no HF data is available or the feed is too sparse/stale.
    Returns a dict with eliminated_buckets, live_buckets, newly_eliminated_this_tick.
    """
    if hf_max is None:
        return None
    if not all_buckets:
        return None
    if hf_observation_count is not None and hf_observation_count < MIN_HF_OBSERVATIONS:
        logger.debug(
            f"elim: {city} skipped — only {hf_observation_count} HF obs (< {MIN_HF_OBSERVATIONS})"
        )
        return None
    if (
        hf_median_gap_min is not None
        and hf_median_gap_min > MAX_HF_MEDIAN_GAP_MIN
    ):
        logger.debug(
            f"elim: {city} skipped — HF median gap {hf_median_gap_min}min > {MAX_HF_MEDIAN_GAP_MIN}"
        )
        return None

    try:
        hf_bucket_int = apply_city_settlement(city, float(hf_max))
    except Exception:
        hf_bucket_int = None

    eliminated: List[Dict[str, Any]] = []
    live: List[str] = []
    for bucket in all_buckets:
        try:
            is_elim, upper = is_bucket_eliminated_by_hf(
                city, bucket, float(hf_max), use_fahrenheit
            )
        except Exception:
            continue

        label = str(bucket.get("label") or bucket.get("slug") or "?")
        no_buy = bucket.get("no_buy")
        slug = bucket.get("slug")

        if not is_elim:
            live.append(label)
            continue

        # Skip if NO price is missing / too close to 1.0 / too low-liquidity.
        # We still include them in eliminated_buckets for observability, but
        # mark them with no edge / no liquidity so the scanner filter catches them.
        locked_edge_pct = None
        if no_buy is not None:
            try:
                locked_edge_pct = round((1.0 - float(no_buy)) * 100.0, 2)
            except Exception:
                locked_edge_pct = None

        liquidity_val = bucket.get("liquidity")
        tradable = bool(
            no_buy is not None
            and locked_edge_pct is not None
            and locked_edge_pct >= MIN_EDGE_FRACTION * 100.0
            and float(no_buy) < MAX_NO_PRICE
            and (liquidity_val is None or float(liquidity_val) >= MIN_NO_LIQUIDITY_USD)
        )

        eliminated.append({
            "label": label,
            "slug": slug,
            "bucket_temp": bucket.get("temp") or bucket.get("value"),
            "direction": bucket.get("direction") or "exact",
            "bucket_upper": upper,
            "no_price": no_buy,
            "no_sell": bucket.get("no_sell"),
            "yes_buy": bucket.get("yes_buy"),
            "probability": bucket.get("probability"),
            "locked_edge_pct": locked_edge_pct,
            "liquidity": liquidity_val,
            "tradable": tradable,
            "eliminated_at_utc": datetime.now(timezone.utc).isoformat(),
            "eliminated_by_temp": round(float(hf_max), 2),
        })

    # Diff against prior state to compute newly_eliminated_this_tick
    prior_labels = _get_prior_eliminated(city, target_date)
    current_labels = [e["label"] for e in eliminated]
    newly_eliminated = [lbl for lbl in current_labels if lbl not in prior_labels]

    # Update prior state for next scan
    if current_labels:
        _update_prior_eliminated(city, target_date, current_labels)

    return {
        "hf_max": round(float(hf_max), 2),
        "hf_max_time": hf_max_time,
        "hf_bucket": hf_bucket_int,
        "hf_source_kind": hf_source_kind,
        "hf_icao": hf_icao,
        "hf_observation_count": hf_observation_count,
        "hf_median_gap_min": hf_median_gap_min,
        "eliminated_buckets": eliminated,
        "live_buckets": live,
        "newly_eliminated_this_tick": newly_eliminated,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "min_margin_f": MIN_MARGIN_F,
            "min_margin_c": MIN_MARGIN_C,
            "min_edge_fraction": MIN_EDGE_FRACTION,
            "max_no_price": MAX_NO_PRICE,
            "min_no_liquidity_usd": MIN_NO_LIQUIDITY_USD,
        },
    }


# ---------------------------------------------------------------------------
# Persistent log for backtest replay
# ---------------------------------------------------------------------------

def _log_path() -> Path:
    runtime_dir = os.getenv("POLYWEATHER_RUNTIME_DATA_DIR")
    if runtime_dir:
        base = Path(runtime_dir)
    else:
        base = Path(__file__).resolve().parents[2] / "data"
    log_dir = base / "alpha_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "eliminations.jsonl"


def append_elimination_log(
    *,
    city: str,
    target_date: str,
    analysis: Dict[str, Any],
) -> None:
    """Append a JSON line for each newly-eliminated bucket for backtest replay."""
    newly = analysis.get("newly_eliminated_this_tick") or []
    if not newly:
        return
    eliminated_by_label = {e["label"]: e for e in analysis.get("eliminated_buckets") or []}
    records = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for label in newly:
        bucket = eliminated_by_label.get(label)
        if not bucket:
            continue
        records.append({
            "timestamp_utc": now_iso,
            "city": city,
            "target_date": target_date,
            "bucket_label": bucket["label"],
            "bucket_slug": bucket.get("slug"),
            "bucket_temp": bucket.get("bucket_temp"),
            "bucket_upper": bucket.get("bucket_upper"),
            "hf_max": analysis.get("hf_max"),
            "hf_max_time": analysis.get("hf_max_time"),
            "hf_source_kind": analysis.get("hf_source_kind"),
            "no_price": bucket.get("no_price"),
            "locked_edge_pct": bucket.get("locked_edge_pct"),
            "liquidity": bucket.get("liquidity"),
            "tradable": bucket.get("tradable"),
        })
    if not records:
        return
    try:
        path = _log_path()
        with open(path, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug(f"elim: failed to write eliminations log: {exc}")
