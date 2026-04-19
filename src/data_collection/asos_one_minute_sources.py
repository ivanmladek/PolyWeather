"""
ASOS 1-Minute High-Frequency Temperature Data Source
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Fetches minute-by-minute temperature observations from the Iowa Environmental
Mesonet (IEM) ASOS 1-minute data archive for US ASOS-equipped aerodromes.

This gives us ~900 US stations with true 1-minute sensor readings at 0.1°F
precision — far ahead of the standard hourly/half-hourly METAR cadence.

API docs: https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py?help
"""

from __future__ import annotations

import csv
import io
import os
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from src.utils.metrics import record_source_call


# ICAO codes for all US ASOS stations tracked in PolyWeather.
# IEM uses FAA LID (the ICAO code minus the leading 'K' for CONUS stations).
_US_ASOS_ICAO_CODES = {
    "KLGA", "KJFK", "KEWR",   # New York area
    "KLAX", "KBUR", "KLGB", "KSNA", "KVNY",  # Los Angeles area
    "KSFO", "KOAK", "KSJC",   # San Francisco area
    "KBKF", "KDEN", "KAPA",   # Denver / Aurora area
    "KAUS", "KEDC",            # Austin area
    "KHOU", "KIAH",            # Houston area
    "KORD", "KMDW",            # Chicago area
    "KDAL", "KDFW",            # Dallas area
    "KMIA", "KOPF",            # Miami area
    "KATL", "KPDK",            # Atlanta area
    "KSEA", "KBFI",            # Seattle area
    "MMMX",                    # Mexico City (non-US but IEM may not have it)
}


def _icao_to_iem_station(icao: str) -> str:
    """Convert ICAO code to IEM station identifier.

    For CONUS stations the FAA LID is the ICAO with the leading 'K' removed.
    IEM expects the 3-letter FAA LID for the 1-minute endpoint.
    """
    icao = icao.strip().upper()
    if icao.startswith("K") and len(icao) == 4:
        return icao[1:]  # KLGA -> LGA
    return icao


def _iem_station_to_icao(station: str) -> str:
    """Convert IEM 3-letter FAA station ID back to ICAO."""
    station = station.strip().upper()
    if len(station) == 3 and station.isalpha():
        return f"K{station}"
    return station


class AsosOneMinuteSourceMixin:
    """Mixin providing ASOS 1-minute temperature data via IEM."""

    # ---------------------------------------------------------------------------
    # Station mapping: maps city keys to their ASOS ICAO codes
    # Only US cities with ASOS stations are eligible.
    # ---------------------------------------------------------------------------
    ASOS_1MIN_STATIONS: Dict[str, str] = {
        "new york": "KLGA",
        "los angeles": "KLAX",
        "san francisco": "KSFO",
        "aurora": "KBKF",
        "austin": "KAUS",
        "houston": "KHOU",
        "chicago": "KORD",
        "dallas": "KDAL",
        "miami": "KMIA",
        "atlanta": "KATL",
        "seattle": "KSEA",
    }

    # Additional nearby ASOS stations per city for redundancy / cross-validation.
    ASOS_1MIN_CLUSTER: Dict[str, List[str]] = {
        "new york": ["KLGA", "KJFK", "KEWR"],
        "los angeles": ["KLAX", "KBUR", "KVNY"],
        "san francisco": ["KSFO", "KOAK", "KSJC"],
        "aurora": ["KBKF", "KDEN", "KAPA"],
        "austin": ["KAUS"],
        "houston": ["KHOU", "KIAH"],
        "chicago": ["KORD", "KMDW"],
        "dallas": ["KDAL", "KDFW"],
        "miami": ["KMIA", "KOPF"],
        "atlanta": ["KATL", "KPDK"],
        "seattle": ["KSEA", "KBFI"],
    }

    IEM_ASOS_1MIN_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"

    def _is_asos_1min_eligible(self, city: str) -> bool:
        """Return True if this city has an ASOS 1-minute data source."""
        return city.lower().strip() in self.ASOS_1MIN_STATIONS

    def _get_asos_icao(self, city: str) -> Optional[str]:
        """Get the primary ASOS ICAO for a city."""
        return self.ASOS_1MIN_STATIONS.get(city.lower().strip())

    def fetch_asos_1min(
        self,
        city: str,
        use_fahrenheit: bool = True,
        utc_offset: int = 0,
        lookback_hours: int = 0,
    ) -> Optional[Dict]:
        """Fetch ASOS 1-minute temperature data for a US city.

        Fetches today's data from local midnight to now (in the city's timezone).
        Returns a rich dict with the full minute-by-minute temperature series
        plus computed peak detection signals.

        Args:
            city: City key (e.g. "new york")
            use_fahrenheit: If True, return temps in °F (native ASOS format)
            utc_offset: City timezone offset in seconds from UTC
            lookback_hours: If >0, fetch this many hours of data instead of
                          today-from-midnight. 0 means from local midnight.

        Returns:
            Dict with keys: source, icao, observations (list of {time, temp_f, temp_c, dwp_f, dwp_c}),
            max_temp, max_temp_time, latest_temp, latest_time, observation_count, etc.
            None if not eligible or fetch fails.
        """
        started = time.perf_counter()
        normalized = city.lower().strip()
        icao = self.ASOS_1MIN_STATIONS.get(normalized)
        if not icao:
            record_source_call("asos_1min", "current", "not_eligible", (time.perf_counter() - started) * 1000.0)
            return None

        station = _icao_to_iem_station(icao)

        # Cache check
        cache_key = f"asos1min:{icao}:{utc_offset}"
        now_ts = time.time()
        cache_ttl = getattr(self, "asos_1min_cache_ttl_sec", 60)
        with self._asos_1min_cache_lock:
            cached = self._asos_1min_cache.get(cache_key)
            if cached and now_ts - cached["t"] < cache_ttl:
                logger.debug(f"ASOS 1-min cache hit {icao} age={int(now_ts - cached['t'])}s")
                record_source_call("asos_1min", "current", "cache_hit", (time.perf_counter() - started) * 1000.0)
                return cached["d"]

        # Compute time range: from local midnight to now (UTC)
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc + timedelta(seconds=utc_offset)

        if lookback_hours > 0:
            start_utc = now_utc - timedelta(hours=lookback_hours)
        else:
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_utc = local_midnight - timedelta(seconds=utc_offset)

        end_utc = now_utc + timedelta(minutes=5)  # small buffer

        params = {
            "station": station,
            "tz": "UTC",
            "year1": start_utc.year,
            "month1": start_utc.month,
            "day1": start_utc.day,
            "hour1": start_utc.hour,
            "minute1": start_utc.minute,
            "year2": end_utc.year,
            "month2": end_utc.month,
            "day2": end_utc.day,
            "hour2": end_utc.hour,
            "minute2": end_utc.minute,
            "vars": ["tmpf", "dwpf"],
            "sample": "1min",
            "what": "download",
            "delim": "comma",
        }

        try:
            timeout = getattr(self, "asos_1min_timeout_sec", 6.0)
            response = self._http_get(
                self.IEM_ASOS_1MIN_URL,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            text = response.text

            observations = self._parse_iem_1min_csv(text, icao, utc_offset)

            if not observations:
                logger.warning(f"ASOS 1-min {icao}: no observations returned")
                record_source_call("asos_1min", "current", "empty", (time.perf_counter() - started) * 1000.0)
                return None

            # Compute derived fields
            max_temp_f = max(o["temp_f"] for o in observations)
            max_temp_c = max(o["temp_c"] for o in observations)
            max_temp_time = next(
                o["local_time"] for o in observations if o["temp_f"] == max_temp_f
            )
            latest = observations[-1]

            result = {
                "source": "asos_1min",
                "icao": icao,
                "station": station,
                "timestamp": datetime.utcnow().isoformat(),
                "observations": observations,
                "observation_count": len(observations),
                "time_range_minutes": len(observations),
                "max_temp_f": round(max_temp_f, 1),
                "max_temp_c": round(max_temp_c, 1),
                "max_temp_time": max_temp_time,
                "latest_temp_f": round(latest["temp_f"], 1),
                "latest_temp_c": round(latest["temp_c"], 1),
                "latest_time": latest["local_time"],
                "latest_utc_time": latest["utc_time"],
                "unit": "fahrenheit" if use_fahrenheit else "celsius",
            }

            # Convert output temps if needed
            if use_fahrenheit:
                result["max_temp"] = result["max_temp_f"]
                result["latest_temp"] = result["latest_temp_f"]
            else:
                result["max_temp"] = result["max_temp_c"]
                result["latest_temp"] = result["latest_temp_c"]

            logger.info(
                f"ASOS 1-min {icao}: {len(observations)} obs, "
                f"max={result['max_temp_f']:.1f}°F at {max_temp_time}, "
                f"latest={result['latest_temp_f']:.1f}°F at {latest['local_time']}"
            )

            with self._asos_1min_cache_lock:
                self._asos_1min_cache[cache_key] = {"d": result, "t": now_ts}

            record_source_call("asos_1min", "current", "success", (time.perf_counter() - started) * 1000.0)
            return result

        except httpx.HTTPError as exc:
            logger.error(f"ASOS 1-min request failed ({icao}): {exc}")
            with self._asos_1min_cache_lock:
                stale = self._asos_1min_cache.get(cache_key)
                if stale:
                    logger.warning(f"ASOS 1-min {icao} failed, using stale cache")
                    record_source_call("asos_1min", "current", "stale_cache", (time.perf_counter() - started) * 1000.0)
                    return stale["d"]
            record_source_call("asos_1min", "current", "error", (time.perf_counter() - started) * 1000.0)
            return None
        except Exception as exc:
            logger.error(f"ASOS 1-min parse error ({icao}): {exc}")
            record_source_call("asos_1min", "current", "parse_error", (time.perf_counter() - started) * 1000.0)
            return None

    def fetch_asos_1min_cluster(
        self,
        city: str,
        use_fahrenheit: bool = True,
        utc_offset: int = 0,
    ) -> List[Dict]:
        """Fetch ASOS 1-minute data for all stations in a city's cluster.

        Returns a list of result dicts (one per station), useful for
        cross-validation of peak detection signals.
        """
        normalized = city.lower().strip()
        cluster = self.ASOS_1MIN_CLUSTER.get(normalized, [])
        results = []

        for cluster_icao in cluster:
            # Temporarily set the station for this city
            original = self.ASOS_1MIN_STATIONS.get(normalized)
            self.ASOS_1MIN_STATIONS[normalized] = cluster_icao
            try:
                result = self.fetch_asos_1min(city, use_fahrenheit, utc_offset)
                if result:
                    results.append(result)
            finally:
                if original:
                    self.ASOS_1MIN_STATIONS[normalized] = original
                elif normalized in self.ASOS_1MIN_STATIONS:
                    del self.ASOS_1MIN_STATIONS[normalized]

        return results

    def _parse_iem_1min_csv(
        self, csv_text: str, icao: str, utc_offset: int
    ) -> List[Dict]:
        """Parse IEM ASOS 1-minute CSV response into observation dicts.

        IEM CSV columns: station,station_name,valid(UTC),tmpf,dwpf
        valid(UTC) is UTC timestamp like: 2026-04-19 14:01
        tmpf/dwpf are in Fahrenheit, may be 'M' for missing.
        """
        observations = []
        reader = csv.DictReader(io.StringIO(csv_text))

        for row in reader:
            try:
                tmpf_raw = (row.get("tmpf") or "").strip()
                dwpf_raw = (row.get("dwpf") or "").strip()
                # Handle both column name variants: 'valid' and 'valid(UTC)'
                valid_raw = (
                    row.get("valid(UTC)")
                    or row.get("valid")
                    or ""
                ).strip()

                # Skip missing values
                if tmpf_raw in ("M", "", "None") or not valid_raw:
                    continue

                temp_f = float(tmpf_raw)
                dwp_f = float(dwpf_raw) if dwpf_raw not in ("M", "", "None") else None
                temp_c = round((temp_f - 32) * 5 / 9, 2)
                dwp_c = round((dwp_f - 32) * 5 / 9, 2) if dwp_f is not None else None

                # Parse UTC time
                utc_dt = datetime.strptime(valid_raw, "%Y-%m-%d %H:%M")
                utc_dt = utc_dt.replace(tzinfo=timezone.utc)
                local_dt = utc_dt + timedelta(seconds=utc_offset)

                observations.append({
                    "utc_time": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "local_time": local_dt.strftime("%H:%M"),
                    "local_datetime": local_dt.strftime("%Y-%m-%dT%H:%M"),
                    "temp_f": temp_f,
                    "temp_c": temp_c,
                    "dwp_f": dwp_f,
                    "dwp_c": dwp_c,
                })
            except (ValueError, KeyError) as exc:
                continue

        # Sort by UTC time ascending
        observations.sort(key=lambda o: o["utc_time"])
        return observations
