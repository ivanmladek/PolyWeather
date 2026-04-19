"""
AWC METAR Cache — High-Frequency Worldwide METAR/SPECI Feed
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Aviation Weather Center (AWC) maintains a gzipped CSV cache of ALL current
METAR/SPECI reports worldwide, updated every minute. This gives us near-real-time
access to SPECI (special weather observations) which are triggered by significant
changes — effectively providing sub-hourly temperature data at international
airports when weather is changing.

Cache URL: https://aviationweather.gov/data/cache/metars.cache.csv.gz

This complements the IEM ASOS 1-minute data (US only) by giving us SPECI-triggered
observations for international airports, which can arrive every few minutes during
rapid weather changes (exactly when alpha matters most).
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import re
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from src.utils.metrics import record_source_call


AWC_METAR_CACHE_URL = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"


def _parse_metar_temp_remarks(raw_metar: str) -> Optional[float]:
    """Extract 0.1°C precision temperature from METAR remarks group.

    The T-group in remarks (e.g., T01560083) gives temperature and dew point
    at 0.1°C resolution, much better than the integer body group.

    Format: T[s]TTT[s]DDD where s=0 (positive) or 1 (negative), TTT/DDD in 0.1°C
    Example: T01560083 → temp=15.6°C, dew=8.3°C
    """
    match = re.search(r"\bT(\d)(\d{3})(\d)(\d{3})\b", raw_metar)
    if match:
        t_sign = -1 if match.group(1) == "1" else 1
        t_value = int(match.group(2)) / 10.0 * t_sign
        return t_value
    return None


class AwcMetarCacheSourceMixin:
    """Mixin providing worldwide METAR/SPECI data from the AWC 1-minute cache.

    This is designed to work alongside standard METAR fetching to capture
    SPECI reports between standard hourly METARs, providing sub-hourly
    temperature updates for international airports.
    """

    def fetch_awc_metar_cache_for_icao(
        self,
        icao: str,
        utc_offset: int = 0,
    ) -> Optional[Dict]:
        """Fetch the latest METAR/SPECI from the AWC cache for a specific station.

        This is most useful for:
        1. Detecting SPECI reports between hourly METARs
        2. Getting the precise T-group temperature (0.1°C) from remarks
        3. Tracking observation frequency (more SPECIs = more active weather)

        Args:
            icao: ICAO code (e.g., "EGLL" for Heathrow)
            utc_offset: Timezone offset in seconds from UTC

        Returns:
            Dict with current observation data, or None.
        """
        started = time.perf_counter()
        icao = icao.strip().upper()

        # Check cache
        cache_key = f"awc_cache:{icao}"
        now_ts = time.time()
        cache_ttl = getattr(self, "awc_metar_cache_ttl_sec", 60)
        with self._awc_metar_cache_lock:
            cached = self._awc_metar_cache.get(cache_key)
            if cached and now_ts - cached["t"] < cache_ttl:
                record_source_call("awc_cache", "current", "cache_hit", (time.perf_counter() - started) * 1000.0)
                return cached["d"]

        # Fetch from AWC API (JSON, simpler than parsing gzipped CSV)
        try:
            url = "https://aviationweather.gov/api/data/metar"
            response = self._http_get(
                url,
                params={"ids": icao, "format": "json", "hours": 3},
                timeout=getattr(self, "awc_cache_timeout_sec", 4.0),
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or not data:
                record_source_call("awc_cache", "current", "empty", (time.perf_counter() - started) * 1000.0)
                return None

            # Process all observations (METARs + SPECIs)
            observations = []
            for obs in data:
                temp_c = obs.get("temp")
                raw_metar = obs.get("rawOb", "")
                report_time = obs.get("reportTime", "")

                if temp_c is None:
                    continue

                # Try to get high-precision temp from remarks
                precise_temp_c = _parse_metar_temp_remarks(raw_metar) if raw_metar else None
                best_temp_c = precise_temp_c if precise_temp_c is not None else temp_c
                temp_precision = "0.1C" if precise_temp_c is not None else "1C"

                # Determine if this is a SPECI
                is_speci = bool(raw_metar and raw_metar.strip().startswith("SPECI"))

                # Parse time
                try:
                    clean_rt = report_time.replace(" ", "T")
                    if not clean_rt.endswith("Z"):
                        clean_rt += "Z"
                    utc_dt = datetime.fromisoformat(clean_rt.replace("Z", "+00:00"))
                    local_dt = utc_dt + timedelta(seconds=utc_offset)
                    local_time = local_dt.strftime("%H:%M")
                    utc_time = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    local_time = ""
                    utc_time = report_time

                observations.append({
                    "utc_time": utc_time,
                    "local_time": local_time,
                    "temp_c": round(best_temp_c, 1),
                    "temp_f": round(best_temp_c * 9 / 5 + 32, 1),
                    "temp_precision": temp_precision,
                    "is_speci": is_speci,
                    "raw_metar": raw_metar,
                    "wind_speed_kt": obs.get("wspd"),
                    "wind_dir": obs.get("wdir"),
                    "visibility_mi": obs.get("visib"),
                })

            if not observations:
                record_source_call("awc_cache", "current", "no_temp", (time.perf_counter() - started) * 1000.0)
                return None

            # Sort newest first
            observations.sort(key=lambda o: o["utc_time"], reverse=True)

            # Count SPECIs
            speci_count = sum(1 for o in observations if o["is_speci"])
            total_count = len(observations)
            latest = observations[0]

            # Find max temp today
            now_utc = datetime.now(timezone.utc)
            local_now = now_utc + timedelta(seconds=utc_offset)
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            utc_midnight = local_midnight - timedelta(seconds=utc_offset)

            max_temp_c_today = -999.0
            max_temp_time = None
            for obs_item in observations:
                try:
                    obs_utc = datetime.fromisoformat(obs_item["utc_time"].replace("Z", "+00:00"))
                    if obs_utc >= utc_midnight and obs_item["temp_c"] > max_temp_c_today:
                        max_temp_c_today = obs_item["temp_c"]
                        max_temp_time = obs_item["local_time"]
                except Exception:
                    continue

            result = {
                "source": "awc_metar_cache",
                "icao": icao,
                "latest_temp_c": latest["temp_c"],
                "latest_temp_f": latest["temp_f"],
                "latest_time": latest["local_time"],
                "latest_precision": latest["temp_precision"],
                "latest_is_speci": latest["is_speci"],
                "max_temp_c_today": max_temp_c_today if max_temp_c_today > -900 else None,
                "max_temp_f_today": round(max_temp_c_today * 9 / 5 + 32, 1) if max_temp_c_today > -900 else None,
                "max_temp_time": max_temp_time,
                "observation_count": total_count,
                "speci_count": speci_count,
                "observation_frequency": "high" if speci_count >= 3 else ("normal" if total_count >= 3 else "low"),
                "observations": observations[:12],  # Last 12 reports
            }

            logger.info(
                f"AWC cache {icao}: {total_count} obs ({speci_count} SPECI), "
                f"latest={latest['temp_c']:.1f}°C ({latest['temp_precision']}) at {latest['local_time']}"
            )

            with self._awc_metar_cache_lock:
                self._awc_metar_cache[cache_key] = {"d": result, "t": now_ts}

            record_source_call("awc_cache", "current", "success", (time.perf_counter() - started) * 1000.0)
            return result

        except httpx.HTTPError as exc:
            logger.error(f"AWC METAR cache fetch failed ({icao}): {exc}")
            with self._awc_metar_cache_lock:
                stale = self._awc_metar_cache.get(cache_key)
                if stale:
                    record_source_call("awc_cache", "current", "stale_cache", (time.perf_counter() - started) * 1000.0)
                    return stale["d"]
            record_source_call("awc_cache", "current", "error", (time.perf_counter() - started) * 1000.0)
            return None
        except Exception as exc:
            logger.error(f"AWC METAR cache parse error ({icao}): {exc}")
            record_source_call("awc_cache", "current", "parse_error", (time.perf_counter() - started) * 1000.0)
            return None
