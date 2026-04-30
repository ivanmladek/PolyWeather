"""
HF Elimination Arbitrage — GCP Pub/Sub Serverless Service

Architecture:
  Cloud Scheduler (2min) → POST /publish → weather.gov 5-min → Pub/Sub
  Pub/Sub push           → POST /        → N=2 confirmed bucket check → @postpeak_elim

Requires 2 CONSECUTIVE HF readings above the bucket upper bound (zero margin)
before firing an alert. This filters single-spike false positives while still
catching real eliminations within one extra 2-min cycle.

Reuses existing modules (DRY):
  - src.analysis.elimination_arbitrage.bucket_upper_bound
  - src.data_collection.polymarket_readonly.PolymarketReadOnlyLayer
  - src.data_collection.city_registry.CITY_REGISTRY
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Project root on sys.path for DRY imports from src/
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import flask
import httpx
from google.cloud import firestore, pubsub_v1
from loguru import logger

from src.data_collection.city_registry import CITY_REGISTRY
from src.data_collection.polymarket_readonly import PolymarketReadOnlyLayer
from src.analysis.elimination_arbitrage import bucket_upper_bound

# Require N consecutive HF readings above bucket_upper (zero margin) to confirm.
CONFIRM_N = int(os.getenv("ELIM_CONFIRM_N", "2"))

# ---------------------------------------------------------------------------
# Config (all from env — Cloud Run runtime config or Secret Manager)
# ---------------------------------------------------------------------------

GCP_PROJECT = os.environ.get("GCP_PROJECT_ID", "")
TOPIC = os.getenv("PUBSUB_TOPIC", "hf-weather-obs")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("POSTPEAK_ELIM_CHAT_ID", "")

# NO price threshold: alert when NO price is BELOW this (i.e. there's edge)
MAX_NO_PRICE = float(os.getenv("ELIM_MAX_NO_PRICE", "0.98"))

# LLM gate — Anthropic (primary) or Groq (fallback)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
LLM_MODEL_ANTHROPIC = os.getenv("ELIM_LLM_MODEL", "claude-sonnet-4-20250514")
LLM_MODEL_GROQ = os.getenv("ELIM_LLM_MODEL_GROQ", "llama-3.3-70b-versatile")

# US cities eligible for 5-min weather.gov HF feed
US_CITIES = {
    k: v for k, v in CITY_REGISTRY.items()
    if v.get("use_fahrenheit") and v.get("icao", "").startswith(("K", "P"))
}

app = flask.Flask(__name__)
_pub: pubsub_v1.PublisherClient | None = None
_mkt = PolymarketReadOnlyLayer()
_fs: firestore.Client | None = None

FIRESTORE_DB = os.getenv("FIRESTORE_DATABASE", "elim-dedup")


def _firestore() -> firestore.Client:
    """Lazy-init Firestore client for alert dedup."""
    global _fs
    if _fs is None:
        _fs = firestore.Client(database=FIRESTORE_DB)
    return _fs


def _dedup_key(city: str, date: str, label: str) -> str:
    """Deterministic doc ID: one alert per bucket per city per day."""
    return f"{city}:{date}:{label}"


def _already_sent(city: str, date: str, label: str) -> bool:
    """Check Firestore if we already alerted for this bucket-city-date."""
    try:
        doc = _firestore().collection("elim_sent").document(_dedup_key(city, date, label)).get()
        return doc.exists
    except Exception as e:
        logger.warning(f"Firestore read failed (allowing alert): {e}")
        return False  # fail-open: send alert if dedup is down


def _mark_sent(city: str, date: str, label: str, temp: float, edge: float):
    """Record that we sent an alert for this bucket-city-date."""
    try:
        _firestore().collection("elim_sent").document(_dedup_key(city, date, label)).set({
            "city": city,
            "date": date,
            "bucket": label,
            "temp_f": temp,
            "edge_pct": edge,
            "sent_at": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        logger.warning(f"Firestore write failed: {e}")


def _get_prev_temps(city: str, date: str) -> list[float]:
    """Get the last CONFIRM_N-1 temps from Firestore for this city+date."""
    try:
        doc = _firestore().collection("prev_obs").document(f"{city}:{date}").get()
        if doc.exists:
            return doc.to_dict().get("temps", [])
    except Exception as e:
        logger.warning(f"Firestore prev_obs read failed: {e}")
    return []


def _push_temp(city: str, date: str, temp_f: float):
    """Append temp to the rolling window, keep last CONFIRM_N entries."""
    try:
        doc_ref = _firestore().collection("prev_obs").document(f"{city}:{date}")
        doc = doc_ref.get()
        temps = doc.to_dict().get("temps", []) if doc.exists else []
        temps.append(temp_f)
        temps = temps[-(CONFIRM_N):]  # keep last N
        doc_ref.set({"city": city, "date": date, "temps": temps})
    except Exception as e:
        logger.warning(f"Firestore prev_obs write failed: {e}")


def _publisher() -> pubsub_v1.PublisherClient:
    global _pub
    if _pub is None:
        _pub = pubsub_v1.PublisherClient()
    return _pub


# ---------------------------------------------------------------------------
# PUBLISHER — Cloud Scheduler hits every 2 min
# ---------------------------------------------------------------------------

@app.route("/publish", methods=["POST"])
def publish():
    """Fetch latest HF observation per US city, publish to Pub/Sub."""
    pub = _publisher()
    topic = pub.topic_path(GCP_PROJECT, TOPIC)
    futures = []
    for city_key, info in US_CITIES.items():
        obs = _fetch_latest_obs(info["icao"])
        if not obs:
            continue
        msg = json.dumps({
            "city": city_key,
            "icao": info["icao"],
            "temp_c": obs["temp_c"],
            "temp_f": obs["temp_f"],
            "time": obs["time"],
        }).encode()
        futures.append((city_key, pub.publish(topic, msg)))

    # Await all publish futures — surfaces permission/network errors
    published = 0
    for city_key, future in futures:
        try:
            future.result(timeout=10)
            published += 1
        except Exception as e:
            logger.error(f"Pub/Sub publish failed for {city_key}: {e}")

    logger.info(f"Published {published}/{len(US_CITIES)} observations")
    return flask.jsonify({"ok": True, "published": published}), 200


def _fetch_latest_obs(icao: str) -> dict | None:
    """Fetch latest valid temp from weather.gov 5-min observations (US only)."""
    try:
        r = httpx.get(
            f"https://api.weather.gov/stations/{icao}/observations",
            params={"limit": 10},
            headers={
                "Accept": "application/geo+json",
                "User-Agent": "PolyWeather-Elim/1.0",
            },
            timeout=8,
        )
        r.raise_for_status()
        for feat in r.json().get("features") or []:
            props = feat.get("properties") or {}
            tc = (props.get("temperature") or {}).get("value")
            if tc is not None:
                tc = float(tc)
                return {
                    "temp_c": round(tc, 2),
                    "temp_f": round(tc * 9 / 5 + 32, 2),
                    "time": props.get("timestamp"),
                }
    except Exception as e:
        logger.warning(f"HF fetch {icao}: {e}")
    return None


# ---------------------------------------------------------------------------
# PROCESSOR — Pub/Sub push delivers each observation
# ---------------------------------------------------------------------------

@app.route("/", methods=["POST"])
def process():
    """Pub/Sub push handler. One message = one city's latest HF observation.

    N=2 CONFIRMED elimination with ZERO margin:
      1. Decode observation (city, temp_f)
      2. Load previous temp(s) from Firestore, append current, save
      3. Fetch Polymarket bucket ladder with live prices
      4. For each bucket: check if ALL last N temps > bucket_upper (no margin)
      5. If confirmed AND NO < 98% → alert (with Firestore dedup)

    This filters single-spike false positives (Atlanta 84.2°F one-off)
    while catching real sustained crossings within one extra 2-min cycle.
    """
    envelope = flask.request.get_json(silent=True) or {}
    raw = envelope.get("message", {}).get("data")
    if not raw:
        return "", 204

    obs = json.loads(base64.b64decode(raw))
    city = obs["city"]
    temp_f = obs["temp_f"]
    icao = obs["icao"]

    # Use the CITY'S LOCAL DATE — not UTC.
    tz_offset = CITY_REGISTRY.get(city, {}).get("tz_offset", 0)
    local_now = datetime.now(timezone.utc) + timedelta(seconds=tz_offset)
    today = local_now.strftime("%Y-%m-%d")

    # --- Track temps for N-confirmation ---
    prev_temps = _get_prev_temps(city, today)
    _push_temp(city, today, temp_f)
    window = prev_temps + [temp_f]  # all temps including current
    window = window[-(CONFIRM_N):]  # last N

    if len(window) < CONFIRM_N:
        return flask.jsonify({
            "action": "skip", "reason": "warming_up",
            "city": city, "temp_f": temp_f, "window_size": len(window),
        }), 200

    # --- Fetch Polymarket bucket ladder (live prices) ---
    buckets = _get_bucket_ladder(city, today)
    if not buckets:
        return flask.jsonify({"action": "skip", "reason": "no_buckets"}), 200

    # --- Check EVERY bucket: N consecutive readings > bucket_upper? ---
    alerts = []
    for bucket in buckets:
        direction = str(bucket.get("direction") or "exact")
        bucket_temp = bucket.get("temp") or bucket.get("value")
        if bucket_temp is None:
            continue

        try:
            upper = bucket_upper_bound(city, float(bucket_temp), direction)
        except Exception:
            continue
        if upper is None:
            continue  # "above" direction — not eliminatable

        # N=2 confirmation: ALL readings in the window must exceed upper (zero margin)
        if not all(t > upper for t in window):
            continue

        # Get NO price — skip if missing or already ≥ 98c (no edge)
        no_buy = bucket.get("no_buy")
        if no_buy is None:
            continue
        no_buy = float(no_buy)
        if no_buy >= MAX_NO_PRICE:
            continue

        edge_pct = round((1.0 - no_buy) * 100.0, 2)
        liq = bucket.get("liquidity")

        alerts.append({
            "label": bucket.get("label") or bucket.get("slug") or "?",
            "slug": bucket.get("slug"),
            "bucket_temp": bucket_temp,
            "direction": direction,
            "bucket_upper": upper,
            "no_price": no_buy,
            "edge_pct": edge_pct,
            "liquidity": liq,
        })

    # --- Dedup: only alert once per bucket-city-date ---
    new_alerts = [a for a in alerts if not _already_sent(city, today, a["label"])]

    # --- LLM gate: ask Claude/Groq for YES/NO on each new alert ---
    approved = []
    for a in new_alerts:
        prompt = _build_llm_context(city, today, temp_f, obs, a, window, buckets)
        llm = _call_llm_gate(prompt)

        if llm is None:
            # No LLM key or call failed — auto-approve (fail-open)
            a["llm_action"] = "AUTO_APPROVED"
            a["llm_reasoning"] = "LLM gate unavailable — auto-approved"
            a["llm_confidence"] = "n/a"
            approved.append(a)
        elif str(llm.get("action", "")).upper().startswith("BUY"):
            a["llm_action"] = llm.get("action", "BUY_NO")
            a["llm_reasoning"] = llm.get("reasoning", "")
            a["llm_confidence"] = llm.get("confidence", "?")
            a["llm_risk_factors"] = llm.get("risk_factors", [])
            approved.append(a)
        else:
            # LLM said SKIP
            logger.info(
                f"LLM SKIP {city} {a['label']}: {llm.get('reasoning', '?')}"
            )

    if approved:
        _send_telegram_alert(city, temp_f, obs, approved, window)
        for a in approved:
            _mark_sent(city, today, a["label"], temp_f, a["edge_pct"])
        logger.info(
            f"ELIM {city}: {temp_f}°F → {len(approved)}/{len(new_alerts)} approved by LLM "
            f"(N={CONFIRM_N} confirmed): "
            + ", ".join(a["label"] for a in approved)
        )
    elif new_alerts:
        # LLM rejected all — still mark as sent to avoid re-evaluation
        for a in new_alerts:
            _mark_sent(city, today, a["label"], temp_f, a["edge_pct"])
        logger.info(
            f"ELIM {city}: {temp_f}°F → {len(new_alerts)} crossing(s) REJECTED by LLM"
        )
    elif alerts:
        logger.debug(
            f"ELIM {city}: {temp_f}°F → {len(alerts)} crossing(s) already sent today"
        )

    return flask.jsonify({
        "action": "alert" if approved else (
            "llm_skip" if new_alerts else ("dedup" if alerts else "no_alert")),
        "city": city,
        "temp_f": temp_f,
        "window": window,
        "buckets_checked": len(buckets),
        "crossings": len(alerts),
        "new_alerts": len(new_alerts),
        "llm_approved": len(approved),
    }), 200


# ---------------------------------------------------------------------------
# LLM gate — asks Claude/Groq whether the elimination is trustworthy
# ---------------------------------------------------------------------------

_ELIM_SYSTEM_PROMPT = """\
You are a temperature elimination checker for Polymarket weather derivatives.

You receive a signal: N consecutive 5-min weather.gov readings show a US city's
temperature has crossed above a bucket's upper bound. Your ONLY job is to sanity-check
the TEMPERATURE DATA. Is the temperature OBVIOUSLY and CLEARLY above the bucket?

IMPORTANT CONTEXT:
- METAR and weather.gov ALWAYS report in whole Celsius (e.g. 28°C, 29°C, 30°C).
  This is NORMAL, not a red flag. Every single reading will be a whole Celsius number.
- The Fahrenheit equivalents land at: ...82.4, 84.2, 86.0, 87.8, 89.6... (1.8°F steps)
- These are the ONLY values you will ever see. Do NOT reject readings just because
  they are at Celsius whole numbers — that is how ALL weather stations report.

YOUR DECISION FRAMEWORK — approve (BUY_NO) unless:
1. The HF temp is the SAME Celsius value as the bucket upper when rounded.
   Example: bucket upper 83.49°F, temp 84.2°F = 29°C. Settlement rounds 84.2 → 84.
   This is ABOVE 83 — APPROVE.
   Example: bucket upper 87.49°F, temp 87.8°F = 31°C. Settlement rounds 87.8 → 88.
   BUT the official METAR max might report 87°F if station logged 30.6°C at peak.
   Check the METAR readings provided — if METAR confirms ≥ the Celsius threshold, APPROVE.
2. The temp and bucket are separated by LESS than one full Celsius step (1.8°F) AND
   the METAR hourly observation does NOT confirm the crossing. This is the ONLY case
   to SKIP — when HF 5-min shows a borderline crossing that METAR hasn't confirmed.

DO NOT consider liquidity, edge %, or NO price. Those are filtered elsewhere.
DO NOT reject based on time of day.
Your ONLY concern: is the temperature clearly above the bucket based on HF + METAR data?

Default to BUY_NO. Only SKIP if genuinely ambiguous temperatures.

Respond ONLY with valid JSON:
{
  "action": "BUY_NO" | "SKIP",
  "confidence": "high" | "medium" | "low",
  "reasoning": "<1-2 sentences, temperature-focused only>"
}
"""


def _fetch_recent_obs(icao: str, limit: int = 12) -> list[dict]:
    """Fetch last N observations for LLM context (HF 5-min + METAR hourly)."""
    try:
        r = httpx.get(
            f"https://api.weather.gov/stations/{icao}/observations",
            params={"limit": limit},
            headers={
                "Accept": "application/geo+json",
                "User-Agent": "PolyWeather-Elim/1.0",
            },
            timeout=8,
        )
        r.raise_for_status()
        out = []
        for feat in r.json().get("features") or []:
            props = feat.get("properties") or {}
            tc = (props.get("temperature") or {}).get("value")
            ts = (props.get("timestamp") or "")[:19]
            if tc is not None:
                tc = float(tc)
                out.append({
                    "time": ts,
                    "temp_c": tc,
                    "temp_f": round(tc * 9 / 5 + 32, 2),
                })
        out.reverse()  # chronological order
        return out
    except Exception:
        return []


def _build_llm_context(city: str, today: str, temp_f: float, obs: dict,
                       alert: dict, window: list[float], all_buckets: list) -> str:
    """Build the user prompt with HF trend + METAR data for LLM evaluation."""
    info = CITY_REGISTRY.get(city, {})
    name = info.get("name", city)
    icao = obs.get("icao", "?")
    obs_time = (obs.get("time") or "?")[:19]
    upper = alert["bucket_upper"]
    margin = round(temp_f - upper, 2)

    # Fetch last 12 readings for trend context
    recent = _fetch_recent_obs(icao, limit=12)
    trend_lines = []
    max_in_window = None
    for r in recent:
        marker = ""
        if r["temp_f"] > upper:
            marker = " > ABOVE bucket upper"
        trend_lines.append(
            f"  {r['time'][11:]}  {r['temp_c']:5.1f}°C = {r['temp_f']:5.1f}°F{marker}"
        )
        if max_in_window is None or r["temp_f"] > max_in_window:
            max_in_window = r["temp_f"]

    max_c = round((max_in_window - 32) * 5 / 9, 1) if max_in_window else "?"

    return f"""ELIMINATION CHECK — {name.upper()} ({icao})
Date: {today} (local)

TARGET BUCKET:
  Label: {alert['label']}
  Bucket upper bound: {upper}°F
  Current HF temp: {temp_f}°F ({obs.get('temp_c', '?')}°C)
  Margin above upper: {margin}°F
  N={CONFIRM_N} confirmation window: {' → '.join(f'{t:.1f}' for t in window)}°F

LAST 12 HF OBSERVATIONS (5-min cadence from weather.gov):
{chr(10).join(trend_lines) if trend_lines else '  (unavailable)'}

MAX IN THIS WINDOW: {max_in_window or '?'}°F ({max_c}°C)

SETTLEMENT ROUNDING: wu_round (half-up) on Fahrenheit.
  {temp_f}°F → settles as {round(temp_f + 0.5):.0f}°F (if this is the daily max)
  Bucket {alert['label']} upper = {upper}°F → any max ≥ {upper + 0.01:.2f}°F exits this bucket.

QUESTION: Based on the HF readings above, is the temperature CLEARLY above {upper}°F?
Look at the trend — is it sustained or a single blip?
"""


def _call_llm_gate(prompt: str) -> dict | None:
    """Call LLM for elimination gate decision. Anthropic primary, Groq fallback."""
    if ANTHROPIC_API_KEY:
        return _call_anthropic_gate(prompt)
    if GROQ_API_KEY:
        return _call_groq_gate(prompt)
    logger.debug("No LLM API key — auto-approving elimination")
    return None  # no key = skip gate, auto-approve


def _call_anthropic_gate(prompt: str) -> dict | None:
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": LLM_MODEL_ANTHROPIC,
                "max_tokens": 400,
                "system": _ELIM_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt
                              + "\n\nRespond ONLY with the JSON object, no markdown fences."}],
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        text_blocks = [b["text"] for b in resp.json().get("content", [])
                       if b.get("type") == "text"]
        raw = "".join(text_blocks).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Anthropic gate call failed: {e}")
        return None


def _call_groq_gate(prompt: str) -> dict | None:
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL_GROQ,
                "temperature": 0.1,
                "max_tokens": 400,
                "messages": [
                    {"role": "system", "content": _ELIM_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        content = (((resp.json().get("choices") or [{}])[0])
                   .get("message") or {}).get("content", "")
        return json.loads(content)
    except Exception as e:
        logger.warning(f"Groq gate call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_bucket_ladder(city: str, target_date: str) -> list:
    """Get full bucket ladder with live prices from Polymarket."""
    try:
        scan = _mkt.build_market_scan(city=city, target_date=target_date)
        return scan.get("all_buckets") or []
    except Exception as e:
        logger.warning(f"Polymarket scan failed for {city}: {e}")
        return []


def _send_telegram_alert(city: str, temp: float, obs: dict, alerts: list,
                         window: list[float] | None = None):
    """Fire elimination alert to @postpeak_elim for confirmed crossings."""
    if not (TG_TOKEN and TG_CHAT):
        logger.warning("Telegram not configured — alert suppressed")
        return

    name = CITY_REGISTRY.get(city, {}).get("name", city)
    obs_time = (obs.get("time") or "?")[:16]
    window_str = " → ".join(f"{t:.1f}" for t in (window or [temp]))
    lines = [
        f"<b>[ELIM] {name.upper()}</b>",
        f"HF <b>{temp}°F</b> @ {obs_time} ({obs['icao']})",
        f"Confirmed: {CONFIRM_N} readings [{window_str}]",
        "",
    ]
    for a in alerts:
        lines.append(
            f"BUY_NO <b>{a['label']}</b> @ {a['no_price'] * 100:.1f}c  "
            f"edge={a['edge_pct']:.1f}%  liq=${(a['liquidity'] or 0):,.0f}"
        )
        if a.get("slug"):
            lines.append(f"  polymarket.com/market/{a['slug']}")
        # LLM reasoning
        llm_r = a.get("llm_reasoning")
        llm_c = a.get("llm_confidence")
        if llm_r:
            lines.append(f"  LLM [{llm_c}]: {llm_r}")

    lines.append(
        f"\n<i>{CONFIRM_N}x HF > bucket upper (zero margin); "
        f"daily max cannot retreat.</i>"
    )

    try:
        httpx.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
