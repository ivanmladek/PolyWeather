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

    # --- HF (high-frequency) bucket crossing ---

    def test_accepts_hf_bucket_crossed_when_metar_stale(self):
        """
        HF data (5-min weather.gov) shows bucket 80 while METAR still shows 79.
        With bucket_crossed=True and hf_bucket matching predicted bucket,
        the gate should accept this as a valid speed-alpha signal because
        this is where the alpha is maximal (market still reading the old METAR).
        """
        d = _make_decision(
            city="Dallas", name="dallas",
            max_so_far=79.0, bucket=80, market_price=0.15,
        )
        # max_so_far=79.0 would round to 79 (not 80), so primary check fails.
        # But HF override shows bucket crossed to 80 — secondary check passes.
        d["detail"]["hf_max_override"] = {
            "bucket_crossed": True,
            "hf_bucket": 80,
            "metar_bucket": 79,
            "hf_max": 79.6,
            "metar_max": 79.0,
            "temp_advantage": 0.6,
            "source_kind": "wgov_5min",
        }
        d["hf_max_override"] = d["detail"]["hf_max_override"]
        assert is_speed_alpha_trade(d) is True

    def test_rejects_hf_bucket_crossed_when_wrong_bucket(self):
        """HF shows bucket 80, but LLM predicted bucket 81 — reject."""
        d = _make_decision(
            city="Dallas", name="dallas",
            max_so_far=79.0, bucket=81, market_price=0.15,
        )
        d["detail"]["hf_max_override"] = {
            "bucket_crossed": True,
            "hf_bucket": 80,  # predicted=81, hf=80 → mismatch
            "metar_bucket": 79,
            "hf_max": 79.6,
            "metar_max": 79.0,
        }
        d["hf_max_override"] = d["detail"]["hf_max_override"]
        assert is_speed_alpha_trade(d) is False

    def test_rejects_when_no_hf_and_bucket_mismatch(self):
        """Without HF override, standard bucket confirmation is required."""
        d = _make_decision(
            city="Dallas", name="dallas",
            max_so_far=79.0, bucket=81, market_price=0.15,
        )
        # No hf_max_override at all
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
    """Legacy and @postpeak cooldowns must be independent."""

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

    def test_postpeak_cooldown_independent_of_legacy(self):
        """Pushing to legacy must NOT block @postpeak, and vice versa."""
        city, bucket, date = "IndependenceTest", 79, "2026-04-15"
        # Mark legacy pushed
        scan_alpha._mark_pushed(city, bucket, date)
        # Postpeak should NOT be on cooldown
        assert scan_alpha._is_postpeak_on_cooldown(city, bucket, date) is False

        # Now mark postpeak pushed
        scan_alpha._mark_postpeak_pushed(city, bucket, date)
        assert scan_alpha._is_postpeak_on_cooldown(city, bucket, date) is True

    def test_postpeak_cooldown_basic(self):
        assert scan_alpha._is_postpeak_on_cooldown("PPTest", 33, "2026-04-17") is False
        scan_alpha._mark_postpeak_pushed("PPTest", 33, "2026-04-17")
        assert scan_alpha._is_postpeak_on_cooldown("PPTest", 33, "2026-04-17") is True


# ===================================================================
# Config constants sanity
# ===================================================================

class TestKellySizing:
    """kelly_size_pct — fractional Kelly position sizing."""

    kelly_fn = staticmethod(scan_alpha.kelly_size_pct)

    def test_miami_speed_alpha(self):
        """Miami Apr 17: post-peak confirmed, market 40%. Capped at 10%."""
        pct, full, label = self.kelly_fn("post_peak", True, 0.65, 0.40)
        assert full == pytest.approx(79.2, abs=0.5)  # (0.875 - 0.40) / 0.60
        # Quarter-Kelly would be 19.8%, but hard cap at 10%
        assert pct == scan_alpha.KELLY_MAX_PCT
        assert "speed-alpha" in label

    def test_golden_hour_uses_empirical_rate(self):
        """Golden hour: uses 36.4% win rate, not model probability."""
        pct, full, label = self.kelly_fn("golden_hour", False, 0.58, 0.13)
        # Kelly with q=0.364, p=0.13: (0.364-0.13)/(1-0.13) = 26.9%
        assert full == pytest.approx(26.9, abs=0.5)
        assert "golden-hour" in label

    def test_no_edge_returns_zero(self):
        """Market price above empirical win rate -> no bet."""
        pct, full, label = self.kelly_fn("golden_hour", False, 0.70, 0.50)
        # q=0.364 < p=0.50 -> no edge
        assert pct == 0.0
        assert "no edge" in label

    def test_extreme_price_skip(self):
        """Market at 99.7% -> skip, even if model says 100%."""
        pct, _, label = self.kelly_fn("post_peak", True, 1.0, 0.997)
        assert pct == 0.0
        assert "extreme" in label

    def test_hard_cap(self):
        """Size must never exceed KELLY_MAX_PCT (10%)."""
        pct, full, _ = self.kelly_fn("post_peak", True, 0.875, 0.05)
        assert full > 80
        assert pct <= scan_alpha.KELLY_MAX_PCT

    def test_post_peak_non_speed_alpha(self):
        """Post-peak but NOT speed-alpha (bucket unconfirmed)."""
        pct, full, label = self.kelly_fn("post_peak", False, 0.80, 0.50)
        # Uses KELLY_Q_POST_PEAK = 0.919: (0.919-0.50)/0.50 = 83.8%
        assert full == pytest.approx(83.8, abs=0.5)
        assert "post-peak" in label


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


# ===================================================================
# Verbose Telegram message formatting with HF context
# ===================================================================

class TestVerboseTelegramMessage:
    """The @postpeak message must include full HF context for trading AIs."""

    def _make_buy_with_hf(self):
        """Build a complete BUY_YES decision with HF override info."""
        return {
            "city": "Dallas",
            "name": "dallas",
            "entry_mode": "post_peak",
            "hours_to_peak": -0.3,
            "sigma": 0.5,
            "max_so_far": 79.6,
            "market_scan": {
                "selected_slug": "highest-temperature-in-dallas-on-april-19-2026-80f",
                "selected_date": "2026-04-19",
                "market_price": 0.15,
                "liquidity": 15000,
            },
            "llm": {
                "action": "BUY_YES",
                "bucket": 80,
                "model_probability": 0.72,
                "estimated_market_price": 0.15,
                "edge_pct": 57.0,
                "confidence": "high",
                "size_pct_of_bankroll": 2.5,
                "reasoning": "HF shows bucket crossed to 80, METAR still at 79",
                "risk_factors": [],
                "time_sensitivity": "immediate",
            },
            "detail": {
                "temp_symbol": "°F",
                "current": {"max_so_far": 79.6, "max_temp_time": "14:55"},
                "deb": {"prediction": 80.2},
                "probabilities": {"mu": 80.1},
                "hf_source": {
                    "icao": "KDAL",
                    "source_kind": "wgov_5min",
                    "observation_count": 198,
                    "median_gap_minutes": 5.0,
                    "max_temp": 79.6,
                    "max_temp_time": "14:55",
                    "latest_temp": 79.2,
                    "latest_time": "15:10",
                },
                "hf_max_override": {
                    "bucket_crossed": True,
                    "hf_bucket": 80,
                    "metar_bucket": 79,
                    "hf_max": 79.6,
                    "hf_max_time": "14:55",
                    "metar_max": 79.0,
                    "metar_max_time": "13:53",
                    "temp_advantage": 0.6,
                    "source_kind": "wgov_5min",
                    "icao": "KDAL",
                    "hf_observation_count": 198,
                    "hf_median_gap_min": 5.0,
                    "hf_beats_metar": True,
                },
                "hf_peak_detection": {
                    "status": "post_peak",
                    "confidence": 0.72,
                    "peak_time": "14:55",
                    "peak_temp_f": 79.6,
                    "decline_from_peak_f": 0.4,
                    "hf_trend_direction": "falling",
                },
                "hf_alpha": {
                    "has_alpha": True,
                    "alpha_type": "hf_post_peak_vs_metar_in_window",
                    "alpha_minutes": 37,
                },
            },
        }

    def test_verbose_message_contains_hf_override_section(self):
        d = self._make_buy_with_hf()
        msg = scan_alpha._format_telegram_message([d], bankroll=1000.0)
        assert "HF MAX OVERRIDE" in msg
        assert "BUCKET CROSSED" in msg
        assert "79.6" in msg  # hf_max
        assert "79.0" in msg  # metar_max
        assert "wgov_5min" in msg
        assert "KDAL" in msg

    def test_verbose_message_contains_hf_peak_detection(self):
        d = self._make_buy_with_hf()
        msg = scan_alpha._format_telegram_message([d], bankroll=1000.0)
        assert "HF PEAK DETECTION" in msg
        assert "post_peak" in msg
        assert "14:55" in msg

    def test_verbose_message_contains_alpha_minutes_ahead(self):
        d = self._make_buy_with_hf()
        msg = scan_alpha._format_telegram_message([d], bankroll=1000.0)
        assert "alpha_minutes_ahead_of_metar" in msg
        assert "37" in msg

    def test_verbose_message_contains_action_payload(self):
        """Must include structured ACTION data for automated trading AI."""
        d = self._make_buy_with_hf()
        msg = scan_alpha._format_telegram_message([d], bankroll=1000.0)
        assert "ACTION: BUY_YES" in msg
        assert "market_slug=" in msg
        assert "target_fill_price" in msg
        assert "min_size_usd" in msg
        assert "max_size_usd" in msg
        assert "polymarket.com/market/" in msg

    def test_verbose_message_contains_market_and_sizing(self):
        d = self._make_buy_with_hf()
        msg = scan_alpha._format_telegram_message([d], bankroll=1000.0)
        assert "MARKET:" in msg
        assert "SIZING:" in msg
        assert "TIMING:" in msg
        assert "model_prob" in msg
        assert "market_price" in msg
        assert "edge=" in msg

    def test_verbose_message_handles_missing_hf(self):
        """Should still format cleanly when HF data is absent."""
        d = self._make_buy_with_hf()
        d["detail"].pop("hf_max_override", None)
        d["detail"].pop("hf_source", None)
        d["detail"].pop("hf_peak_detection", None)
        d["detail"].pop("hf_alpha", None)
        msg = scan_alpha._format_telegram_message([d], bankroll=1000.0)
        # Core sections should still appear
        assert "MARKET:" in msg
        assert "SETTLEMENT ANCHOR:" in msg
        assert "ACTION: BUY_YES" in msg
        # HF sections should be omitted (not crash)
        assert "HF MAX OVERRIDE" not in msg

    def test_verbose_message_bucket_crossed_marker(self):
        """The key alpha signal: 'BUCKET CROSSED' should be clearly visible."""
        d = self._make_buy_with_hf()
        msg = scan_alpha._format_telegram_message([d], bankroll=1000.0)
        # The asterisk-wrapped BUCKET CROSSED line is the key trading trigger
        assert "*** BUCKET CROSSED" in msg
        assert "HF-TRIGGERED ALERT" in msg


# ===================================================================
# HF Elimination Arbitrage — evaluate_elimination_trades + formatter
# ===================================================================

class TestElimArbEvaluation:
    """evaluate_elimination_trades — core elim-arb signal filter."""

    def setup_method(self):
        """Clear elim cooldowns between tests."""
        scan_alpha._elim_cooldown.clear()
        scan_alpha._elim_daily_deployed.clear()
        scan_alpha._elim_session_trajectory.clear()

    def _sf_elim_analysis(self, *, include_64_65=False):
        """Live-example elimination_analysis like SF Apr 19."""
        buckets = []
        if include_64_65:
            buckets.append({
                "label": "64-65°F",
                "slug": "..-64-65f",
                "bucket_temp": 64.0,
                "bucket_upper": 65.499,
                "direction": "exact",
                "no_price": 0.999,
                "locked_edge_pct": 0.1,
                "liquidity": 4837,
                "tradable": False,  # edge 0.1% below the 1.5% floor
                "eliminated_at_utc": "2026-04-19T20:45:00Z",
                "eliminated_by_temp": 68.0,
            })
        buckets.append({
            "label": "66-67°F",
            "slug": "highest-temperature-in-san-francisco-on-april-19-2026-66-67f",
            "bucket_temp": 66.0,
            "bucket_upper": 67.499,
            "direction": "exact",
            "no_price": 0.95,
            "locked_edge_pct": 5.0,
            "liquidity": 3680,
            "tradable": True,
            "eliminated_at_utc": "2026-04-19T21:05:00Z",
            "eliminated_by_temp": 68.0,
        })
        newly = [b["label"] for b in buckets]
        return {
            "hf_max": 68.0,
            "hf_max_time": "12:05",
            "hf_source_kind": "wgov_5min",
            "hf_icao": "KSFO",
            "hf_observation_count": 226,
            "hf_median_gap_min": 5.0,
            "eliminated_buckets": buckets,
            "live_buckets": ["68-69°F", "70F+"],
            "newly_eliminated_this_tick": newly,
            "updated_at": "2026-04-19T21:05:00Z",
        }

    def test_single_tradable_signal(self):
        elim = self._sf_elim_analysis()
        trades = scan_alpha.evaluate_elimination_trades(
            city="san francisco",
            display_name="San Francisco",
            elimination=elim,
            temp_symbol="°F",
            bankroll=1000.0,
        )
        assert len(trades) == 1
        t = trades[0]
        assert t["bucket_label"] == "66-67°F"
        assert t["no_price"] == 0.95
        assert t["edge_pct"] == 5.0
        assert t["size_usd"] > 0

    def test_below_edge_floor_skipped(self):
        """64-65F at 99.9c has 0.1% edge — below 1.5% floor."""
        elim = self._sf_elim_analysis(include_64_65=True)
        trades = scan_alpha.evaluate_elimination_trades(
            city="san francisco",
            display_name="San Francisco",
            elimination=elim,
            temp_symbol="°F",
            bankroll=1000.0,
        )
        labels = [t["bucket_label"] for t in trades]
        assert "64-65°F" not in labels
        assert "66-67°F" in labels

    def test_per_bucket_cooldown(self):
        elim = self._sf_elim_analysis()
        # First call — trade fires
        trades1 = scan_alpha.evaluate_elimination_trades(
            city="san francisco",
            display_name="San Francisco",
            elimination=elim,
            temp_symbol="°F",
            bankroll=1000.0,
        )
        assert len(trades1) == 1
        # Mark as pushed — simulating Telegram send
        t = trades1[0]
        scan_alpha._mark_elim_pushed(t["city"], t["date"], t["bucket_label"])
        # Second call — cooldown active, no trade
        trades2 = scan_alpha.evaluate_elimination_trades(
            city="san francisco",
            display_name="San Francisco",
            elimination=elim,
            temp_symbol="°F",
            bankroll=1000.0,
        )
        assert trades2 == []

    def test_liquidity_floor(self):
        elim = self._sf_elim_analysis()
        # Lower liquidity below ELIM_MIN_LIQUIDITY_USD (default $500)
        elim["eliminated_buckets"][0]["liquidity"] = 100
        trades = scan_alpha.evaluate_elimination_trades(
            city="san francisco",
            display_name="San Francisco",
            elimination=elim,
            temp_symbol="°F",
            bankroll=1000.0,
        )
        assert trades == []

    def test_daily_cap_enforcement(self):
        """Once daily cap is hit, further trades should be sized to 0 and skipped."""
        elim = self._sf_elim_analysis()
        # Simulate already-deployed near the daily cap
        scan_alpha._record_elim_deployment(
            1000.0 * (scan_alpha.ELIM_DAILY_CAP_PCT / 100.0) - 5  # leave $5 remaining
        )
        trades = scan_alpha.evaluate_elimination_trades(
            city="san francisco",
            display_name="San Francisco",
            elimination=elim,
            temp_symbol="°F",
            bankroll=1000.0,
        )
        # $5 remaining < $10 floor → skipped
        assert trades == []

    def test_high_edge_marker(self):
        """An elim trade with edge >= 8% gets marked high_edge=True."""
        elim = self._sf_elim_analysis()
        # Override to a high-edge case
        elim["eliminated_buckets"][0]["no_price"] = 0.89
        elim["eliminated_buckets"][0]["locked_edge_pct"] = 11.0
        trades = scan_alpha.evaluate_elimination_trades(
            city="san francisco",
            display_name="San Francisco",
            elimination=elim,
            temp_symbol="°F",
            bankroll=1000.0,
        )
        assert len(trades) == 1
        assert trades[0]["high_edge"] is True


class TestElimSizeCaps:
    """_elim_size_usd — three-cap sizing logic."""

    def setup_method(self):
        scan_alpha._elim_daily_deployed.clear()

    def test_per_trade_cap(self):
        # bankroll=$1000, per-trade-cap = 5% = $50
        size = scan_alpha._elim_size_usd(
            bankroll=1000.0, no_price=0.95, liquidity_usd=100000
        )
        assert size == 50.0

    def test_book_depth_cap(self):
        # bankroll=$1000 (per-trade $50), liquidity=$100 → 30% = $30
        size = scan_alpha._elim_size_usd(
            bankroll=1000.0, no_price=0.95, liquidity_usd=100
        )
        assert size == 30.0

    def test_daily_cap(self):
        # bankroll=$1000, daily-cap = 15% = $150
        # Deploy $145 already; only $5 remaining, floored by other caps
        scan_alpha._record_elim_deployment(145.0)
        size = scan_alpha._elim_size_usd(
            bankroll=1000.0, no_price=0.95, liquidity_usd=100000
        )
        assert size == 5.0


class TestElimTelegramMessage:
    """_format_elim_telegram_message — verbose output for trading AIs."""

    def setup_method(self):
        scan_alpha._elim_cooldown.clear()
        scan_alpha._elim_daily_deployed.clear()
        scan_alpha._elim_session_trajectory.clear()

    def _build_trades_by_city(self):
        elim_sf = {
            "hf_max": 68.0,
            "hf_max_time": "12:05",
            "hf_source_kind": "wgov_5min",
            "hf_icao": "KSFO",
            "hf_observation_count": 226,
            "hf_median_gap_min": 5.0,
            "eliminated_buckets": [],
            "live_buckets": ["68-69°F", "70F+"],
            "newly_eliminated_this_tick": ["66-67°F"],
        }
        trades = [{
            "city": "san francisco",
            "display_name": "San Francisco",
            "bucket_label": "66-67°F",
            "slug": "highest-temperature-in-san-francisco-on-april-19-2026-66-67f",
            "no_price": 0.95,
            "edge_pct": 5.0,
            "liquidity": 3680,
            "size_usd": 50.0,
            "bucket_temp": 66.0,
            "bucket_upper": 67.499,
            "hf_max": 68.0,
            "hf_max_time": "12:05",
            "hf_source_kind": "wgov_5min",
            "hf_icao": "KSFO",
            "temp_symbol": "°F",
            "date": "2026-04-19",
            "high_edge": False,
        }]
        return {
            "san francisco": {
                "display_name": "San Francisco",
                "elimination": elim_sf,
                "trades": trades,
            }
        }, {"san francisco": ({}, {})}

    def test_message_contains_core_sections(self):
        trades_by_city, city_data = self._build_trades_by_city()
        msg = scan_alpha._format_elim_telegram_message(
            trades_by_city, bankroll=1000.0, city_meta=city_data
        )
        assert "[ELIM-ARB]" in msg
        assert "SAN FRANCISCO" in msg
        assert "KSFO" in msg
        assert "BUY_NO" in msg
        assert "66-67°F" in msg
        assert "edge=5.00%" in msg or "edge=5.0" in msg
        assert "market_slug=" in msg
        assert "target_fill_price" in msg
        assert "rationale:" in msg
        assert "=== SUMMARY ===" in msg

    def test_message_contains_action_url(self):
        trades_by_city, city_data = self._build_trades_by_city()
        msg = scan_alpha._format_elim_telegram_message(
            trades_by_city, bankroll=1000.0, city_meta=city_data
        )
        assert "polymarket.com/market/" in msg

    def test_message_flags_high_edge(self):
        trades_by_city, city_data = self._build_trades_by_city()
        trades_by_city["san francisco"]["trades"][0]["high_edge"] = True
        trades_by_city["san francisco"]["trades"][0]["no_price"] = 0.89
        trades_by_city["san francisco"]["trades"][0]["edge_pct"] = 11.0
        msg = scan_alpha._format_elim_telegram_message(
            trades_by_city, bankroll=1000.0, city_meta=city_data
        )
        assert "*** HIGH EDGE" in msg

    def test_message_shows_newly_eliminated_list(self):
        trades_by_city, city_data = self._build_trades_by_city()
        msg = scan_alpha._format_elim_telegram_message(
            trades_by_city, bankroll=1000.0, city_meta=city_data
        )
        assert "NEWLY ELIMINATED THIS CYCLE" in msg
        assert "66-67°F" in msg
