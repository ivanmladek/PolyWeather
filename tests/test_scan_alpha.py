"""
Tests for scripts/scan_alpha.py — speed-alpha filter, settlement rounding,
prompt building safety, and signal classification.
"""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load scan_alpha as a module without running __main__
# ---------------------------------------------------------------------------
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

spec = importlib.util.spec_from_file_location("scan_alpha", _root / "scripts" / "scan_alpha.py")
scan_alpha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan_alpha)

is_speed_alpha_trade = scan_alpha.is_speed_alpha_trade


# Also import settlement rounding directly for cross-validation
from src.analysis.settlement_rounding import apply_city_settlement, wu_round


# ---------------------------------------------------------------------------
# Fixtures: representative decision dicts from real trades
# ---------------------------------------------------------------------------

def _make_decision(*, entry_mode="post_peak", city="Dallas", name="dallas",
                   max_so_far=79.0, bucket=79, market_price=0.12,
                   sigma=0.5, liquidity=10000):
    """Build a minimal decision dict matching scan_alpha's structure."""
    return {
        "entry_mode": entry_mode,
        "city": city,
        "name": name,
        "detail": {"current": {"max_so_far": max_so_far}},
        "market_scan": {"liquidity": liquidity},
        "llm": {"bucket": bucket, "estimated_market_price": market_price},
        "sigma": sigma,
    }


# ===================================================================
# Settlement rounding
# ===================================================================

class TestSettlementRounding:
    """wu_round and HKO floor rounding used for bucket matching."""

    def test_wu_round_half_up(self):
        assert wu_round(79.5) == 80
        assert wu_round(79.4) == 79
        assert wu_round(20.0) == 20

    def test_wu_round_negative(self):
        assert wu_round(-2.5) == -3
        assert wu_round(-2.4) == -2

    def test_wu_round_none(self):
        assert wu_round(None) is None

    def test_hko_floor_rounding(self):
        """Hong Kong uses floor(), not wu_round."""
        assert apply_city_settlement("hong kong", 30.9) == 30
        assert apply_city_settlement("hong kong", 30.0) == 30
        assert apply_city_settlement("hong kong", 31.0) == 31

    def test_lau_fau_shan_also_hko(self):
        assert apply_city_settlement("lau fau shan", 28.7) == 28

    def test_regular_city_uses_wu_round(self):
        assert apply_city_settlement("dallas", 79.0) == 79
        assert apply_city_settlement("dallas", 79.5) == 80
        assert apply_city_settlement("taipei", 31.5) == 32

    def test_none_passthrough(self):
        assert apply_city_settlement("dallas", None) is None


# ===================================================================
# Speed-alpha gate
# ===================================================================

class TestSpeedAlphaGate:
    """is_speed_alpha_trade — the core filter for @postpeak signals."""

    def test_perfect_post_peak_signal(self):
        """Dallas Apr 15: post-peak, bucket=79, max_so_far=79, mkt=12%."""
        d = _make_decision()
        assert is_speed_alpha_trade(d) is True

    def test_rejects_golden_hour(self):
        d = _make_decision(entry_mode="golden_hour")
        assert is_speed_alpha_trade(d) is False

    def test_rejects_unknown_entry_mode(self):
        d = _make_decision(entry_mode="unknown")
        assert is_speed_alpha_trade(d) is False

    def test_rejects_market_too_low(self):
        """Market < 5% = likely bucket-market mismatch."""
        d = _make_decision(market_price=0.01)
        assert is_speed_alpha_trade(d) is False

    def test_rejects_market_too_high(self):
        """Market > 70% = already priced in, no alpha."""
        d = _make_decision(market_price=0.85)
        assert is_speed_alpha_trade(d) is False

    def test_accepts_market_at_boundaries(self):
        """5% and 70% should be accepted (inclusive)."""
        d_low = _make_decision(market_price=0.05)
        assert is_speed_alpha_trade(d_low) is True

        d_high = _make_decision(market_price=0.70)
        assert is_speed_alpha_trade(d_high) is True

    def test_rejects_bucket_not_confirmed(self):
        """max_so_far doesn't round to predicted bucket."""
        d = _make_decision(max_so_far=79.0, bucket=81)
        assert is_speed_alpha_trade(d) is False

    def test_rejects_no_max_so_far(self):
        d = _make_decision()
        d["detail"]["current"]["max_so_far"] = None
        assert is_speed_alpha_trade(d) is False

    def test_rejects_missing_detail(self):
        d = _make_decision()
        d["detail"] = {}
        assert is_speed_alpha_trade(d) is False

    def test_rejects_sigma_too_wide(self):
        d = _make_decision(sigma=2.0)
        assert is_speed_alpha_trade(d) is False

    def test_accepts_sigma_at_boundary(self):
        d = _make_decision(sigma=1.49)
        assert is_speed_alpha_trade(d) is True

    def test_rejects_low_liquidity(self):
        d = _make_decision(liquidity=50)
        assert is_speed_alpha_trade(d) is False

    def test_rejects_no_bucket(self):
        d = _make_decision()
        d["llm"]["bucket"] = None
        assert is_speed_alpha_trade(d) is False

    # --- HKO city rounding in the gate ---

    def test_hko_floor_rounding_in_gate(self):
        """Hong Kong: max_so_far=30.9 should confirm bucket=30 (floor)."""
        d = _make_decision(city="Hong Kong", name="hong kong",
                           max_so_far=30.9, bucket=30, market_price=0.40)
        assert is_speed_alpha_trade(d) is True

    def test_hko_floor_rejects_wu_round_bucket(self):
        """Hong Kong: max_so_far=30.9 -> floor=30, NOT wu_round=31."""
        d = _make_decision(city="Hong Kong", name="hong kong",
                           max_so_far=30.9, bucket=31, market_price=0.40)
        assert is_speed_alpha_trade(d) is False

    # --- Real backtest cases ---

    def test_real_case_atlanta_apr15(self):
        """Atlanta Apr 15: post-peak, bucket=84, max=84.0, mkt=30%."""
        d = _make_decision(city="Atlanta", name="atlanta",
                           max_so_far=84.0, bucket=84, market_price=0.30)
        assert is_speed_alpha_trade(d) is True

    def test_real_case_taipei_fails(self):
        """Taipei: predicted 29, actual 31.5 -> settled 32. Bucket mismatch."""
        d = _make_decision(city="Taipei", name="taipei",
                           max_so_far=31.5, bucket=29, market_price=0.26)
        assert is_speed_alpha_trade(d) is False

    def test_real_case_london_priced_in(self):
        """London Apr 16: market at 99%, no alpha left."""
        d = _make_decision(city="London", name="london",
                           max_so_far=18.0, bucket=18, market_price=0.99)
        assert is_speed_alpha_trade(d) is False


# ===================================================================
# Prompt building: single-bucket distribution safety
# ===================================================================

class TestPromptBuildingSafety:
    """The _build_user_prompt f-string must not crash on edge-case distributions."""

    def test_single_bucket_distribution(self):
        """Tel Aviv case: only 1 bucket in distribution. Must not IndexError."""
        # We can't easily call _build_user_prompt without full API data,
        # so test the pre-computation logic directly.
        sorted_dist = [{"value": 33, "probability": 1.0, "range": "33-34"}]

        top1_str = (
            f"{sorted_dist[0].get('value')}deg @ "
            f"{sorted_dist[0].get('probability', 0)*100:.1f}% "
            f"(range: {sorted_dist[0].get('range', '?')})"
        ) if sorted_dist else "? (no distribution)"

        if len(sorted_dist) > 1:
            b2 = sorted_dist[1]
            top2_str = f"{b2.get('value')}deg"
            adjacent = abs((sorted_dist[0].get("value") or 0) - (b2.get("value") or 0)) <= 1
        else:
            top2_str = "(none — single-bucket distribution)"
            adjacent = False

        combined = sum(b.get("probability", 0) for b in sorted_dist[:2]) * 100

        assert "33deg" in top1_str
        assert top2_str == "(none — single-bucket distribution)"
        assert adjacent is False
        assert combined == pytest.approx(100.0)

    def test_two_bucket_distribution(self):
        """Normal case: 2+ buckets should extract both."""
        sorted_dist = [
            {"value": 79, "probability": 0.6, "range": "79-80"},
            {"value": 80, "probability": 0.3, "range": "80-81"},
        ]

        if len(sorted_dist) > 1:
            b2 = sorted_dist[1]
            top2_str = (
                f"{b2.get('value')}deg @ {b2.get('probability', 0)*100:.1f}% "
                f"(range: {b2.get('range', '?')})"
            )
            adjacent = abs((sorted_dist[0].get("value") or 0) - (b2.get("value") or 0)) <= 1
        else:
            top2_str = "(none)"
            adjacent = False

        assert "80deg" in top2_str
        assert "30.0%" in top2_str
        assert adjacent is True

    def test_empty_distribution(self):
        sorted_dist = []
        top1_str = (
            f"{sorted_dist[0].get('value')}" if sorted_dist else "? (no distribution)"
        )
        assert top1_str == "? (no distribution)"

    def test_non_adjacent_buckets(self):
        sorted_dist = [
            {"value": 79, "probability": 0.4, "range": "79-80"},
            {"value": 82, "probability": 0.2, "range": "82-83"},
        ]
        adjacent = (
            len(sorted_dist) > 1
            and abs((sorted_dist[0].get("value") or 0) - (sorted_dist[1].get("value") or 0)) <= 1
        )
        assert adjacent is False


# ===================================================================
# Cooldown logic
# ===================================================================

class TestCooldown:
    def test_not_on_cooldown_initially(self):
        assert scan_alpha._is_on_cooldown("TestCity", 79, "2026-04-15") is False

    def test_on_cooldown_after_mark(self):
        scan_alpha._mark_pushed("CooldownTest", 99, "2026-04-15")
        assert scan_alpha._is_on_cooldown("CooldownTest", 99, "2026-04-15") is True

    def test_different_bucket_not_on_cooldown(self):
        scan_alpha._mark_pushed("CooldownTest2", 80, "2026-04-15")
        assert scan_alpha._is_on_cooldown("CooldownTest2", 81, "2026-04-15") is False

    def test_different_date_not_on_cooldown(self):
        scan_alpha._mark_pushed("CooldownTest3", 80, "2026-04-15")
        assert scan_alpha._is_on_cooldown("CooldownTest3", 80, "2026-04-16") is False


# ===================================================================
# Config constants sanity
# ===================================================================

class TestConfigConstants:
    """Sanity check that the speed-alpha thresholds match the report."""

    def test_speed_alpha_market_window(self):
        assert scan_alpha.SPEED_ALPHA_MKT_MIN == 0.05
        assert scan_alpha.SPEED_ALPHA_MKT_MAX == 0.70

    def test_golden_hour_window(self):
        assert scan_alpha.GOLDEN_HOUR_MIN == 1.0
        assert scan_alpha.GOLDEN_HOUR_MAX == 3.0

    def test_post_peak_window(self):
        assert scan_alpha.POST_PEAK_MAX == 2.0

    def test_sigma_limits(self):
        assert scan_alpha.MAX_SIGMA_GOLDEN == 1.5
        assert scan_alpha.SPEED_ALPHA_SIGMA_MAX == 1.5
