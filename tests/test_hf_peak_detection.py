"""
Tests for high-frequency (1-minute) peak detection engine and ASOS data source.
"""

import pytest
from datetime import datetime, timezone
from src.analysis.hf_peak_detection import (
    detect_peak,
    compute_hf_alpha_summary,
    PeakDetectionResult,
    _check_sustained_decline,
    _rolling_regression_slope,
    _check_plateau_break,
)


# ---------------------------------------------------------------------------
# Helper to generate synthetic 1-minute observation data
# ---------------------------------------------------------------------------

def _make_observations(
    temps_f: list[float],
    start_hour: int = 8,
    start_minute: int = 0,
) -> list[dict]:
    """Generate synthetic 1-minute observations from a list of °F temps."""
    observations = []
    for i, temp_f in enumerate(temps_f):
        minute = start_minute + i
        hour = start_hour + minute // 60
        minute_of_hour = minute % 60
        temp_c = round((temp_f - 32) * 5 / 9, 2)
        observations.append({
            "utc_time": f"2026-04-19T{hour:02d}:{minute_of_hour:02d}:00Z",
            "local_time": f"{hour:02d}:{minute_of_hour:02d}",
            "local_datetime": f"2026-04-19T{hour:02d}:{minute_of_hour:02d}",
            "temp_f": temp_f,
            "temp_c": temp_c,
            "dwp_f": temp_f - 10.0,
            "dwp_c": round((temp_f - 10.0 - 32) * 5 / 9, 2),
        })
    return observations


# ---------------------------------------------------------------------------
# PeakDetectionResult tests
# ---------------------------------------------------------------------------

class TestPeakDetectionResult:
    def test_to_dict(self):
        result = PeakDetectionResult(
            status="post_peak",
            confidence=0.75,
            peak_temp_f=85.0,
            peak_temp_c=29.4,
            peak_time="14:30",
            observation_count=120,
        )
        d = result.to_dict()
        assert d["status"] == "post_peak"
        assert d["confidence"] == 0.75
        assert d["peak_temp_f"] == 85.0
        assert d["observation_count"] == 120

    def test_default_values(self):
        result = PeakDetectionResult(status="uncertain", confidence=0.0)
        assert result.alpha_signal == "none"
        assert result.hf_trend_direction == "unknown"
        assert result.evidence == []


# ---------------------------------------------------------------------------
# Internal helper tests
# ---------------------------------------------------------------------------

class TestSustainedDecline:
    def test_no_decline(self):
        # Temperatures still rising
        temps = [70.0, 71.0, 72.0, 73.0, 74.0, 75.0]
        result = _check_sustained_decline(temps, peak_idx=5)
        assert result["is_declining"] is False

    def test_clear_decline(self):
        # Peak at index 0, then sustained decline for 20 minutes
        temps = [85.0] + [85.0 - 0.2 * i for i in range(1, 25)]
        result = _check_sustained_decline(temps, peak_idx=0)
        assert result["is_declining"] is True
        assert result["consecutive_decline_minutes"] >= 15
        assert result["decline_amount_f"] > 1.0


class TestRollingRegressionSlope:
    def test_rising_slope(self):
        # Linear rise: 0.1°F per minute
        temps = [70.0 + 0.1 * i for i in range(30)]
        slope = _rolling_regression_slope(temps, window=20)
        assert slope is not None
        assert slope > 0.05

    def test_falling_slope(self):
        # Linear fall: -0.1°F per minute
        temps = [85.0 - 0.1 * i for i in range(30)]
        slope = _rolling_regression_slope(temps, window=20)
        assert slope is not None
        assert slope < -0.05

    def test_flat_slope(self):
        temps = [75.0] * 30
        slope = _rolling_regression_slope(temps, window=20)
        assert slope is not None
        assert abs(slope) < 0.01

    def test_insufficient_data(self):
        temps = [75.0, 76.0]
        slope = _rolling_regression_slope(temps, window=20)
        assert slope is None


class TestPlateauBreak:
    def test_no_plateau(self):
        # Steady rise, no plateau
        temps = [70.0 + 0.2 * i for i in range(30)]
        result = _check_plateau_break(temps, peak_idx=0)
        assert result["is_broken"] is False

    def test_clear_plateau_break(self):
        # 15 minutes of plateau at 85°F, then drop to 83°F
        temps = [85.0] * 15 + [85.0 - 0.2 * i for i in range(1, 16)]
        result = _check_plateau_break(temps, peak_idx=0)
        assert result["is_broken"] is True
        assert result["plateau_minutes"] >= 10


# ---------------------------------------------------------------------------
# Main detect_peak function tests
# ---------------------------------------------------------------------------

class TestDetectPeak:
    def test_insufficient_data(self):
        obs = _make_observations([70.0, 71.0, 72.0])
        result = detect_peak(obs)
        assert result.status == "insufficient_data"
        assert result.confidence == 0.0

    def test_clear_pre_peak_morning(self):
        """Temperature steadily rising in the morning -> pre_peak."""
        temps = [65.0 + 0.15 * i for i in range(60)]  # Rising 0.15°F/min for 60 min
        obs = _make_observations(temps, start_hour=9)
        result = detect_peak(
            obs,
            expected_peak_start_hour=13,
            expected_peak_end_hour=15,
            local_hour_frac=10.0,
        )
        assert result.status == "pre_peak"
        assert result.hf_trend_direction == "rising"
        assert result.alpha_signal == "none"

    def test_clear_post_peak_afternoon(self):
        """Temperature peaked and is now clearly declining in afternoon."""
        # 30 min of rise, peak, then 40 min of sustained decline
        temps = [78.0 + 0.2 * i for i in range(30)]  # Rise to ~84°F
        peak_temp = temps[-1]
        temps += [peak_temp - 0.1 * i for i in range(1, 41)]  # Decline
        obs = _make_observations(temps, start_hour=13)
        result = detect_peak(
            obs,
            expected_peak_start_hour=13,
            expected_peak_end_hour=15,
            local_hour_frac=14.5,
        )
        assert result.status == "post_peak"
        assert result.confidence > 0.4
        assert result.peak_temp_f > 83.0
        assert result.decline_from_peak_f > 0.5
        assert result.minutes_since_peak > 30
        assert result.alpha_signal in ("strong_post_peak", "likely_post_peak", "possible_post_peak")

    def test_strong_post_peak_past_window(self):
        """Clear post-peak after expected peak window -> high confidence."""
        temps = [80.0 + 0.15 * i for i in range(40)]  # Rise
        peak_temp = temps[-1]
        temps += [peak_temp - 0.15 * i for i in range(1, 50)]  # Strong decline
        obs = _make_observations(temps, start_hour=12)
        result = detect_peak(
            obs,
            expected_peak_start_hour=13,
            expected_peak_end_hour=15,
            local_hour_frac=16.0,  # Past peak window
        )
        assert result.status == "post_peak"
        assert result.confidence >= 0.6
        # Should have alpha signal
        assert result.alpha_signal in ("strong_post_peak", "likely_post_peak")

    def test_plateau_at_peak(self):
        """Temperature plateaus near peak -> uncertain/at_peak."""
        temps = [75.0 + 0.2 * i for i in range(30)]  # Rise
        peak_temp = temps[-1]
        temps += [peak_temp + (0.1 if i % 2 else -0.1) for i in range(20)]  # Plateau
        obs = _make_observations(temps, start_hour=13)
        result = detect_peak(
            obs,
            expected_peak_start_hour=13,
            expected_peak_end_hour=15,
            local_hour_frac=14.0,
        )
        assert result.status in ("at_peak", "uncertain", "pre_peak")

    def test_empty_observations(self):
        result = detect_peak([])
        assert result.status == "insufficient_data"


# ---------------------------------------------------------------------------
# Alpha summary tests
# ---------------------------------------------------------------------------

class TestComputeHFAlphaSummary:
    def test_hf_post_peak_vs_metar_before(self):
        """HF says post-peak while METAR is still pre-peak -> alpha."""
        peak_result = PeakDetectionResult(
            status="post_peak",
            confidence=0.75,
            peak_temp_f=88.0,
            peak_temp_c=31.1,
            peak_time="14:30",
            current_temp_f=86.0,
            current_temp_c=30.0,
            decline_from_peak_f=2.0,
            decline_from_peak_c=1.1,
            alpha_signal="strong_post_peak",
            alpha_minutes_ahead=25,
            hf_trend_slope_per_min=-0.05,
            hf_trend_direction="falling",
        )
        summary = compute_hf_alpha_summary(
            peak_result=peak_result,
            metar_peak_status="before",
            deb_prediction=90.0,
            use_fahrenheit=True,
        )
        assert summary["has_alpha"] is True
        assert summary["alpha_type"] == "hf_post_peak_vs_metar_before"
        assert summary["alpha_confidence"] > 0
        assert "overpricing" in summary["market_implication"].lower()

    def test_hf_post_peak_vs_metar_in_window(self):
        """HF post-peak vs METAR in_window, peak below DEB -> alpha."""
        peak_result = PeakDetectionResult(
            status="post_peak",
            confidence=0.70,
            peak_temp_f=85.0,
            peak_temp_c=29.4,
            peak_time="14:15",
            current_temp_f=83.5,
            current_temp_c=28.6,
            decline_from_peak_f=1.5,
            decline_from_peak_c=0.8,
            alpha_signal="likely_post_peak",
            alpha_minutes_ahead=30,
            hf_trend_slope_per_min=-0.04,
        )
        summary = compute_hf_alpha_summary(
            peak_result=peak_result,
            metar_peak_status="in_window",
            deb_prediction=88.0,
            use_fahrenheit=True,
        )
        assert summary["has_alpha"] is True
        assert "below DEB" in summary["market_implication"]

    def test_no_alpha_when_aligned(self):
        """No alpha when both HF and METAR agree on status."""
        peak_result = PeakDetectionResult(
            status="post_peak",
            confidence=0.80,
            peak_temp_f=85.0,
            peak_temp_c=29.4,
            peak_time="14:00",
            current_temp_f=82.0,
            current_temp_c=27.8,
            decline_from_peak_f=3.0,
            decline_from_peak_c=1.7,
            alpha_signal="strong_post_peak",
        )
        summary = compute_hf_alpha_summary(
            peak_result=peak_result,
            metar_peak_status="past",
            use_fahrenheit=True,
        )
        assert summary["has_alpha"] is False

    def test_hf_rising_vs_metar_past(self):
        """HF shows rising while METAR says past -> secondary warming alpha."""
        peak_result = PeakDetectionResult(
            status="pre_peak",
            confidence=0.60,
            peak_temp_f=80.0,
            peak_temp_c=26.7,
            current_temp_f=81.5,
            current_temp_c=27.5,
            decline_from_peak_f=-1.5,
            decline_from_peak_c=-0.8,
            hf_trend_slope_per_min=0.05,
            hf_trend_direction="rising",
            alpha_signal="none",
        )
        summary = compute_hf_alpha_summary(
            peak_result=peak_result,
            metar_peak_status="past",
            use_fahrenheit=True,
        )
        assert summary["has_alpha"] is True
        assert summary["alpha_type"] == "hf_rising_vs_metar_past"
        assert "secondary warming" in summary["market_implication"].lower()


# ---------------------------------------------------------------------------
# ASOS source mixin tests (unit-level, no network)
# ---------------------------------------------------------------------------

class TestAsosOneMinuteSourceMixin:
    def test_icao_to_iem_station(self):
        from src.data_collection.asos_one_minute_sources import (
            _icao_to_iem_station,
            _iem_station_to_icao,
        )
        assert _icao_to_iem_station("KLGA") == "LGA"
        assert _icao_to_iem_station("KJFK") == "JFK"
        assert _icao_to_iem_station("KORD") == "ORD"
        assert _icao_to_iem_station("MMMX") == "MMMX"  # Non-US stays as-is

        assert _iem_station_to_icao("LGA") == "KLGA"
        assert _iem_station_to_icao("JFK") == "KJFK"
        assert _iem_station_to_icao("MMMX") == "MMMX"

    def test_parse_iem_csv_valid_column(self):
        """IEM CSV with 'valid' column name (older format)."""
        from src.data_collection.asos_one_minute_sources import AsosOneMinuteSourceMixin

        mixin = AsosOneMinuteSourceMixin()
        csv_text = """station,valid,tmpf,dwpf
LGA,2026-04-19 14:01,78.1,55.0
LGA,2026-04-19 14:02,78.3,55.1
LGA,2026-04-19 14:03,M,M
LGA,2026-04-19 14:04,78.5,55.2
"""
        observations = mixin._parse_iem_1min_csv(csv_text, "KLGA", -18000)
        assert len(observations) == 3  # Skips the 'M' row
        assert observations[0]["temp_f"] == 78.1
        assert observations[1]["temp_f"] == 78.3
        assert observations[2]["temp_f"] == 78.5
        # Check local time conversion (UTC-5)
        assert observations[0]["local_time"] == "09:01"

    def test_parse_iem_csv_valid_utc_column(self):
        """IEM CSV with 'valid(UTC)' column name (current format)."""
        from src.data_collection.asos_one_minute_sources import AsosOneMinuteSourceMixin

        mixin = AsosOneMinuteSourceMixin()
        csv_text = """station,station_name,valid(UTC),tmpf,dwpf
HOU,HOUSTON/WILL HOBBY,2026-04-19 14:01,78.1,55.0
HOU,HOUSTON/WILL HOBBY,2026-04-19 14:02,78.3,55.1
HOU,HOUSTON/WILL HOBBY,2026-04-19 14:03,M,M
HOU,HOUSTON/WILL HOBBY,2026-04-19 14:04,78.5,55.2
"""
        observations = mixin._parse_iem_1min_csv(csv_text, "KHOU", -21600)
        assert len(observations) == 3
        assert observations[0]["temp_f"] == 78.1
        assert observations[2]["temp_f"] == 78.5

    def test_eligible_cities(self):
        from src.data_collection.asos_one_minute_sources import AsosOneMinuteSourceMixin

        mixin = AsosOneMinuteSourceMixin()
        assert mixin._is_asos_1min_eligible("new york") is True
        assert mixin._is_asos_1min_eligible("los angeles") is True
        assert mixin._is_asos_1min_eligible("london") is False
        assert mixin._is_asos_1min_eligible("hong kong") is False

    def test_get_asos_icao(self):
        from src.data_collection.asos_one_minute_sources import AsosOneMinuteSourceMixin

        mixin = AsosOneMinuteSourceMixin()
        assert mixin._get_asos_icao("new york") == "KLGA"
        assert mixin._get_asos_icao("chicago") == "KORD"
        assert mixin._get_asos_icao("london") is None


# ---------------------------------------------------------------------------
# HF intraday (METAR/SPECI-based) tests
# ---------------------------------------------------------------------------

class TestHFIntradayICAODetection:
    def test_us_icao_detection(self):
        from src.data_collection.hf_intraday_sources import _icao_is_us
        # K-prefix CONUS stations
        assert _icao_is_us("KLGA") is True
        assert _icao_is_us("KHOU") is True
        assert _icao_is_us("KORD") is True
        # P-prefix Alaska/Hawaii stations
        assert _icao_is_us("PANC") is True  # Anchorage
        assert _icao_is_us("PHNL") is True  # Honolulu
        # Non-US
        assert _icao_is_us("EGLL") is False  # London
        assert _icao_is_us("RJTT") is False  # Tokyo
        assert _icao_is_us("MMMX") is False  # Mexico City
        assert _icao_is_us("VHHH") is False  # Hong Kong
        # Edge cases
        assert _icao_is_us("") is False
        assert _icao_is_us(None) is False
        assert _icao_is_us("K") is False  # Too short
        assert _icao_is_us("KLGAX") is False  # Too long


class TestHFIntradayTGroupParsing:
    def test_parse_positive_temp_remarks(self):
        from src.data_collection.hf_intraday_sources import (
            _parse_metar_temp_remarks,
            _parse_metar_dew_remarks,
        )

        # T01560083 -> temp=15.6 degC, dew=8.3 degC
        raw = "METAR EGLL 191530Z 24015KT 9999 BKN030 15/08 Q1015 RMK AO2 T01560083"
        assert _parse_metar_temp_remarks(raw) == 15.6
        assert _parse_metar_dew_remarks(raw) == 8.3

    def test_parse_negative_temp_remarks(self):
        from src.data_collection.hf_intraday_sources import _parse_metar_temp_remarks

        # T10150050: t sign=1 (neg), value=1.5 -> temp=-1.5 degC
        raw = "METAR KORD 191530Z 27015KT 10SM CLR M01/M05 A3015 RMK AO2 T10150050"
        assert _parse_metar_temp_remarks(raw) == -1.5

    def test_no_remarks_returns_none(self):
        from src.data_collection.hf_intraday_sources import _parse_metar_temp_remarks

        # Standard METAR with no T-group remarks
        raw = "METAR EGLL 191530Z 24015KT 9999 BKN030 15/08 Q1015"
        assert _parse_metar_temp_remarks(raw) is None

    def test_empty_string(self):
        from src.data_collection.hf_intraday_sources import _parse_metar_temp_remarks

        assert _parse_metar_temp_remarks("") is None
        assert _parse_metar_temp_remarks(None) is None

    def test_precise_temp_more_accurate_than_integer(self):
        """Verify the T-group gives better precision than the body group."""
        from src.data_collection.hf_intraday_sources import _parse_metar_temp_remarks

        # Body says 15/08, remarks say T01560083 (15.6/8.3 -> body rounds to 16)
        raw = "METAR EGLL 191530Z 24015KT 9999 BKN030 16/08 Q1015 RMK AO2 T01560083"
        precise = _parse_metar_temp_remarks(raw)
        assert precise == 15.6  # More precise than the integer body 16
