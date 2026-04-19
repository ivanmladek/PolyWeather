"""
High-Frequency Peak Detection Engine
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Uses minute-by-minute ASOS temperature data to detect whether a station has
passed its daily temperature peak, BEFORE the next hourly METAR confirms it.

This is the core alpha signal: if we can detect "post-peak" with 1-minute data
while the market is still pricing off the last hourly METAR, we have an
informational edge of up to 59 minutes.

Detection methods:
1. **Sustained Decline**: Temperature falls for N consecutive minutes below
   the intraday max by a meaningful margin.
2. **Rolling Window Reversal**: The slope of a rolling linear regression over
   the last M minutes turns negative.
3. **Peak Plateau + Break**: Temperature plateaus (stagnates) then breaks
   downward, indicating the peak has formed.
4. **Time-of-Day Prior**: Bayesian adjustment using the expected peak window
   from Open-Meteo hourly profile — if we're past the expected peak hour AND
   temperature is declining, confidence is much higher.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


@dataclass
class PeakDetectionResult:
    """Result of high-frequency peak detection analysis."""

    # Core signal
    status: str  # "pre_peak", "at_peak", "post_peak", "uncertain", "insufficient_data"
    confidence: float  # 0.0 to 1.0

    # Detected peak
    peak_temp_f: Optional[float] = None
    peak_temp_c: Optional[float] = None
    peak_time: Optional[str] = None  # local time HH:MM
    peak_utc_time: Optional[str] = None

    # Time since peak
    minutes_since_peak: Optional[int] = None

    # Current state
    current_temp_f: Optional[float] = None
    current_temp_c: Optional[float] = None
    decline_from_peak_f: Optional[float] = None
    decline_from_peak_c: Optional[float] = None

    # Trend analysis from HF data
    hf_trend_slope_per_min: Optional[float] = None  # °F/min over last window
    hf_trend_direction: str = "unknown"  # "rising", "falling", "stagnant", "plateau_break"

    # Alpha signal
    alpha_signal: str = "none"  # "strong_post_peak", "likely_post_peak", "possible_post_peak", "none"
    alpha_minutes_ahead: Optional[int] = None  # estimated minutes of alpha vs next METAR

    # Evidence
    evidence: List[str] = field(default_factory=list)

    # Raw statistics
    observation_count: int = 0
    max_temp_f: Optional[float] = None
    latest_temp_f: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "peak_temp_f": self.peak_temp_f,
            "peak_temp_c": self.peak_temp_c,
            "peak_time": self.peak_time,
            "peak_utc_time": self.peak_utc_time,
            "minutes_since_peak": self.minutes_since_peak,
            "current_temp_f": self.current_temp_f,
            "current_temp_c": self.current_temp_c,
            "decline_from_peak_f": self.decline_from_peak_f,
            "decline_from_peak_c": self.decline_from_peak_c,
            "hf_trend_slope_per_min": self.hf_trend_slope_per_min,
            "hf_trend_direction": self.hf_trend_direction,
            "alpha_signal": self.alpha_signal,
            "alpha_minutes_ahead": self.alpha_minutes_ahead,
            "evidence": self.evidence,
            "observation_count": self.observation_count,
            "max_temp_f": self.max_temp_f,
            "latest_temp_f": self.latest_temp_f,
        }


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
# Minimum observations needed before we attempt peak detection
MIN_OBSERVATIONS = 30  # 30 minutes of data minimum

# Sustained decline: require this many consecutive minutes of decline
SUSTAINED_DECLINE_MINUTES = 15

# Minimum decline from peak to declare "post-peak" (°F)
MIN_DECLINE_THRESHOLD_F = 1.0

# Strong decline threshold (°F)
STRONG_DECLINE_THRESHOLD_F = 2.0

# Rolling regression window (minutes)
REGRESSION_WINDOW = 20

# Plateau detection: max variance in °F over window to be "plateau"
PLATEAU_MAX_RANGE_F = 0.5
PLATEAU_MIN_MINUTES = 10

# After plateau, decline this much to confirm break (°F)
PLATEAU_BREAK_THRESHOLD_F = 0.8

# Time-of-day confidence boost when past expected peak window
TOD_PAST_PEAK_CONFIDENCE_BOOST = 0.2

# Maximum minutes of alpha we can claim (cap for display)
MAX_ALPHA_MINUTES = 59


def detect_peak(
    observations: List[Dict],
    expected_peak_start_hour: int = 13,
    expected_peak_end_hour: int = 15,
    local_hour_frac: float = 12.0,
) -> PeakDetectionResult:
    """Analyze 1-minute observations to detect post-peak temperature status.

    Args:
        observations: List of dicts with keys:
            - temp_f (float): Temperature in Fahrenheit
            - temp_c (float): Temperature in Celsius
            - local_time (str): HH:MM local time
            - utc_time (str): ISO UTC time
        expected_peak_start_hour: Expected peak window start (from Open-Meteo)
        expected_peak_end_hour: Expected peak window end
        local_hour_frac: Current local time as fractional hour (e.g. 14.5 = 2:30 PM)

    Returns:
        PeakDetectionResult with status, confidence, and alpha signal.
    """
    if not observations or len(observations) < MIN_OBSERVATIONS:
        return PeakDetectionResult(
            status="insufficient_data",
            confidence=0.0,
            observation_count=len(observations) if observations else 0,
            evidence=["Insufficient 1-minute data for peak detection"],
        )

    temps_f = [o["temp_f"] for o in observations]
    temps_c = [o["temp_c"] for o in observations]
    n = len(temps_f)

    # Find the peak
    max_temp_f = max(temps_f)
    max_temp_c = max(temps_c)
    peak_idx = 0
    for i, t in enumerate(temps_f):
        if t == max_temp_f:
            peak_idx = i  # last occurrence of max

    peak_obs = observations[peak_idx]
    latest_obs = observations[-1]
    current_temp_f = latest_obs["temp_f"]
    current_temp_c = latest_obs["temp_c"]

    decline_f = max_temp_f - current_temp_f
    decline_c = max_temp_c - current_temp_c
    minutes_since_peak = n - 1 - peak_idx

    evidence = []
    confidence = 0.0

    # ----- Method 1: Sustained Decline -----
    sustained_decline = _check_sustained_decline(temps_f, peak_idx)
    if sustained_decline["is_declining"]:
        confidence += 0.30
        evidence.append(
            f"Sustained decline: {sustained_decline['consecutive_decline_minutes']}min "
            f"of falling temps ({sustained_decline['decline_amount_f']:.1f}°F)"
        )

    # ----- Method 2: Rolling Regression Slope -----
    slope = _rolling_regression_slope(temps_f, window=REGRESSION_WINDOW)
    if slope is not None:
        if slope < -0.05:  # losing >0.05°F/min = 3°F/hour
            confidence += 0.25
            evidence.append(f"Strong negative slope: {slope:.3f}°F/min ({slope*60:.1f}°F/hr)")
        elif slope < -0.02:  # >1.2°F/hour decline
            confidence += 0.15
            evidence.append(f"Moderate negative slope: {slope:.3f}°F/min ({slope*60:.1f}°F/hr)")
        elif slope > 0.02:
            confidence -= 0.10
            evidence.append(f"Still rising: slope={slope:.3f}°F/min ({slope*60:.1f}°F/hr)")

    # ----- Method 3: Plateau + Break -----
    plateau_break = _check_plateau_break(temps_f, peak_idx)
    if plateau_break["is_broken"]:
        confidence += 0.20
        evidence.append(
            f"Plateau break detected: plateau of {plateau_break['plateau_minutes']}min "
            f"then dropped {plateau_break['break_amount_f']:.1f}°F"
        )

    # ----- Method 4: Time-of-Day Prior -----
    if local_hour_frac > expected_peak_end_hour:
        confidence += TOD_PAST_PEAK_CONFIDENCE_BOOST
        evidence.append(
            f"Past expected peak window ({expected_peak_start_hour}:00-{expected_peak_end_hour}:00)"
        )
    elif local_hour_frac < expected_peak_start_hour:
        confidence -= 0.15
        evidence.append(
            f"Before expected peak window ({expected_peak_start_hour}:00-{expected_peak_end_hour}:00)"
        )

    # ----- Method 5: Magnitude of decline from peak -----
    if decline_f >= STRONG_DECLINE_THRESHOLD_F:
        confidence += 0.15
        evidence.append(f"Significant decline from peak: {decline_f:.1f}°F")
    elif decline_f >= MIN_DECLINE_THRESHOLD_F:
        confidence += 0.08
        evidence.append(f"Moderate decline from peak: {decline_f:.1f}°F")
    elif decline_f < 0.3:
        confidence -= 0.10
        evidence.append(f"Minimal decline from peak: {decline_f:.1f}°F (may still be at peak)")

    # ----- Method 6: Minutes since peak -----
    if minutes_since_peak >= 30:
        confidence += 0.10
        evidence.append(f"Peak was {minutes_since_peak}min ago")
    elif minutes_since_peak >= 15:
        confidence += 0.05
        evidence.append(f"Peak was {minutes_since_peak}min ago")

    # Clamp confidence
    confidence = max(0.0, min(1.0, confidence))

    # Determine HF trend direction
    if slope is not None:
        if slope > 0.02:
            hf_trend_direction = "rising"
        elif slope < -0.02:
            hf_trend_direction = "falling"
        elif plateau_break["is_broken"]:
            hf_trend_direction = "plateau_break"
        else:
            hf_trend_direction = "stagnant"
    else:
        hf_trend_direction = "unknown"

    # Determine peak status
    if confidence >= 0.65:
        status = "post_peak"
    elif confidence >= 0.40:
        status = "post_peak" if decline_f >= MIN_DECLINE_THRESHOLD_F else "at_peak"
    elif confidence >= 0.20 and decline_f >= MIN_DECLINE_THRESHOLD_F:
        status = "at_peak"
    elif slope is not None and slope > 0.01:
        status = "pre_peak"
    else:
        status = "uncertain"

    # Determine alpha signal
    if status == "post_peak" and confidence >= 0.70:
        alpha_signal = "strong_post_peak"
    elif status == "post_peak" and confidence >= 0.50:
        alpha_signal = "likely_post_peak"
    elif status == "post_peak" or (status == "at_peak" and decline_f >= STRONG_DECLINE_THRESHOLD_F):
        alpha_signal = "possible_post_peak"
    else:
        alpha_signal = "none"

    # Estimate alpha minutes ahead of next METAR
    # Standard METAR comes at :50 or :00 of each hour
    alpha_minutes_ahead = None
    if alpha_signal != "none":
        # Next METAR is at the next :50 or :00
        current_minute = int((local_hour_frac % 1) * 60)
        if current_minute < 50:
            alpha_minutes_ahead = min(50 - current_minute, MAX_ALPHA_MINUTES)
        else:
            alpha_minutes_ahead = min(60 - current_minute + 50, MAX_ALPHA_MINUTES)

    return PeakDetectionResult(
        status=status,
        confidence=confidence,
        peak_temp_f=round(max_temp_f, 1),
        peak_temp_c=round(max_temp_c, 1),
        peak_time=peak_obs.get("local_time"),
        peak_utc_time=peak_obs.get("utc_time"),
        minutes_since_peak=minutes_since_peak,
        current_temp_f=round(current_temp_f, 1),
        current_temp_c=round(current_temp_c, 1),
        decline_from_peak_f=round(decline_f, 1),
        decline_from_peak_c=round(decline_c, 1),
        hf_trend_slope_per_min=round(slope, 4) if slope is not None else None,
        hf_trend_direction=hf_trend_direction,
        alpha_signal=alpha_signal,
        alpha_minutes_ahead=alpha_minutes_ahead,
        evidence=evidence,
        observation_count=n,
        max_temp_f=round(max_temp_f, 1),
        latest_temp_f=round(current_temp_f, 1),
    )


def _check_sustained_decline(temps: List[float], peak_idx: int) -> Dict:
    """Check if temperature has been declining sustainedly since peak.

    Looks at the tail of the series (after peak) and counts consecutive
    minutes where each reading is <= the previous one.
    """
    if peak_idx >= len(temps) - 1:
        return {"is_declining": False, "consecutive_decline_minutes": 0, "decline_amount_f": 0.0}

    post_peak = temps[peak_idx:]
    consecutive = 0
    max_consecutive = 0
    current_run_start = 0

    for i in range(1, len(post_peak)):
        if post_peak[i] <= post_peak[i - 1]:
            consecutive += 1
            max_consecutive = max(max_consecutive, consecutive)
        else:
            consecutive = 0

    # Also check overall decline
    total_decline = post_peak[0] - post_peak[-1]

    is_declining = (
        max_consecutive >= SUSTAINED_DECLINE_MINUTES
        and total_decline >= MIN_DECLINE_THRESHOLD_F
    )

    return {
        "is_declining": is_declining,
        "consecutive_decline_minutes": max_consecutive,
        "decline_amount_f": round(total_decline, 1),
    }


def _rolling_regression_slope(temps: List[float], window: int = 20) -> Optional[float]:
    """Compute the slope of a simple linear regression over the last `window` observations.

    Returns slope in °F/minute. Negative = cooling, positive = warming.
    """
    if len(temps) < window:
        if len(temps) >= 10:
            window = len(temps)
        else:
            return None

    recent = temps[-window:]
    n = len(recent)
    x_mean = (n - 1) / 2
    y_mean = sum(recent) / n

    numerator = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(recent))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        return 0.0

    return numerator / denominator


def _check_plateau_break(temps: List[float], peak_idx: int) -> Dict:
    """Detect a plateau near the peak followed by a downward break.

    A plateau is a period where temperature varies by less than PLATEAU_MAX_RANGE_F
    for at least PLATEAU_MIN_MINUTES. A break is when temperature drops more than
    PLATEAU_BREAK_THRESHOLD_F below the plateau level.
    """
    if peak_idx >= len(temps) - PLATEAU_MIN_MINUTES:
        return {"is_broken": False, "plateau_minutes": 0, "break_amount_f": 0.0}

    # Look at data from peak onwards
    post_peak = temps[peak_idx:]

    # Find the plateau: a run of readings with small range
    best_plateau_end = 0
    plateau_level = post_peak[0]

    for end in range(PLATEAU_MIN_MINUTES, len(post_peak)):
        window = post_peak[:end]
        window_range = max(window) - min(window)
        if window_range <= PLATEAU_MAX_RANGE_F:
            best_plateau_end = end
            plateau_level = sum(window) / len(window)
        else:
            break

    if best_plateau_end < PLATEAU_MIN_MINUTES:
        return {"is_broken": False, "plateau_minutes": 0, "break_amount_f": 0.0}

    # Check if there's a break after the plateau
    if best_plateau_end < len(post_peak):
        post_plateau = post_peak[best_plateau_end:]
        if post_plateau:
            min_after_plateau = min(post_plateau)
            break_amount = plateau_level - min_after_plateau
            is_broken = break_amount >= PLATEAU_BREAK_THRESHOLD_F

            return {
                "is_broken": is_broken,
                "plateau_minutes": best_plateau_end,
                "break_amount_f": round(break_amount, 1),
            }

    return {"is_broken": False, "plateau_minutes": best_plateau_end, "break_amount_f": 0.0}


def compute_hf_alpha_summary(
    peak_result: PeakDetectionResult,
    metar_peak_status: str,
    deb_prediction: Optional[float] = None,
    use_fahrenheit: bool = True,
) -> Dict:
    """Compute an alpha summary comparing HF peak detection vs METAR-based peak status.

    This is the key alpha function: it tells us when HF data shows post-peak
    while METAR still thinks we're at peak or pre-peak.

    Args:
        peak_result: HF peak detection result
        metar_peak_status: Current peak_status from METAR/trend_engine ("before", "in_window", "past")
        deb_prediction: DEB blended forecast (for comparing peak vs forecast)
        use_fahrenheit: Temperature unit

    Returns:
        Dict with alpha assessment:
            - has_alpha: bool (True if HF gives us info METAR doesn't)
            - alpha_type: str (description of alpha edge)
            - alpha_confidence: float (0-1)
            - alpha_minutes: int (estimated minutes ahead)
            - market_implication: str (what this means for Polymarket)
            - hf_status: str (HF peak detection status)
            - metar_status: str (METAR-based peak status)
    """
    temp_symbol = "°F" if use_fahrenheit else "°C"
    peak_temp = peak_result.peak_temp_f if use_fahrenheit else peak_result.peak_temp_c
    current_temp = peak_result.current_temp_f if use_fahrenheit else peak_result.current_temp_c
    decline = peak_result.decline_from_peak_f if use_fahrenheit else peak_result.decline_from_peak_c

    result = {
        "has_alpha": False,
        "alpha_type": "none",
        "alpha_confidence": 0.0,
        "alpha_minutes": 0,
        "market_implication": "",
        "hf_status": peak_result.status,
        "metar_status": metar_peak_status,
        "hf_confidence": peak_result.confidence,
        "peak_temp": peak_temp,
        "current_temp": current_temp,
        "decline": decline,
    }

    # Case 1: HF says post-peak, METAR still says in_window or before
    # This is the strongest alpha signal.
    if peak_result.status == "post_peak" and metar_peak_status in ("before", "in_window"):
        result["has_alpha"] = True
        result["alpha_confidence"] = peak_result.confidence
        result["alpha_minutes"] = peak_result.alpha_minutes_ahead or 0

        if metar_peak_status == "before":
            result["alpha_type"] = "hf_post_peak_vs_metar_before"
            result["market_implication"] = (
                f"1-min data shows peak ALREADY reached at {peak_result.peak_time} "
                f"({peak_temp}{temp_symbol}) and declining (now {current_temp}{temp_symbol}, "
                f"-{decline}{temp_symbol}). METAR still pre-peak. "
                f"Market may be overpricing upside."
            )
        else:
            result["alpha_type"] = "hf_post_peak_vs_metar_in_window"
            if deb_prediction is not None and peak_temp is not None:
                if peak_temp < deb_prediction:
                    miss = deb_prediction - peak_temp
                    result["market_implication"] = (
                        f"1-min data shows peak already passed at {peak_result.peak_time} "
                        f"({peak_temp}{temp_symbol}) — {miss:.1f}{temp_symbol} below DEB forecast "
                        f"of {deb_prediction}{temp_symbol}. Temperature declining "
                        f"({decline}{temp_symbol} off peak). Daily high likely locked in below forecast."
                    )
                else:
                    result["market_implication"] = (
                        f"1-min data confirms peak reached at {peak_result.peak_time} "
                        f"({peak_temp}{temp_symbol}) and now declining ({decline}{temp_symbol} off peak). "
                        f"Daily high appears locked in."
                    )
            else:
                result["market_implication"] = (
                    f"1-min data shows peak passed at {peak_result.peak_time} "
                    f"({peak_temp}{temp_symbol}), declining {decline}{temp_symbol}. "
                    f"Daily high likely locked in."
                )

    # Case 2: HF says at_peak, METAR says before
    elif peak_result.status == "at_peak" and metar_peak_status == "before":
        result["has_alpha"] = True
        result["alpha_type"] = "hf_at_peak_vs_metar_before"
        result["alpha_confidence"] = peak_result.confidence * 0.7
        result["alpha_minutes"] = peak_result.alpha_minutes_ahead or 0
        result["market_implication"] = (
            f"1-min data suggests approaching/at peak ({peak_temp}{temp_symbol} "
            f"at {peak_result.peak_time}). METAR still pre-peak. Watch for confirmed break."
        )

    # Case 3: HF says pre_peak, METAR says past
    # This is unusual but could happen if there's a secondary warming
    elif peak_result.status == "pre_peak" and metar_peak_status == "past":
        result["has_alpha"] = True
        result["alpha_type"] = "hf_rising_vs_metar_past"
        result["alpha_confidence"] = peak_result.confidence * 0.5
        result["alpha_minutes"] = peak_result.alpha_minutes_ahead or 0
        result["market_implication"] = (
            f"Unexpected: 1-min data shows temperature still RISING "
            f"(slope={peak_result.hf_trend_slope_per_min:.3f}{temp_symbol}/min) "
            f"while METAR trend says past peak. Possible secondary warming — "
            f"daily high may not be locked in yet."
        )

    return result
