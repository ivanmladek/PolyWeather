"""
Tests for HF elimination arbitrage — docs/HF_ELIMINATION_ARBITRAGE.md.

Covers:
- Bucket-upper-bound probing (wu_round, floor, band settlement)
- Elimination detection (with safety margin, direction-aware)
- newly_eliminated_this_tick diff against prior state (per city+date)
- Tradability filter (edge floor, price ceiling, liquidity floor)
- analyze_elimination end-to-end
- Persistent eliminations.jsonl log appender
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.analysis import elimination_arbitrage as elim_mod
from src.analysis.elimination_arbitrage import (
    MIN_MARGIN_F,
    analyze_elimination,
    append_elimination_log,
    bucket_upper_bound,
    is_bucket_eliminated_by_hf,
    reset_prior_state,
)


@pytest.fixture(autouse=True)
def _clear_elim_state():
    """Every test starts with a fresh prior-state map."""
    reset_prior_state()
    yield
    reset_prior_state()


# ---------------------------------------------------------------------------
# Bucket upper bound
# ---------------------------------------------------------------------------

class TestBucketUpperBound:
    def test_wu_round_exact_bucket(self):
        # For 'san francisco' (wu_round half-up), bucket 66 → upper 66.499
        upper = bucket_upper_bound("san francisco", 66.0, "exact")
        assert upper is not None
        # upper should round to 66 (not 67)
        from src.analysis.settlement_rounding import apply_city_settlement
        assert apply_city_settlement("san francisco", upper) == 66

    def test_hko_floor_bucket(self):
        # Hong Kong uses floor: bucket 30 includes [30.0, 30.999]
        upper = bucket_upper_bound("hong kong", 30.0, "exact")
        assert upper is not None
        from src.analysis.settlement_rounding import apply_city_settlement
        assert apply_city_settlement("hong kong", upper) == 30

    def test_below_direction_upper_is_bucket_temp(self):
        # "<=77F" — eliminated if max > 77F
        assert bucket_upper_bound("houston", 77.0, "below") == 77.0

    def test_above_direction_returns_none(self):
        # "77F+" is not eliminated by rising temp (it remains live forever once temp is above)
        assert bucket_upper_bound("houston", 77.0, "above") is None


# ---------------------------------------------------------------------------
# Elimination detection
# ---------------------------------------------------------------------------

class TestEliminationDetection:
    def test_clear_elimination(self):
        """HF max 68.0F should eliminate bucket 66 (wu_round upper 66.499)."""
        bucket = {"label": "66-67°F", "temp": 66.0, "direction": "exact", "no_buy": 0.95}
        is_elim, upper = is_bucket_eliminated_by_hf(
            "san francisco", bucket, hf_max=68.0, use_fahrenheit=True
        )
        assert is_elim is True
        assert upper is not None and upper < 67.0

    def test_not_eliminated_below_margin(self):
        """HF max 66.6F is above 66.499 but only 0.1F; margin (0.3F) not met."""
        bucket = {"label": "66-67°F", "temp": 66.0, "direction": "exact", "no_buy": 0.95}
        is_elim, _ = is_bucket_eliminated_by_hf(
            "san francisco", bucket, hf_max=66.6, use_fahrenheit=True
        )
        assert is_elim is False

    def test_eliminated_at_safety_margin(self):
        """HF max 66.9F exceeds 66.499 + 0.3 = 66.799 — eliminated."""
        bucket = {"label": "66-67°F", "temp": 66.0, "direction": "exact", "no_buy": 0.95}
        is_elim, _ = is_bucket_eliminated_by_hf(
            "san francisco", bucket, hf_max=66.9, use_fahrenheit=True
        )
        assert is_elim is True

    def test_above_bucket_not_eliminated(self):
        """A "77F+" bucket is never marked eliminated by elim-arb (different logic)."""
        bucket = {"label": "77+", "temp": 77.0, "direction": "above", "no_buy": 0.10}
        is_elim, _ = is_bucket_eliminated_by_hf(
            "houston", bucket, hf_max=85.0, use_fahrenheit=True
        )
        assert is_elim is False

    def test_hko_floor_elimination(self):
        """Hong Kong bucket 30 (floor) — HF 31.5C clearly eliminates."""
        bucket = {"label": "30C", "temp": 30.0, "direction": "exact", "no_buy": 0.90}
        is_elim, upper = is_bucket_eliminated_by_hf(
            "hong kong", bucket, hf_max=31.5, use_fahrenheit=False
        )
        assert is_elim is True
        # HKO upper for bucket 30 should be near 30.99
        assert upper is not None and 30.5 < upper < 31.0


# ---------------------------------------------------------------------------
# analyze_elimination end-to-end
# ---------------------------------------------------------------------------

class TestAnalyzeElimination:
    def _sf_buckets(self):
        """Fixture — SF market with 5 buckets like the Apr 19 screenshot."""
        return [
            {
                "label": "64-65°F", "temp": 64.0, "direction": "exact",
                "no_buy": 0.999, "liquidity": 4837,
                "slug": "highest-temperature-in-san-francisco-on-april-19-2026-64-65f",
            },
            {
                "label": "66-67°F", "temp": 66.0, "direction": "exact",
                "no_buy": 0.95, "liquidity": 3680,
                "slug": "highest-temperature-in-san-francisco-on-april-19-2026-66-67f",
            },
            {
                "label": "68-69°F", "temp": 68.0, "direction": "exact",
                "no_buy": 0.21, "liquidity": 5495,
                "slug": "highest-temperature-in-san-francisco-on-april-19-2026-68-69f",
            },
            {
                "label": "70F+", "temp": 70.0, "direction": "above",
                "no_buy": 0.937, "liquidity": 6185,
                "slug": "highest-temperature-in-san-francisco-on-april-19-2026-70f-or-higher",
            },
        ]

    def test_sf_live_example_from_doc(self):
        """The exact scenario from docs/HF_ELIMINATION_ARBITRAGE.md §2."""
        result = analyze_elimination(
            city="san francisco",
            target_date="2026-04-19",
            hf_max=68.0,
            hf_max_time="12:05",
            hf_source_kind="wgov_5min",
            hf_icao="KSFO",
            hf_observation_count=226,
            hf_median_gap_min=5.0,
            all_buckets=self._sf_buckets(),
            use_fahrenheit=True,
        )
        assert result is not None
        assert result["hf_max"] == 68.0
        assert result["hf_bucket"] == 68
        eliminated = result["eliminated_buckets"]
        # 64-65F and 66-67F should be eliminated; 68-69F live; 70F+ live
        eliminated_labels = [e["label"] for e in eliminated]
        assert "64-65°F" in eliminated_labels
        assert "66-67°F" in eliminated_labels
        assert "68-69°F" not in eliminated_labels
        assert "70F+" not in eliminated_labels
        # 68-69F and 70F+ should be in live_buckets
        assert "68-69°F" in result["live_buckets"]
        assert "70F+" in result["live_buckets"]

    def test_tradability_filter(self):
        """66-67F at 95c → tradable. 64-65F at 99.9c → NOT tradable (below edge floor)."""
        result = analyze_elimination(
            city="san francisco",
            target_date="2026-04-19",
            hf_max=68.0,
            hf_max_time="12:05",
            hf_source_kind="wgov_5min",
            hf_icao="KSFO",
            hf_observation_count=226,
            hf_median_gap_min=5.0,
            all_buckets=self._sf_buckets(),
            use_fahrenheit=True,
        )
        eliminated = {e["label"]: e for e in result["eliminated_buckets"]}
        assert eliminated["66-67°F"]["tradable"] is True
        assert eliminated["66-67°F"]["locked_edge_pct"] == 5.0
        assert eliminated["64-65°F"]["tradable"] is False  # below edge floor
        assert eliminated["64-65°F"]["locked_edge_pct"] < 1.5

    def test_insufficient_observations_returns_none(self):
        result = analyze_elimination(
            city="san francisco",
            target_date="2026-04-19",
            hf_max=68.0,
            hf_max_time="12:05",
            hf_source_kind="wgov_5min",
            hf_icao="KSFO",
            hf_observation_count=2,  # below MIN_HF_OBSERVATIONS=3
            hf_median_gap_min=5.0,
            all_buckets=self._sf_buckets(),
            use_fahrenheit=True,
        )
        assert result is None

    def test_stale_feed_returns_none(self):
        """Median gap > 15 min means the station is reporting erratically."""
        result = analyze_elimination(
            city="san francisco",
            target_date="2026-04-19",
            hf_max=68.0,
            hf_max_time="12:05",
            hf_source_kind="awc_metar_speci",
            hf_icao="KSFO",
            hf_observation_count=10,
            hf_median_gap_min=60.0,  # hourly METAR fallback — too stale
            all_buckets=self._sf_buckets(),
            use_fahrenheit=True,
        )
        assert result is None


# ---------------------------------------------------------------------------
# newly_eliminated_this_tick cascade behaviour
# ---------------------------------------------------------------------------

class TestCascadeEliminatedDiff:
    """The cascade — temp rises through multiple boundaries during the day.
    Each analyze_elimination call should only flag NEWLY-crossed buckets."""

    def _nyc_buckets(self):
        return [
            {"label": "52-53F", "temp": 52.0, "direction": "exact", "no_buy": 0.99, "liquidity": 800, "slug": "..-52f"},
            {"label": "54-55F", "temp": 54.0, "direction": "exact", "no_buy": 0.96, "liquidity": 1200, "slug": "..-54f"},
            {"label": "56-57F", "temp": 56.0, "direction": "exact", "no_buy": 0.95, "liquidity": 1500, "slug": "..-56f"},
            {"label": "58-59F", "temp": 58.0, "direction": "exact", "no_buy": 0.92, "liquidity": 1800, "slug": "..-58f"},
            {"label": "60-61F", "temp": 60.0, "direction": "exact", "no_buy": 0.50, "liquidity": 2000, "slug": "..-60f"},
        ]

    def test_cascade_morning_to_noon(self):
        # 08:00 — HF max 55.0F → eliminates 52-53 and 54-55 (fresh)
        r1 = analyze_elimination(
            city="new york", target_date="2026-04-19",
            hf_max=55.0, hf_max_time="08:00",
            hf_source_kind="wgov_5min", hf_icao="KLGA",
            hf_observation_count=100, hf_median_gap_min=5.0,
            all_buckets=self._nyc_buckets(), use_fahrenheit=True,
        )
        assert r1 is not None
        newly_1 = set(r1["newly_eliminated_this_tick"])
        assert "52-53F" in newly_1
        assert "54-55F" in newly_1

        # 10:30 — HF max 57.0F → 56-57F is new; 52-53, 54-55 already dead
        r2 = analyze_elimination(
            city="new york", target_date="2026-04-19",
            hf_max=57.0, hf_max_time="10:30",
            hf_source_kind="wgov_5min", hf_icao="KLGA",
            hf_observation_count=130, hf_median_gap_min=5.0,
            all_buckets=self._nyc_buckets(), use_fahrenheit=True,
        )
        newly_2 = set(r2["newly_eliminated_this_tick"])
        assert "56-57F" in newly_2
        assert "52-53F" not in newly_2  # already eliminated in prior tick
        assert "54-55F" not in newly_2

        # 12:00 — HF max 60.0F → 58-59F new. 60-61F just entered, not eliminated yet.
        r3 = analyze_elimination(
            city="new york", target_date="2026-04-19",
            hf_max=60.0, hf_max_time="12:00",
            hf_source_kind="wgov_5min", hf_icao="KLGA",
            hf_observation_count=160, hf_median_gap_min=5.0,
            all_buckets=self._nyc_buckets(), use_fahrenheit=True,
        )
        newly_3 = set(r3["newly_eliminated_this_tick"])
        assert "58-59F" in newly_3
        assert "56-57F" not in newly_3
        assert "60-61F" not in newly_3  # we're IN this bucket, not past it

    def test_new_day_resets_state(self):
        # Day 1 — eliminate 52-53 and 54-55
        analyze_elimination(
            city="new york", target_date="2026-04-19",
            hf_max=55.0, hf_max_time="08:00",
            hf_source_kind="wgov_5min", hf_icao="KLGA",
            hf_observation_count=100, hf_median_gap_min=5.0,
            all_buckets=self._nyc_buckets(), use_fahrenheit=True,
        )
        # Day 2 — same starting temp should produce same NEW eliminations
        r = analyze_elimination(
            city="new york", target_date="2026-04-20",  # different date
            hf_max=55.0, hf_max_time="08:00",
            hf_source_kind="wgov_5min", hf_icao="KLGA",
            hf_observation_count=100, hf_median_gap_min=5.0,
            all_buckets=self._nyc_buckets(), use_fahrenheit=True,
        )
        newly = set(r["newly_eliminated_this_tick"])
        assert "52-53F" in newly
        assert "54-55F" in newly

    def test_no_re_entry_on_retracement(self):
        # 13:00 — hits 68F, eliminates 66-67
        r1 = analyze_elimination(
            city="houston", target_date="2026-04-19",
            hf_max=68.0, hf_max_time="13:00",
            hf_source_kind="wgov_5min", hf_icao="KHOU",
            hf_observation_count=150, hf_median_gap_min=5.0,
            all_buckets=[
                {"label": "66-67°F", "temp": 66.0, "direction": "exact", "no_buy": 0.95, "liquidity": 2000, "slug": "..-66f"},
            ],
            use_fahrenheit=True,
        )
        assert "66-67°F" in r1["newly_eliminated_this_tick"]

        # 14:30 — temp dips back to 65F (cloud cover). Bucket should NOT re-fire.
        # Note: analyze_elimination uses the current hf_max, so if hf_max is 65F
        # then 66-67 isn't even 'eliminated' per current reading. We're testing
        # the prior-state persistence: on a subsequent tick with hf_max=68 again
        # (temp re-warms), bucket should still not be "newly" eliminated because
        # it was already in prior state.
        r2 = analyze_elimination(
            city="houston", target_date="2026-04-19",
            hf_max=68.0, hf_max_time="15:00",
            hf_source_kind="wgov_5min", hf_icao="KHOU",
            hf_observation_count=180, hf_median_gap_min=5.0,
            all_buckets=[
                {"label": "66-67°F", "temp": 66.0, "direction": "exact", "no_buy": 0.95, "liquidity": 2000, "slug": "..-66f"},
            ],
            use_fahrenheit=True,
        )
        assert "66-67°F" not in r2["newly_eliminated_this_tick"]


# ---------------------------------------------------------------------------
# Persistent log for backtest replay
# ---------------------------------------------------------------------------

class TestEliminationLog:
    def test_appends_newly_eliminated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POLYWEATHER_RUNTIME_DATA_DIR", str(tmp_path))
        # Also reload the log-path resolver
        analysis = {
            "hf_max": 68.0,
            "hf_max_time": "12:05",
            "hf_source_kind": "wgov_5min",
            "newly_eliminated_this_tick": ["66-67°F"],
            "eliminated_buckets": [
                {
                    "label": "66-67°F",
                    "slug": "..-66-67f",
                    "bucket_temp": 66.0,
                    "bucket_upper": 67.499,
                    "no_price": 0.95,
                    "locked_edge_pct": 5.0,
                    "liquidity": 3680,
                    "tradable": True,
                },
            ],
        }
        append_elimination_log(
            city="san francisco",
            target_date="2026-04-19",
            analysis=analysis,
        )
        log_path = tmp_path / "alpha_logs" / "eliminations.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["city"] == "san francisco"
        assert rec["bucket_label"] == "66-67°F"
        assert rec["locked_edge_pct"] == 5.0
        assert rec["tradable"] is True

    def test_skips_empty_newly_eliminated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POLYWEATHER_RUNTIME_DATA_DIR", str(tmp_path))
        analysis = {
            "hf_max": 68.0,
            "newly_eliminated_this_tick": [],
            "eliminated_buckets": [{"label": "66-67°F", "tradable": True}],
        }
        append_elimination_log(
            city="san francisco", target_date="2026-04-19", analysis=analysis
        )
        log_path = tmp_path / "alpha_logs" / "eliminations.jsonl"
        assert not log_path.exists()
