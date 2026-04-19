"""
High-Frequency Intraday Temperature Source
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Fetches the highest-frequency real-time temperature observations available
for any aerodrome.

Source priority (highest frequency first):
1. **weather.gov observations API** (5-minute resolution, US stations only, real-time)
   - URL: https://api.weather.gov/stations/{icao}/observations
   - Returns ~12 observations per hour
   - No API key required, no rate limit issues
   - Covers all US ASOS-equipped airports
   - This is the primary real-time HF feed

2. **AWC METAR API** (hourly + SPECI, worldwide, real-time) — fallback
   - Used for non-US stations (London, Tokyo, etc.)
   - Extracts 0.1°C precision temp from T-group remarks
   - SPECIs add 2-5 extra reports per hour during active weather

3. **IEM ASOS 1-minute** — handled in asos_one_minute_sources.py
   - True 1-minute resolution but has 1-2 week archive lag
   - Used only for historical analysis/backfill
"""

from __future__ import annotations

import re
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx
from loguru import logger

from src.utils.metrics import record_source_call


def _parse_metar_temp_remarks(raw_metar: str) -> Optional[float]:
    """Extract 0.1°C precision temp from METAR T-group in remarks.

    Format: T[s]TTT[s]DDD where s=0 (positive) or 1 (negative).
    Example: T01560083 -> temp=15.6°C, dew=8.3°C
    """
    if not raw_metar:
        return None
    match = re.search(r"\bT(\d)(\d{3})(\d)(\d{3})\b", raw_metar)
    if match:
        t_sign = -1 if match.group(1) == "1" else 1
        return (int(match.group(2)) / 10.0) * t_sign
    return None


def _parse_metar_dew_remarks(raw_metar: str) -> Optional[float]:
    """Extract 0.1°C precision dewpoint from METAR T-group remarks."""
    if not raw_metar:
        return None
    match = re.search(r"\bT(\d)(\d{3})(\d)(\d{3})\b", raw_metar)
    if match:
        d_sign = -1 if match.group(3) == "1" else 1
        return (int(match.group(4)) / 10.0) * d_sign
    return None


# Cached set of US ICAO codes that support weather.gov observations
_WEATHER_GOV_US_PREFIXES = ("K", "P")  # K = CONUS, P = Pacific (Alaska/Hawaii)


def _icao_is_us(icao: str) -> bool:
    """Heuristic: weather.gov supports US stations (K prefix for CONUS, P for Alaska/Hawaii)."""
    if not icao:
        return False
    return icao.strip().upper().startswith(_WEATHER_GOV_US_PREFIXES) and len(icao.strip()) == 4


class HFIntradaySourceMixin:
    """Mixin providing real-time high-frequency intraday temperature data."""

    WEATHER_GOV_OBS_URL = "https://api.weather.gov/stations/{icao}/observations"
    AWC_METAR_API_URL = "https://aviationweather.gov/api/data/metar"

    def fetch_hf_intraday(
        self,
        city: str,
        icao: str,
        use_fahrenheit: bool = False,
        utc_offset: int = 0,
        lookback_hours: int = 24,
    ) -> Optional[Dict]:
        """Fetch high-frequency intraday temperature series.

        Uses weather.gov 5-minute observations for US stations, falls back
        to AWC METAR/SPECI for international stations.

        Returns:
            Dict with observations list, peak detection inputs, and metadata.
        """
        icao = (icao or "").strip().upper()
        if not icao:
            return None

        # US stations: use weather.gov for 5-min resolution
        if _icao_is_us(icao):
            result = self._fetch_hf_weather_gov(city, icao, use_fahrenheit, utc_offset, lookback_hours)
            if result:
                return result
            logger.debug(f"weather.gov HF fetch failed for {icao}, falling back to AWC METAR")

        # Non-US or weather.gov failed: AWC METAR + SPECI
        return self._fetch_hf_awc_metar(city, icao, use_fahrenheit, utc_offset, lookback_hours)

    # ---------------------------------------------------------------------
    # Source A: weather.gov 5-minute observations (US, real-time)
    # ---------------------------------------------------------------------

    def _fetch_hf_weather_gov(
        self,
        city: str,
        icao: str,
        use_fahrenheit: bool,
        utc_offset: int,
        lookback_hours: int,
    ) -> Optional[Dict]:
        started = time.perf_counter()

        cache_key = f"hf_wgov:{icao}:{utc_offset}"
        now_ts = time.time()
        cache_ttl = getattr(self, "hf_intraday_cache_ttl_sec", 60)
        with self._hf_intraday_cache_lock:
            cached = self._hf_intraday_cache.get(cache_key)
            if cached and now_ts - cached["t"] < cache_ttl:
                record_source_call("hf_intraday", "wgov", "cache_hit", (time.perf_counter() - started) * 1000.0)
                return cached["d"]

        try:
            url = self.WEATHER_GOV_OBS_URL.format(icao=icao)
            # Fetch enough for today (288 obs/day at 5min); use limit=500 for safety
            limit = min(500, max(100, lookback_hours * 12 + 10))
            timeout = getattr(self, "hf_intraday_timeout_sec", 6.0)
            response = self._http_get(
                url,
                params={"limit": limit},
                headers={"Accept": "application/geo+json", "User-Agent": "PolyWeather/1.0"},
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            features = data.get("features") or []
            if not features:
                record_source_call("hf_intraday", "wgov", "empty", (time.perf_counter() - started) * 1000.0)
                return None

            # Filter to today in the city's local timezone
            now_utc = datetime.now(timezone.utc)
            local_now = now_utc + timedelta(seconds=utc_offset)
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            utc_midnight = local_midnight - timedelta(seconds=utc_offset)

            observations = []
            for feat in features:
                props = feat.get("properties") or {}
                ts_raw = props.get("timestamp") or ""
                temp_info = props.get("temperature") or {}
                temp_c_raw = temp_info.get("value")
                dew_info = props.get("dewpoint") or {}
                dew_c_raw = dew_info.get("value")

                if temp_c_raw is None:
                    continue
                try:
                    temp_c = float(temp_c_raw)
                except (TypeError, ValueError):
                    continue

                # Plausibility check
                if hasattr(self, "_is_plausible_metar_temp_c"):
                    if not self._is_plausible_metar_temp_c(temp_c, city, icao):
                        continue

                try:
                    utc_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if utc_dt.tzinfo is None:
                        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                if utc_dt < utc_midnight:
                    continue  # before today in local tz

                local_dt = utc_dt + timedelta(seconds=utc_offset)
                raw_metar = props.get("rawMessage") or ""
                is_speci = bool(raw_metar and raw_metar.strip().startswith("SPECI"))

                dew_c = None
                if dew_c_raw is not None:
                    try:
                        dew_c = float(dew_c_raw)
                    except (TypeError, ValueError):
                        dew_c = None

                temp_f = temp_c * 9 / 5 + 32
                dew_f = dew_c * 9 / 5 + 32 if dew_c is not None else None

                observations.append({
                    "utc_time": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "local_time": local_dt.strftime("%H:%M"),
                    "local_datetime": local_dt.strftime("%Y-%m-%dT%H:%M"),
                    "temp_c": round(temp_c, 2),
                    "temp_f": round(temp_f, 2),
                    "dwp_c": round(dew_c, 2) if dew_c is not None else None,
                    "dwp_f": round(dew_f, 2) if dew_f is not None else None,
                    "is_speci": is_speci,
                    "precision": "0.1C",
                    "raw_metar": raw_metar,
                })

            if not observations:
                record_source_call("hf_intraday", "wgov", "no_today_obs", (time.perf_counter() - started) * 1000.0)
                return None

            # Dedupe by utc_time and sort ascending
            seen = set()
            deduped = []
            for o in sorted(observations, key=lambda x: x["utc_time"]):
                if o["utc_time"] in seen:
                    continue
                seen.add(o["utc_time"])
                deduped.append(o)
            observations = deduped

            # Stats
            max_temp_c = max(o["temp_c"] for o in observations)
            max_temp_f = max(o["temp_f"] for o in observations)
            max_obs = next(o for o in observations if o["temp_c"] == max_temp_c)
            latest = observations[-1]
            speci_count = sum(1 for o in observations if o["is_speci"])

            # Compute typical cadence (median gap in minutes)
            gaps = []
            for i in range(1, len(observations)):
                t1 = datetime.fromisoformat(observations[i - 1]["utc_time"].replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(observations[i]["utc_time"].replace("Z", "+00:00"))
                gaps.append((t2 - t1).total_seconds() / 60.0)
            median_gap_min = sorted(gaps)[len(gaps) // 2] if gaps else None

            result = {
                "source": "hf_intraday",
                "source_kind": "wgov_5min",
                "icao": icao,
                "timestamp": datetime.utcnow().isoformat(),
                "observations": observations,
                "observation_count": len(observations),
                "speci_count": speci_count,
                "precise_count": len(observations),  # all wgov obs are 0.1C precise
                "median_gap_minutes": round(median_gap_min, 1) if median_gap_min is not None else None,
                "max_temp_c": round(max_temp_c, 1),
                "max_temp_f": round(max_temp_f, 1),
                "max_temp_time": max_obs["local_time"],
                "latest_temp_c": round(latest["temp_c"], 1),
                "latest_temp_f": round(latest["temp_f"], 1),
                "latest_time": latest["local_time"],
                "latest_utc_time": latest["utc_time"],
                "latest_is_speci": latest["is_speci"],
                "latest_precision": latest["precision"],
                "unit": "fahrenheit" if use_fahrenheit else "celsius",
                "max_temp": round(max_temp_f, 1) if use_fahrenheit else round(max_temp_c, 1),
                "latest_temp": round(latest["temp_f"], 1) if use_fahrenheit else round(latest["temp_c"], 1),
            }

            logger.info(
                f"HF intraday {icao} (wgov 5min): {len(observations)} obs "
                f"(median gap {median_gap_min:.1f}min), "
                f"max={max_temp_c:.1f}°C at {max_obs['local_time']}, "
                f"latest={latest['temp_c']:.1f}°C at {latest['local_time']}"
            )

            with self._hf_intraday_cache_lock:
                self._hf_intraday_cache[cache_key] = {"d": result, "t": now_ts}

            record_source_call("hf_intraday", "wgov", "success", (time.perf_counter() - started) * 1000.0)
            return result

        except httpx.HTTPError as exc:
            logger.warning(f"weather.gov HF fetch failed ({icao}): {exc}")
            record_source_call("hf_intraday", "wgov", "error", (time.perf_counter() - started) * 1000.0)
            return None
        except Exception as exc:
            logger.error(f"weather.gov HF parse error ({icao}): {exc}")
            record_source_call("hf_intraday", "wgov", "parse_error", (time.perf_counter() - started) * 1000.0)
            return None

    # ---------------------------------------------------------------------
    # Source B: AWC METAR + SPECI (worldwide fallback)
    # ---------------------------------------------------------------------

    def _fetch_hf_awc_metar(
        self,
        city: str,
        icao: str,
        use_fahrenheit: bool,
        utc_offset: int,
        lookback_hours: int,
    ) -> Optional[Dict]:
        started = time.perf_counter()

        cache_key = f"hf_awc:{icao}:{utc_offset}"
        now_ts = time.time()
        cache_ttl = getattr(self, "hf_intraday_cache_ttl_sec", 60)
        with self._hf_intraday_cache_lock:
            cached = self._hf_intraday_cache.get(cache_key)
            if cached and now_ts - cached["t"] < cache_ttl:
                record_source_call("hf_intraday", "awc", "cache_hit", (time.perf_counter() - started) * 1000.0)
                return cached["d"]

        try:
            timeout = getattr(self, "hf_intraday_timeout_sec", 5.0)
            response = self._http_get(
                self.AWC_METAR_API_URL,
                params={"ids": icao, "format": "json", "hours": lookback_hours},
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, list) or not data:
                record_source_call("hf_intraday", "awc", "empty", (time.perf_counter() - started) * 1000.0)
                return None

            now_utc = datetime.now(timezone.utc)
            local_now = now_utc + timedelta(seconds=utc_offset)
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            utc_midnight = local_midnight - timedelta(seconds=utc_offset)

            observations = []
            for obs in data:
                temp_c_metar = obs.get("temp")
                raw_metar = obs.get("rawOb") or ""
                report_time_raw = obs.get("reportTime") or ""

                precise_temp_c = _parse_metar_temp_remarks(raw_metar)
                precise_dew_c = _parse_metar_dew_remarks(raw_metar)

                temp_c = precise_temp_c if precise_temp_c is not None else temp_c_metar
                if temp_c is None:
                    continue
                dew_c = precise_dew_c if precise_dew_c is not None else obs.get("dewp")
                precision = "0.1C" if precise_temp_c is not None else "1C"

                if hasattr(self, "_is_plausible_metar_temp_c"):
                    if not self._is_plausible_metar_temp_c(temp_c, city, icao):
                        continue

                try:
                    clean = report_time_raw.replace(" ", "T")
                    if not clean.endswith("Z"):
                        clean += "Z"
                    utc_dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
                except Exception:
                    continue

                if utc_dt < utc_midnight:
                    continue

                local_dt = utc_dt + timedelta(seconds=utc_offset)
                is_speci = bool(raw_metar and raw_metar.strip().startswith("SPECI"))

                temp_f = temp_c * 9 / 5 + 32
                dew_f = dew_c * 9 / 5 + 32 if dew_c is not None else None

                observations.append({
                    "utc_time": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "local_time": local_dt.strftime("%H:%M"),
                    "local_datetime": local_dt.strftime("%Y-%m-%dT%H:%M"),
                    "temp_c": round(temp_c, 2),
                    "temp_f": round(temp_f, 2),
                    "dwp_c": round(dew_c, 2) if dew_c is not None else None,
                    "dwp_f": round(dew_f, 2) if dew_f is not None else None,
                    "is_speci": is_speci,
                    "precision": precision,
                    "raw_metar": raw_metar,
                })

            if not observations:
                record_source_call("hf_intraday", "awc", "no_today_obs", (time.perf_counter() - started) * 1000.0)
                return None

            # Dedupe and sort
            seen = set()
            deduped = []
            for o in sorted(observations, key=lambda x: x["utc_time"]):
                if o["utc_time"] in seen:
                    continue
                seen.add(o["utc_time"])
                deduped.append(o)
            observations = deduped

            max_temp_c = max(o["temp_c"] for o in observations)
            max_temp_f = max(o["temp_f"] for o in observations)
            max_obs = next(o for o in observations if o["temp_c"] == max_temp_c)
            latest = observations[-1]
            speci_count = sum(1 for o in observations if o["is_speci"])
            precise_count = sum(1 for o in observations if o["precision"] == "0.1C")

            gaps = []
            for i in range(1, len(observations)):
                t1 = datetime.fromisoformat(observations[i - 1]["utc_time"].replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(observations[i]["utc_time"].replace("Z", "+00:00"))
                gaps.append((t2 - t1).total_seconds() / 60.0)
            median_gap_min = sorted(gaps)[len(gaps) // 2] if gaps else None

            result = {
                "source": "hf_intraday",
                "source_kind": "awc_metar_speci",
                "icao": icao,
                "timestamp": datetime.utcnow().isoformat(),
                "observations": observations,
                "observation_count": len(observations),
                "speci_count": speci_count,
                "precise_count": precise_count,
                "median_gap_minutes": round(median_gap_min, 1) if median_gap_min is not None else None,
                "max_temp_c": round(max_temp_c, 1),
                "max_temp_f": round(max_temp_f, 1),
                "max_temp_time": max_obs["local_time"],
                "latest_temp_c": round(latest["temp_c"], 1),
                "latest_temp_f": round(latest["temp_f"], 1),
                "latest_time": latest["local_time"],
                "latest_utc_time": latest["utc_time"],
                "latest_is_speci": latest["is_speci"],
                "latest_precision": latest["precision"],
                "unit": "fahrenheit" if use_fahrenheit else "celsius",
                "max_temp": round(max_temp_f, 1) if use_fahrenheit else round(max_temp_c, 1),
                "latest_temp": round(latest["temp_f"], 1) if use_fahrenheit else round(latest["temp_c"], 1),
            }

            logger.info(
                f"HF intraday {icao} (AWC METAR): {len(observations)} obs "
                f"({speci_count} SPECI, {precise_count} precise, median gap {median_gap_min}min), "
                f"max={max_temp_c:.1f}°C at {max_obs['local_time']}, "
                f"latest={latest['temp_c']:.1f}°C at {latest['local_time']}"
            )

            with self._hf_intraday_cache_lock:
                self._hf_intraday_cache[cache_key] = {"d": result, "t": now_ts}

            record_source_call("hf_intraday", "awc", "success", (time.perf_counter() - started) * 1000.0)
            return result

        except httpx.HTTPError as exc:
            logger.error(f"AWC HF METAR fetch failed ({icao}): {exc}")
            with self._hf_intraday_cache_lock:
                stale = self._hf_intraday_cache.get(cache_key)
                if stale:
                    record_source_call("hf_intraday", "awc", "stale_cache", (time.perf_counter() - started) * 1000.0)
                    return stale["d"]
            record_source_call("hf_intraday", "awc", "error", (time.perf_counter() - started) * 1000.0)
            return None
        except Exception as exc:
            logger.error(f"AWC HF METAR parse error ({icao}): {exc}")
            record_source_call("hf_intraday", "awc", "parse_error", (time.perf_counter() - started) * 1000.0)
            return None
