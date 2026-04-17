"""
scan_alpha.py — Alpha scanner for PolyWeather temperature markets.

Scans all 48 cities via localhost API, compares model probability buckets
against live Polymarket prices, sends candidates to an LLM for buy/sell
decisions, and pushes genuine BUY YES signals to Telegram.

Usage:
    # One-shot scan
    python scripts/scan_alpha.py

    # Dry run (no Telegram push)
    python scripts/scan_alpha.py --dry-run

    # Loop mode — re-scans every 15 minutes (aligned with METAR updates)
    python scripts/scan_alpha.py --loop

    # Loop with custom interval
    python scripts/scan_alpha.py --loop --interval 600

    # Override bankroll
    python scripts/scan_alpha.py --bankroll 500
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_root / ".env")

LOCAL_API = os.getenv("POLYWEATHER_ALPHA_API_URL", "http://127.0.0.1:8000")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("POLYWEATHER_ALPHA_LLM_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("POLYWEATHER_ALPHA_ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [
    c.strip()
    for c in (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID") or "").split(",")
    if c.strip()
]
# Dedicated channel for high-conviction post-peak speed-alpha signals only
POSTPEAK_CHAT_ID = os.getenv("POSTPEAK_CHAT_ID", "").strip()

import csv  # noqa: E402
import httpx  # noqa: E402

LOG_DIR = _root / "data" / "alpha_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SCAN_LOG = LOG_DIR / "scans.jsonl"       # every scan cycle
SIGNAL_LOG = LOG_DIR / "signals.jsonl"   # every LLM decision (BUY + SKIP)
TRADE_LOG = LOG_DIR / "trades.csv"       # BUY YES only — for P&L backtesting

MIN_EDGE_PCT = 0.1  # report anything with edge > 0.1%
LLM_TOP_N = 8  # send top N candidates to LLM
DEFAULT_BANKROLL = 1000.0
DEFAULT_LOOP_INTERVAL = 900  # 15 minutes
SIGNAL_COOLDOWN_SEC = 3600  # don't re-push the same city+bucket within 1 hour

# ---- Backtest-derived entry windows ----
# Data: 142 city-days, Apr 13-16 2026
# Bucket accuracy: 18% at 3-5h, 37.5% at 1-2h, 44.6% at 0-1h, 66% post-peak
# Strategy: TWO entry modes
#   MODE 1 "golden hour": 1-3h before peak, sigma tightening, METAR rising
#   MODE 2 "post-peak capture": 0-2h after peak, max_so_far is the settlement,
#          market hasn't updated yet
GOLDEN_HOUR_MIN = 1.0   # hours before peak start
GOLDEN_HOUR_MAX = 3.0   # hours before peak end
POST_PEAK_MAX = 2.0     # hours after peak to scan for value capture
MAX_SIGMA_GOLDEN = 1.5  # sigma must be below this for golden hour
MIN_TOP2_PROB = 0.40    # top-2 buckets must cover >= 40% combined probability

# Speed-alpha filter: post-peak, bucket confirmed by observation, market 5-70%
# Backtest: 14/16 = 87.5% win rate in this window (Apr 15-17 2026)
SPEED_ALPHA_MKT_MIN = 0.05   # below 5% = likely bucket-market mismatch
SPEED_ALPHA_MKT_MAX = 0.70   # above 70% = market already priced in, no alpha
SPEED_ALPHA_SIGMA_MAX = 1.5  # model must have converged
SPEED_ALPHA_LIQ_MIN = 200    # minimum liquidity for execution (keep permissive for now)

# Tracks recently pushed signals to avoid spamming
_signal_cooldown: dict[str, float] = {}

# ---------------------------------------------------------------------------
# Static UTC peak hours per city (typical daily high window in UTC)
# Used for fast pre-filtering before any API calls.
# Format: (utc_peak_start, utc_peak_end) — the hours when daily high typically occurs
# ---------------------------------------------------------------------------
CITY_UTC_PEAK: dict[str, tuple[int, int]] = {
    # APAC (+7 to +12)
    "wellington":    (0, 4),     # 12:00-16:00 NZST (UTC+12)
    "tokyo":         (5, 7),     # 14:00-16:00 JST (UTC+9)
    "seoul":         (3, 4),     # 12:00-13:00 KST (UTC+9)
    "busan":         (4, 5),     # 13:00-14:00 KST (UTC+9)
    "taipei":        (4, 5),     # 12:00-13:00 CST (UTC+8)
    "hong kong":     (5, 7),     # 13:00-15:00 HKT (UTC+8)
    "lau fau shan":  (5, 7),     # 13:00-15:00 HKT (UTC+8)
    "shanghai":      (5, 6),     # 13:00-14:00 CST (UTC+8)
    "shenzhen":      (5, 9),     # 13:00-17:00 CST (UTC+8)
    "beijing":       (8, 9),     # 16:00-17:00 CST (UTC+8)
    "chengdu":       (9, 10),    # 17:00-18:00 CST (UTC+8)
    "chongqing":     (9, 10),    # 17:00-18:00 CST (UTC+8)
    "wuhan":         (7, 8),     # 15:00-16:00 CST (UTC+8)
    "singapore":     (6, 8),     # 14:00-16:00 SGT (UTC+8)
    "kuala lumpur":  (6, 6),     # 14:00 MYT (UTC+8)
    "jakarta":       (6, 6),     # 13:00 WIB (UTC+7)
    # South Asia + Middle East
    "lucknow":       (8, 9),     # 14:00 IST (UTC+5.5)
    "jeddah":        (10, 11),   # 13:00-14:00 AST (UTC+3)
    "tel aviv":      (11, 12),   # 14:00-15:00 IDT (UTC+3)
    # Europe + Africa
    "moscow":        (9, 10),    # 12:00-13:00 MSK (UTC+3)
    "helsinki":      (11, 14),   # 14:00-17:00 EEST (UTC+3)
    "istanbul":      (11, 12),   # 14:00-15:00 TRT (UTC+3)
    "ankara":        (12, 14),   # 15:00-17:00 TRT (UTC+3)
    "warsaw":        (13, 13),   # 15:00 CEST (UTC+2)
    "amsterdam":     (13, 14),   # 15:00-16:00 CEST (UTC+2)
    "paris":         (14, 16),   # 16:00-18:00 CEST (UTC+2)
    "munich":        (14, 14),   # 16:00 CEST (UTC+2)
    "milan":         (15, 15),   # 17:00 CEST (UTC+2)
    "madrid":        (15, 17),   # 17:00-19:00 CEST (UTC+2)
    "london":        (14, 15),   # 15:00-16:00 BST (UTC+1)
    "lagos":         (13, 14),   # 14:00-15:00 WAT (UTC+1)
    "cape town":     (12, 12),   # 14:00 SAST (UTC+2)
    # Americas
    "buenos aires":  (17, 18),   # 14:00-15:00 ART (UTC-3)
    "sao paulo":     (17, 19),   # 14:00-16:00 BRT (UTC-3)
    "panama city":   (19, 19),   # 14:00 EST (UTC-5)
    "mexico city":   (20, 22),   # 14:00-16:00 CDT (UTC-6, DST)
    "toronto":       (18, 18),   # 14:00 EDT (UTC-4)
    "new york":      (19, 19),   # 15:00 EDT (UTC-4)
    "miami":         (17, 17),   # 13:00 EDT (UTC-4)
    "atlanta":       (21, 21),   # 17:00 EDT (UTC-4)
    "chicago":       (22, 22),   # 17:00 CDT (UTC-5)
    "houston":       (21, 21),   # 16:00 CDT (UTC-5)
    "austin":        (21, 23),   # 16:00-18:00 CDT (UTC-5)
    "dallas":        (23, 23),   # 18:00 CDT (UTC-5)
    "denver":        (19, 19),   # 13:00 MDT (UTC-6)
    "los angeles":   (19, 22),   # 12:00-15:00 PDT (UTC-7)
    "san francisco": (20, 20),   # 13:00 PDT (UTC-7)
    "seattle":       (22, 1),    # 15:00-18:00 PDT (UTC-7)
}


def _city_hours_to_peak_utc(city_name: str) -> float | None:
    """Return hours from now (UTC) to the city's typical peak start. None if unknown."""
    entry = CITY_UTC_PEAK.get(city_name.lower())
    if not entry:
        return None
    peak_start, peak_end = entry
    now_h = datetime.now(timezone.utc).hour + datetime.now(timezone.utc).minute / 60.0
    # Handle wrap-around (e.g. Seattle peak_end=1 meaning 01:00 next day)
    if peak_start <= peak_end:
        diff = peak_start - now_h
    else:
        # Wraps midnight
        if now_h >= peak_start:
            diff = 0  # we're in the window
        else:
            diff = peak_start - now_h
    if diff < -12:
        diff += 24  # next day
    return diff

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_get(path: str, timeout: float = 20.0):
    r = requests.get(f"{LOCAL_API}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def _log_jsonl(path: Path, record: dict) -> None:
    """Append a JSON record to a JSONL file."""
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"  [WARN] Failed to write log {path.name}: {e}")


def _log_trade_csv(record: dict) -> None:
    """Append a trade record to the CSV trade log for P&L tracking."""
    file_exists = TRADE_LOG.exists()
    fieldnames = [
        "timestamp_utc", "city", "date", "entry_mode",
        "bucket", "adjacent_bucket", "model_probability", "market_price",
        "edge_pct", "confidence", "size_pct", "size_usd", "bankroll",
        "sigma", "metar_rising", "max_so_far", "deb_prediction",
        "mu", "top1_prob", "top2_prob", "hours_to_peak",
        "market_slug", "market_question", "liquidity", "volume",
        "reasoning", "risk_factors",
        # filled in later by settlement checker:
        "actual_high", "settlement_bucket", "trade_won", "pnl_usd",
    ]
    try:
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(record)
    except Exception as e:
        print(f"  [WARN] Failed to write trade log: {e}")


def _is_on_cooldown(city: str, bucket: int | None, date: str) -> bool:
    key = f"{city}|{bucket}|{date}"
    last = _signal_cooldown.get(key, 0)
    return (time.time() - last) < SIGNAL_COOLDOWN_SEC


def _mark_pushed(city: str, bucket: int | None, date: str) -> None:
    key = f"{city}|{bucket}|{date}"
    _signal_cooldown[key] = time.time()


def _send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
        except Exception as e:
            print(f"  [WARN] Telegram send failed for {chat_id}: {e}")


def _send_telegram_photo(photo_url: str, caption: str) -> None:
    """Send a photo (by URL) with caption to all Telegram chats."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "photo": photo_url,
                    "caption": caption[:1024],  # Telegram caption limit
                },
                timeout=15,
            )
        except Exception as e:
            print(f"  [WARN] Telegram photo send failed for {chat_id}: {e}")


def _send_postpeak_telegram(text: str) -> None:
    """Send to the dedicated @postpeak channel for speed-alpha signals only."""
    if not TELEGRAM_BOT_TOKEN or not POSTPEAK_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": POSTPEAK_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"  [WARN] Postpeak Telegram send failed: {e}")


def _send_postpeak_telegram_photo(photo_url: str, caption: str) -> None:
    """Send a photo to the dedicated @postpeak channel."""
    if not TELEGRAM_BOT_TOKEN or not POSTPEAK_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        requests.post(
            url,
            json={
                "chat_id": POSTPEAK_CHAT_ID,
                "photo": photo_url,
                "caption": caption[:1024],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"  [WARN] Postpeak Telegram photo send failed: {e}")


def is_speed_alpha_trade(decision: dict) -> bool:
    """
    Returns True if a BUY_YES signal is in the speed-alpha window:
    post-peak, bucket confirmed by max_so_far observation, market 5-70%.

    Backtest (Apr 15-17): 14/16 = 87.5% win rate in this window.
    The alpha is speed of interpretation, not prediction — the scanner reads
    the METAR and maps it to a settlement bucket faster than the market reprices.
    """
    from src.analysis.settlement_rounding import apply_city_settlement

    # 1. Must be post-peak entry mode
    if decision.get("entry_mode") != "post_peak":
        return False

    # 2. Bucket must be confirmed by observation (max_so_far rounds to predicted bucket)
    llm = decision.get("llm", {})
    predicted_bucket = llm.get("bucket")
    if predicted_bucket is None:
        return False

    detail = decision.get("detail") or decision.get("market_scan", {})
    # Try multiple paths to find max_so_far
    max_so_far = None
    if isinstance(decision.get("detail"), dict):
        max_so_far = decision["detail"].get("current", {}).get("max_so_far")
    if max_so_far is None:
        max_so_far = decision.get("max_so_far")

    if max_so_far is None:
        return False

    city_name = decision.get("name") or decision.get("city", "")
    settled_bucket = apply_city_settlement(city_name, max_so_far)
    if settled_bucket != predicted_bucket:
        return False

    # 3. Market must be in the speed-alpha window (5-70%)
    mkt_price = llm.get("estimated_market_price") or 0
    if mkt_price < SPEED_ALPHA_MKT_MIN or mkt_price > SPEED_ALPHA_MKT_MAX:
        return False

    # 4. Model must have converged (sigma not too wide)
    sigma = decision.get("sigma") or 0
    if sigma >= SPEED_ALPHA_SIGMA_MAX:
        return False

    # 5. Minimum liquidity
    ms = decision.get("market_scan", {})
    liq = ms.get("liquidity") or 0
    if liq < SPEED_ALPHA_LIQ_MIN:
        return False

    return True


def _hours_until_peak(local_time_str: str, peak_hours: list) -> float | None:
    """Estimate hours from current local time to first peak hour."""
    if not local_time_str or not peak_hours:
        return None
    try:
        parts = local_time_str.replace(":", ".")
        now_h = float(parts.split(".")[0]) + float(parts.split(".")[1]) / 60.0
        first_peak = int(peak_hours[0].split(":")[0])
        diff = first_peak - now_h
        return diff if diff > 0 else 0.0
    except Exception:
        return None


def _extract_multi_model(d: dict) -> dict:
    """Pull today's multi-model forecasts into a flat dict."""
    mmd = d.get("multi_model_daily", {})
    today = d.get("local_date", "")
    if today in mmd:
        return mmd[today].get("models", {})
    # fallback: first key
    for k, v in mmd.items():
        return v.get("models", {})
    return {}


def _extract_surface_structure(d: dict) -> dict:
    """Extract vertical profile / upper-air / TAF signals."""
    vps = d.get("vertical_profile_signal", {})
    taf = d.get("taf", {}).get("signal", {})
    dc = d.get("dynamic_commentary", {})
    return {
        "cape": vps.get("cape_max"),
        "cin": vps.get("cin_min"),
        "lifted_index": vps.get("lifted_index_min"),
        "blh": vps.get("boundary_layer_height_max"),
        "suppression_risk": vps.get("suppression_risk"),
        "trigger_risk": vps.get("trigger_risk"),
        "taf_available": taf.get("available", False),
        "taf_raw": (taf.get("raw_taf") or "")[:120],
        "commentary": dc.get("summary", ""),
        "notes": dc.get("notes", []),
    }


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a temperature settlement market analyst for Polymarket weather derivatives.

BACKTESTED STRATEGY (142 city-days, Apr 13-16 2026):
- Bucket accuracy 3-5h before peak: ~18% (LOSING if you just buy the top bucket)
- Bucket accuracy 1-2h before peak: 37.5% (PROFITABLE if market < 30%)
- Bucket accuracy at peak: 44.6%
- Post-peak accuracy: 66.2% (HIGHLY PROFITABLE if market is slow to update)
- Stable bucket (same prediction 2h+): 20% vs unstable 13% — stability helps

You will be told the ENTRY MODE:
  "golden_hour" = 1-3h before peak. METAR is rising, sigma is tight (<1.5).
  "post_peak" = 0-2h after peak. Max_so_far is likely the settlement value.

STRATEGY BY MODE:

GOLDEN HOUR (1-3h before peak):
- BUY YES on the model's top bucket OR top-2 bucket spread
- ONLY if: METAR is rising monotonically, sigma < 1.5, top-2 prob > 40%
- The METAR sequence is your primary signal — if the last 3+ readings show
  steady warming toward the model's predicted high, confidence increases
- Rate of warming matters: +1°C/hour or more = strong signal
- If max_so_far is already within 1° of the model's predicted bucket, HIGH confidence
- Edge threshold: model_prob > market_price + 5% minimum
- Position: 1-3% of bankroll

POST-PEAK CAPTURE (0-2h after peak):
- The max_so_far IS the settlement value (66% accuracy from backtest)
- BUY YES on the bucket matching max_so_far if market is still pricing uncertainty
- This is a VALUE play — the outcome is nearly certain, you're just faster than the market
- Only if model has >50% on one bucket and market is pricing it < 40%
- Position: 3-5% of bankroll (higher confidence)

CRITICAL RULES:
1. SKIP > losing money. Always.
2. Read the METAR sequence carefully — it tells you if warming is on track
3. For golden_hour: the model's top-2 adjacent buckets together are the real bet.
   If top bucket is 17° at 35% and adjacent 18° at 30%, that's 65% coverage.
   If market prices 17° at 20%, that's +15% edge on that bucket alone.
4. For post_peak: if max_so_far = 17.3°C and model says 17° bucket at 85%,
   and market still prices 17° at 50%, that's +35% edge. TAKE IT.
5. SKIP if: sigma > 1.5, METAR falling, precip > 40%, liquidity < $200
6. IGNORE "market_scan.available" and "past_end_time" — markets ARE open.

ADJACENT BUCKET ANALYSIS:
When responding, analyze BOTH the top bucket and its neighbor. Report which
specific bucket(s) you'd buy YES on. If the top-2 are adjacent (e.g. 17° and 18°),
consider buying both for a wider net.

Respond ONLY with valid JSON:
{
  "action": "BUY_YES" | "SKIP",
  "entry_mode": "golden_hour" | "post_peak",
  "bucket": <integer - the primary temperature bucket value>,
  "adjacent_bucket": <integer | null - secondary bucket if buying a spread>,
  "model_probability": <float 0-1>,
  "estimated_market_price": <float 0-1>,
  "edge_pct": <float>,
  "confidence": "high" | "medium" | "low",
  "size_pct_of_bankroll": <float>,
  "reasoning": "<2-3 sentences explaining why>",
  "risk_factors": ["<factor1>", "<factor2>"],
  "time_sensitivity": "urgent" | "normal" | "wait"
}
"""


def _format_top_buckets(top_buckets: list) -> str:
    if not top_buckets:
        return "  (no bucket prices available)"
    lines = []
    for b in top_buckets:
        temp = b.get("bucket_temp", b.get("temperature", "?"))
        yes_buy = b.get("yes_buy")
        mkt_price = b.get("market_price")
        prob = b.get("probability")
        price_str = f"YES={yes_buy:.2f}" if yes_buy else (f"mid={mkt_price:.2f}" if mkt_price else f"implied={prob:.2f}" if prob else "no price")
        q = b.get("question", "")[:60]
        liq = b.get("liquidity", 0)
        lines.append(f"  {temp}deg: {price_str}  liq=${liq:.0f}  {q}")
    return chr(10).join(lines)


def _build_user_prompt(city_name: str, d: dict, ms: dict) -> str:
    cur = d.get("current", {})
    deb = d.get("deb", {})
    prob = d.get("probabilities", {})
    peak = d.get("peak", {})
    dm = d.get("deviation_monitor", {})
    tr = d.get("trend", {})
    ens = d.get("ensemble", {})
    models = _extract_multi_model(d)
    struct = _extract_surface_structure(d)
    risk = d.get("risk", {})
    bucket = ms.get("temperature_bucket", {}) or {}
    airport_cur = d.get("airport_current", {})

    # Probability distribution
    dist_lines = []
    for b in prob.get("distribution", []):
        dist_lines.append(f"  {b['value']}deg [{b['range']}]: {b['probability']*100:.1f}%")

    # Shadow (EMOS) distribution if available
    shadow_lines = []
    for b in prob.get("shadow_distribution", []) or []:
        shadow_lines.append(f"  {b['value']}deg [{b['range']}]: {b['probability']*100:.1f}%")

    # FULL METAR observation sequence — chronological, ALL readings today
    metar_obs = d.get("metar_today_obs", [])
    metar_sequence = []
    for o in metar_obs:
        t = o.get("time", "?")
        temp = o.get("temp", "?")
        wdir = o.get("wdir", "")
        wspd = o.get("wspd", "")
        wind_str = f" wind={wdir}@{wspd}kt" if wdir else ""
        metar_sequence.append(f"  {t}: {temp}deg{wind_str}")

    # Recent cluster observations
    recent_obs = d.get("metar_recent_obs", [])
    cluster_lines = []
    for o in recent_obs:
        cluster_lines.append(f"  {o.get('time','?')}: {o.get('temp','?')}deg wind={o.get('wdir','')}@{o.get('wspd','')}kt cloud_rank={o.get('cloud_rank','')}")

    # Nearby official stations
    nearby = d.get("official_nearby", [])
    nearby_lines = []
    for s in nearby[:8]:
        nearby_lines.append(f"  {s.get('station_label','?')}: {s.get('temp','?')}deg")

    # MGM data (for Turkey cities)
    mgm = d.get("mgm", {})
    mgm_nearby = d.get("mgm_nearby", [])

    # Network signals
    net_lead = d.get("network_lead_signal", {})
    net_spread = d.get("network_spread_signal", {})

    # Hourly forecast for peak window
    h48 = d.get("hourly_next_48h", {})
    hourly_temps = list(zip(h48.get("times", [])[:12], h48.get("temps", [])[:12])) if h48 else []
    hourly_lines = [f"  {t}: {v}deg" for t, v in hourly_temps]

    # Dynamic commentary
    dc = d.get("dynamic_commentary", {})
    commentary_notes = dc.get("notes", [])

    # Multi-model daily (today + next days)
    mmd = d.get("multi_model_daily", {})
    mmd_lines = []
    for date_key, info in list(mmd.items())[:3]:
        m = info.get("models", {})
        model_str = ", ".join(f"{k}={v}" for k, v in m.items())
        d_deb = info.get("deb", {})
        mmd_lines.append(f"  {date_key}: {model_str}  DEB={d_deb.get('prediction')}")

    # Pace info from deviation monitor
    pace_adj_high = dm.get("pace_adjusted_high") or cur.get("max_so_far")

    liq_val = ms.get('liquidity') or 0
    mp_val = ms.get('model_probability') or 0
    mkt_val = ms.get('market_price')
    edge_val = ms.get('edge_percent')
    bp_val = (bucket.get('probability') or 0)

    # Entry mode from candidate + sorted distribution
    entry_mode_val = ms.get("_entry_mode", "unknown")
    dist_raw = prob.get("distribution", [])
    sorted_dist = sorted(dist_raw, key=lambda b: b.get("probability", 0), reverse=True)

    # Pre-compute top-2 bucket strings to avoid index errors on single-bucket distributions
    top1_str = (
        f"{sorted_dist[0].get('value')}deg @ {sorted_dist[0].get('probability', 0)*100:.1f}% "
        f"(range: {sorted_dist[0].get('range', '?')})"
    ) if sorted_dist else "? (no distribution)"
    if len(sorted_dist) > 1:
        b2 = sorted_dist[1]
        top2_str = (
            f"{b2.get('value')}deg @ {b2.get('probability', 0)*100:.1f}% "
            f"(range: {b2.get('range', '?')})"
        )
        adjacent = abs((sorted_dist[0].get('value') or 0) - (b2.get('value') or 0)) <= 1
    else:
        top2_str = "(none — single-bucket distribution)"
        adjacent = False
    combined_top2 = sum(b.get('probability', 0) for b in sorted_dist[:2]) * 100

    return f"""\
CITY: {city_name}
DATE: {d.get('local_date', '?')}
LOCAL TIME: {d.get('local_time', '?')}
ENTRY MODE: {entry_mode_val}

TOP-2 BUCKET ANALYSIS:
- Top bucket: {top1_str}
- 2nd bucket: {top2_str}
- Combined top-2 probability: {combined_top2:.1f}%
- Are they adjacent? {'YES' if adjacent else 'NO'}

========== CURRENT ANCHOR STATE ==========
- Current temp: {cur.get('temp')} | Max so far: {cur.get('max_so_far')}
- WU settlement bucket (current): {cur.get('wu_settlement')}
- Settlement source: {cur.get('settlement_source')} ({cur.get('settlement_source_label')})
- Weather phenomena: {cur.get('wx_desc') or 'none'}
- Wind: {cur.get('wind_dir')} at {cur.get('wind_speed_kt')}kt
- Cloud: {cur.get('cloud_desc') or 'unknown'}
- Humidity: {cur.get('humidity') or 'unknown'}
- Raw METAR: {cur.get('raw_metar', 'unavailable')}
- Observation age: {cur.get('obs_age_min', '?')} minutes

========== METAR OBSERVATION SEQUENCE (chronological — CRITICAL) ==========
Read this carefully to judge if the daily peak has been reached.
If temps are still rising, peak is likely ahead. If flat/falling, peak may be done.
{chr(10).join(metar_sequence) if metar_sequence else '  (no observations available)'}

Total observations today: {len(metar_obs)}
Latest reading: {metar_sequence[-1] if metar_sequence else 'none'}

========== METAR CLUSTER (nearby reference stations) ==========
{chr(10).join(cluster_lines) if cluster_lines else '  (none)'}

========== PEAK & TREND ==========
- Peak window: {peak.get('hours', [])} | Status: {peak.get('status', '?')}
- Trend direction: {tr.get('direction', '?')}
- Is dead market: {tr.get('is_dead_market', False)}
- Is cooling: {tr.get('is_cooling', False)}
- Deviation: {dm.get('label_en', '?')} ({dm.get('trend_label_en', '?')})
- Deviation delta: {dm.get('current_delta', '?')}deg
- Deviation severity: {dm.get('severity', '?')}

========== MODEL FORECAST ==========
- DEB prediction: {deb.get('prediction')}
- DEB weights: {deb.get('weights_info', '?')}
- Forecast today_high: {d.get('forecast', {}).get('today_high')}
- Multi-model:
{chr(10).join(mmd_lines) if mmd_lines else '  (none)'}
- Ensemble: median={ens.get('median')}, p10={ens.get('p10')}, p90={ens.get('p90')}
- Model mu: {prob.get('mu')} | sigma: {prob.get('raw_sigma')}
- Calibration: engine={prob.get('engine')} mode={prob.get('calibration_mode')}

========== PROBABILITY BUCKETS (model) ==========
{chr(10).join(dist_lines) if dist_lines else '  (none)'}

{"========== SHADOW DISTRIBUTION (EMOS) ==========" + chr(10) + chr(10).join(shadow_lines) if shadow_lines else ""}

========== MARKET SCAN ==========
- Matched market: {(ms.get('primary_market') or {}).get('question', '?')}
- Market slug: {ms.get('selected_slug', '?')}
- Focus bucket: {bucket.get('value')}deg @ {bp_val*100:.1f}%
- Model probability: {mp_val}
- Market YES price: {mkt_val}
- Edge: {edge_val}%
- Signal: {ms.get('signal_label')} | Confidence: {ms.get('confidence')}
- Liquidity: ${liq_val:.0f}
- Volume: ${ms.get('volume') or 0:.0f}
- Sparkline (top bucket probs): {ms.get('sparkline', [])}
- Anchor model: {ms.get('anchor_model', '?')}
- Anchor settlement: {ms.get('anchor_settlement', '?')}

========== MARKET BUCKET LADDER (per-bucket prices from Polymarket) ==========
{_format_top_buckets(ms.get('top_buckets', []))}

========== STRUCTURAL SIGNALS ==========
Upper air:
- CAPE: {struct['cape']} | CIN: {struct['cin']} | Lifted Index: {struct['lifted_index']}
- Boundary layer height: {struct['blh']}m
- Suppression risk: {struct['suppression_risk']} | Trigger risk: {struct['trigger_risk']}

TAF:
- {struct['taf_raw'] or 'unavailable'}

Commentary:
- {struct['commentary']}
{chr(10).join(f'- {n}' for n in commentary_notes) if commentary_notes else ''}

========== NEARBY STATIONS ==========
{chr(10).join(nearby_lines) if nearby_lines else '  (none)'}
Network lead: {json.dumps(net_lead) if net_lead else 'none'}
Network spread: {json.dumps(net_spread) if net_spread else 'none'}
{"MGM (Turkey): temp=" + str(mgm.get("temp")) + " time=" + str(mgm.get("time")) if mgm.get("temp") else ""}

========== HOURLY FORECAST (next hours) ==========
{chr(10).join(hourly_lines) if hourly_lines else '  (none)'}

========== CONTEXT ==========
- Risk level: {risk.get('level', '?')} | Airport: {risk.get('airport', '?')} ({risk.get('distance_km', '?')}km)
- Risk warning: {risk.get('warning', 'none')}
- Sunrise: {d.get('forecast', {}).get('sunrise', '?')} | Sunset: {d.get('forecast', {}).get('sunset', '?')}
"""


def _call_llm(user_prompt: str) -> dict | None:
    if ANTHROPIC_API_KEY:
        return _call_anthropic(user_prompt)
    if GROQ_API_KEY:
        return _call_groq(user_prompt)
    print("  [WARN] No ANTHROPIC_API_KEY or GROQ_API_KEY set — skipping LLM call")
    return None


def _call_anthropic(user_prompt: str) -> dict | None:
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 600,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_prompt + "\n\nRespond ONLY with the JSON object, no markdown fences."},
        ],
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
        # Anthropic returns content as a list of blocks
        text_blocks = [b["text"] for b in body.get("content", []) if b.get("type") == "text"]
        raw = "".join(text_blocks).strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
        return json.loads(raw)
    except Exception as e:
        print(f"  [ERROR] Anthropic call failed: {e}")
        return None


def _call_groq(user_prompt: str) -> dict | None:
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.1,
        "max_tokens": 500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
        content = (((body.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
        return json.loads(content)
    except Exception as e:
        print(f"  [ERROR] Groq call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Telegram message formatter
# ---------------------------------------------------------------------------


def _format_telegram_message(decisions: list, bankroll: float) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"PolyWeather Alpha Scanner | {now_utc}", ""]

    buys = [d for d in decisions if d["llm"].get("action") == "BUY_YES"]
    skips = [d for d in decisions if d["llm"].get("action") != "BUY_YES"]

    if not buys:
        lines.append("No actionable trades found this scan.")
        lines.append(f"Scanned {len(decisions)} candidates, all SKIP.")
        return "\n".join(lines)

    lines.append(f"BUY YES signals: {len(buys)}")
    lines.append("")

    for i, d in enumerate(buys, 1):
        llm = d["llm"]
        ms = d["market_scan"]
        city = d["city"]
        size_pct = llm.get("size_pct_of_bankroll") or 0
        size_usd = bankroll * (size_pct / 100.0)
        slug = ms.get("selected_slug", "")
        q = (ms.get("primary_market") or {}).get("question", "")
        mp = llm.get("model_probability") or 0
        mkp = llm.get("estimated_market_price") or 0
        ep = llm.get("edge_pct")
        ep_str = f"{ep:+.1f}%" if isinstance(ep, (int, float)) else str(ep)

        lines.append(f"{i}. {city} | {(llm.get('confidence') or '?').upper()} confidence")
        lines.append(f"   Bucket: {llm.get('bucket')}deg")
        lines.append(f"   Model: {mp:.1%} vs Market: {mkp:.1%}")
        lines.append(f"   Edge: {ep_str}")
        lines.append(f"   Size: ${size_usd:.0f} ({size_pct:.1f}% of ${bankroll:.0f})")
        lines.append(f"   Timing: {llm.get('time_sensitivity', '?')}")
        lines.append(f"   Reason: {llm.get('reasoning', '?')}")
        risks = llm.get("risk_factors", [])
        if risks:
            lines.append(f"   Risks: {', '.join(risks)}")
        if q:
            lines.append(f"   Market: {q}")
        if slug:
            lines.append(f"   Link: https://polymarket.com/market/{slug}")
        lines.append("")

    if skips:
        lines.append(f"Skipped: {len(skips)} candidates (no clean edge)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------


def scan(*, dry_run: bool = False, bankroll: float = DEFAULT_BANKROLL, wave: str = "all"):
    print(f"PolyWeather Alpha Scanner")
    print(f"  API: {LOCAL_API}")
    llm_label = f"Anthropic/{ANTHROPIC_MODEL}" if ANTHROPIC_API_KEY else (f"Groq/{GROQ_MODEL}" if GROQ_API_KEY else "DISABLED (no API key)")
    print(f"  LLM: {llm_label}")
    print(f"  Telegram: {'enabled' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS else 'disabled'}")
    print(f"  Bankroll: ${bankroll:.0f}")
    print(f"  Dry run: {dry_run}")
    print(f"  Wave: {wave}")
    print()

    # 1. Get all cities
    cities = _api_get("/api/cities")["cities"]
    print(f"Loaded {len(cities)} cities\n")

    candidates = []
    skipped_reasons = {}

    # 2. Scan each city — pre-filter by UTC peak timing BEFORE any API calls
    now_utc = datetime.now(timezone.utc)
    print(f"Current UTC: {now_utc.strftime('%H:%M')}\n")

    for c in cities:
        name = c["name"]
        display = c["display_name"]
        sys.stdout.write(f"  {display:<16}")
        sys.stdout.flush()

        # UTC peak pre-filter: two entry modes
        # MODE 1: "golden hour" — 1-3h before peak (37.5% bucket accuracy)
        # MODE 2: "post-peak capture" — 0-2h after peak (66% accuracy)
        h_to_peak = _city_hours_to_peak_utc(name)
        entry_mode = None
        if h_to_peak is not None:
            if GOLDEN_HOUR_MIN <= h_to_peak <= GOLDEN_HOUR_MAX:
                entry_mode = "golden_hour"
            elif -POST_PEAK_MAX <= h_to_peak < 0:
                entry_mode = "post_peak"
            else:
                print(f" SKIP timing (peak in {h_to_peak:+.1f}h, need {GOLDEN_HOUR_MIN}-{GOLDEN_HOUR_MAX}h or post-peak)")
                skipped_reasons[display] = f"timing={h_to_peak:+.1f}h"
                continue

        # Fetch regular endpoint (fresh METAR/weather)
        try:
            d = _api_get(f"/api/city/{name}")
        except Exception as e:
            print(f" FETCH ERROR (city): {e}")
            continue

        # Get structural signals from detail endpoint
        try:
            detail = _api_get(f"/api/city/{name}/detail")
            for key in ("vertical_profile_signal", "taf", "official_nearby",
                        "network_lead_signal", "network_spread_signal",
                        "metar_recent_obs", "settlement_today_obs"):
                if key not in d or not d[key]:
                    d[key] = detail.get(key, d.get(key))
        except Exception:
            pass

        peak = d.get("peak", {})
        tr = d.get("trend", {})
        dm = d.get("deviation_monitor", {})
        lt = d.get("local_time", "")
        prob = d.get("probabilities", {})
        dist = prob.get("distribution", [])

        # Use the detail endpoint's market_scan — now returns prices even for past_end_time
        try:
            detail = _api_get(f"/api/city/{name}/detail")
            ms = detail.get("market_scan", {}) or {}
        except Exception:
            ms = {}

        # Also grab top_buckets for LLM context (each has its own market price)
        top_buckets = ms.get("top_buckets", [])
        all_buckets = ms.get("all_buckets", [])

        # Basic filters
        peak_status = peak.get("status", "")
        is_dead = tr.get("is_dead_market", False)
        edge = ms.get("edge_percent")
        liq = ms.get("liquidity") or 0
        model_p = ms.get("model_probability") or 0
        mkt_price = ms.get("market_price")
        sigma = prob.get("raw_sigma") or 0
        hours_to_peak = _hours_until_peak(lt, peak.get("hours", []))

        # Skip dead markets
        if is_dead and entry_mode != "post_peak":
            print(f" SKIP dead_market")
            skipped_reasons[display] = "dead_market"
            continue

        # Must have probability distribution
        dist = prob.get("distribution", [])
        if not dist:
            print(f" SKIP no_probability_data")
            skipped_reasons[display] = "no_prob"
            continue

        # Top-1 and top-2 bucket probabilities
        sorted_dist = sorted(dist, key=lambda b: b.get("probability", 0), reverse=True)
        top1_prob = sorted_dist[0].get("probability", 0) if sorted_dist else 0
        top2_prob = sum(b.get("probability", 0) for b in sorted_dist[:2])
        top1_val = sorted_dist[0].get("value") if sorted_dist else None
        top2_val = sorted_dist[1].get("value") if len(sorted_dist) > 1 else None

        # METAR rising check: are last 3+ readings monotonically increasing?
        metar_obs = d.get("metar_today_obs", [])
        metar_temps = [o.get("temp") for o in metar_obs if o.get("temp") is not None]
        metar_rising = False
        metar_trend_str = "?"
        if len(metar_temps) >= 3:
            last3 = metar_temps[-3:]
            metar_rising = all(last3[i] <= last3[i + 1] for i in range(len(last3) - 1))
            metar_trend_str = "RISING" if metar_rising else "FLAT/MIXED"
        elif len(metar_temps) >= 2:
            metar_rising = metar_temps[-1] > metar_temps[-2]
            metar_trend_str = "RISING" if metar_rising else "FLAT/FALLING"

        max_so_far = d.get("current", {}).get("max_so_far")
        edge_display = edge if edge is not None else None

        # MODE-SPECIFIC FILTERS
        if entry_mode == "golden_hour":
            # Sigma must be tightening
            if sigma > MAX_SIGMA_GOLDEN:
                print(f" SKIP sigma={sigma:.1f} > {MAX_SIGMA_GOLDEN} [{entry_mode}]")
                skipped_reasons[display] = f"high_sigma={sigma:.1f}"
                continue
            # Top-2 buckets must cover enough probability
            if top2_prob < MIN_TOP2_PROB:
                print(f" SKIP top2_prob={top2_prob:.1%} < {MIN_TOP2_PROB:.0%} [{entry_mode}]")
                skipped_reasons[display] = f"low_top2={top2_prob:.1%}"
                continue
            # METAR should be rising (warmup phase)
            if not metar_rising and len(metar_temps) >= 3:
                print(f" SKIP metar_not_rising ({metar_trend_str}) [{entry_mode}]")
                skipped_reasons[display] = f"metar_{metar_trend_str}"
                continue

        elif entry_mode == "post_peak":
            # Post-peak: max_so_far should exist
            if max_so_far is None:
                print(f" SKIP no_max_so_far [{entry_mode}]")
                skipped_reasons[display] = "no_max"
                continue
            # Top-1 must be clear leader (>35%) — still above random for 3-5 bucket spread
            # At 35%+ the model has a clear pick even if not dominant
            if top1_prob < 0.35:
                print(f" SKIP top1_prob={top1_prob:.1%} < 35% [{entry_mode}]")
                skipped_reasons[display] = f"low_conf={top1_prob:.1%}"
                continue

        # Liquidity filter
        if liq < 100:
            print(f" SKIP low_liquidity=${liq:.0f}")
            continue

        peak_str = f"peak_in={h_to_peak:+.1f}h" if h_to_peak is not None else "peak=?"
        edge_s = f"edge={edge_display:+.1f}%" if edge_display is not None else "edge=??"
        mode_tag = f"[{entry_mode}]"
        print(f" CANDIDATE {mode_tag:<14} {edge_s}  top1={top1_val}@{top1_prob:.0%}  top2={top2_val}@{top2_prob:.0%}  sig={sigma:.1f}  metar={metar_trend_str}  liq=${liq:.0f}  {peak_str}")

        candidates.append({
            "city": display,
            "name": name,
            "detail": d,
            "market_scan": ms,
            "entry_mode": entry_mode,
            "edge": edge_display if edge_display is not None else 0,
            "top1_prob": top1_prob,
            "top2_prob": top2_prob,
            "top1_val": top1_val,
            "top2_val": top2_val,
            "sigma": sigma,
            "metar_rising": metar_rising,
            "metar_trend": metar_trend_str,
            "hours_to_peak": h_to_peak,
        })

    # Sort: post_peak first (highest accuracy), then by absolute edge
    candidates.sort(key=lambda x: (
        0 if x["entry_mode"] == "post_peak" else 1,
        abs(x["edge"]) if x["edge"] else 0,
        x["top1_prob"],
    ), reverse=True)

    print(f"\n{'='*110}")
    print(f"ALL CANDIDATES: {len(candidates)} | Skipped: {len(skipped_reasons)}")
    print(f"{'='*110}")
    print(f"{'City':<15} {'Mode':<13} {'Edge':>7} {'Top1':>6} {'Top2':>6} {'Sig':>5} {'METAR':>8} {'Liq':>9} {'Peak':>7} {'MktP':>6}")
    print("-" * 110)
    for cand in candidates:
        ms = cand["market_scan"]
        e = cand["edge"]
        e_s = f"{e:+.1f}%" if e else "??"
        mp = ms.get("market_price")
        mp_s = f"{mp:.2f}" if mp is not None else "?"
        h = cand["hours_to_peak"]
        h_s = f"{h:+.1f}h" if h is not None else "?"
        print(f"  {cand['city']:<13} {cand['entry_mode']:<13} {e_s:>7} {str(cand['top1_val'])+'°':>6} {str(cand['top2_val'])+'°':>6} {cand['sigma']:>5.1f} {cand['metar_trend']:>8} {'$'+str(int(ms.get('liquidity') or 0)):>9} {h_s:>7} {mp_s:>6}")
    print()

    # Split: top N go to LLM, rest just printed
    llm_candidates = candidates[:LLM_TOP_N]
    rest_candidates = candidates[LLM_TOP_N:]

    if rest_candidates:
        print(f"(Sending top {LLM_TOP_N} to LLM, {len(rest_candidates)} more listed above)\n")

    if not candidates:
        print("No candidates found. Markets may be closed or no edge detected.")
        msg = (
            f"PolyWeather Alpha Scanner | {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n\n"
            f"No actionable trades found.\n"
            f"Scanned {len(cities)} cities, 0 candidates.\n"
            f"Skipped: {json.dumps(dict(list(skipped_reasons.items())[:10]), indent=2)}"
        )
        if not dry_run:
            _send_telegram(msg)
        return

    # 4. LLM evaluation (top N only)
    decisions = []
    for cand in llm_candidates:
        city = cand["city"]
        d = cand["detail"]
        ms = cand["market_scan"]
        # Inject entry_mode so LLM prompt can read it
        ms["_entry_mode"] = cand["entry_mode"]

        mode_tag = cand["entry_mode"]
        print(f"LLM evaluating: {city} [{mode_tag}] (edge={cand['edge']:+.1f}%) ...")

        user_prompt = _build_user_prompt(city, d, ms)
        llm_result = _call_llm(user_prompt)

        if not llm_result:
            print(f"  -> LLM returned nothing, skipping")
            continue

        action = llm_result.get("action", "SKIP")
        edge_pct = llm_result.get("edge_pct")
        edge_str = f"{edge_pct:+.1f}%" if isinstance(edge_pct, (int, float)) else str(edge_pct)
        reasoning = llm_result.get("reasoning", "")
        risks = llm_result.get("risk_factors", [])
        print(f"  -> {action}  bucket={llm_result.get('bucket')}  conf={llm_result.get('confidence')}  edge={edge_str}")
        print(f"     Reason: {reasoning}")
        if risks:
            print(f"     Risks: {', '.join(str(r) for r in risks)}")

        decisions.append({
            "city": city,
            "name": cand["name"],
            "detail": d,
            "market_scan": ms,
            "llm": llm_result,
            "entry_mode": cand["entry_mode"],
            "sigma": cand["sigma"],
            "metar_rising": cand["metar_rising"],
            "top1_prob": cand["top1_prob"],
            "top2_prob": cand["top2_prob"],
            "hours_to_peak": cand["hours_to_peak"],
        })

        # Log every LLM decision to signals.jsonl
        _log_jsonl(SIGNAL_LOG, {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "city": city,
            "date": ms.get("selected_date"),
            "entry_mode": cand["entry_mode"],
            "action": action,
            "bucket": llm_result.get("bucket"),
            "adjacent_bucket": llm_result.get("adjacent_bucket"),
            "model_probability": llm_result.get("model_probability"),
            "estimated_market_price": llm_result.get("estimated_market_price"),
            "edge_pct": edge_pct,
            "confidence": llm_result.get("confidence"),
            "size_pct": llm_result.get("size_pct_of_bankroll"),
            "reasoning": reasoning,
            "risk_factors": risks,
            "sigma": cand["sigma"],
            "metar_rising": cand["metar_rising"],
            "top1_prob": cand["top1_prob"],
            "top2_prob": cand["top2_prob"],
            "hours_to_peak": cand["hours_to_peak"],
            "market_slug": ms.get("selected_slug"),
            "market_price": ms.get("market_price"),
            "liquidity": ms.get("liquidity"),
            "max_so_far": d.get("current", {}).get("max_so_far"),
            "deb_prediction": d.get("deb", {}).get("prediction"),
            "mu": d.get("probabilities", {}).get("mu"),
        })

        time.sleep(0.5)  # rate limit courtesy

    # 5. Output
    print(f"\n{'='*70}")
    print("RESULTS")
    print(f"{'='*70}\n")

    buys = [d for d in decisions if d["llm"].get("action") == "BUY_YES"]
    skips_llm = [d for d in decisions if d["llm"].get("action") != "BUY_YES"]

    if buys:
        print(f"BUY YES: {len(buys)}")
        for d in buys:
            llm = d["llm"]
            ms = d["market_scan"]
            size_pct = llm.get("size_pct_of_bankroll") or 0
            size_usd = bankroll * (size_pct / 100.0)
            mp = llm.get("model_probability") or 0
            mkp = llm.get("estimated_market_price") or 0
            ep = llm.get("edge_pct")
            ep_str = f"{ep:+.1f}%" if isinstance(ep, (int, float)) else str(ep)
            print(f"  {d['city']:<14} bucket={llm.get('bucket')}  model={mp:.1%}  mkt={mkp:.1%}  edge={ep_str}  size=${size_usd:.0f}  [{llm.get('confidence')}]")
            print(f"    {llm.get('reasoning', '')}")

            # Log to trades.csv for P&L backtesting
            _log_trade_csv({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "city": d["city"],
                "date": ms.get("selected_date"),
                "entry_mode": d.get("entry_mode"),
                "bucket": llm.get("bucket"),
                "adjacent_bucket": llm.get("adjacent_bucket"),
                "model_probability": mp,
                "market_price": mkp,
                "edge_pct": ep,
                "confidence": llm.get("confidence"),
                "size_pct": size_pct,
                "size_usd": size_usd,
                "bankroll": bankroll,
                "sigma": d.get("sigma"),
                "metar_rising": d.get("metar_rising"),
                "max_so_far": d.get("detail", {}).get("current", {}).get("max_so_far"),
                "deb_prediction": d.get("detail", {}).get("deb", {}).get("prediction"),
                "mu": d.get("detail", {}).get("probabilities", {}).get("mu"),
                "top1_prob": d.get("top1_prob"),
                "top2_prob": d.get("top2_prob"),
                "hours_to_peak": d.get("hours_to_peak"),
                "market_slug": ms.get("selected_slug"),
                "market_question": (ms.get("primary_market") or {}).get("question"),
                "liquidity": ms.get("liquidity"),
                "volume": ms.get("volume"),
                "reasoning": llm.get("reasoning"),
                "risk_factors": json.dumps(llm.get("risk_factors", [])),
            })
    else:
        print("No BUY YES signals.")

    # Log scan summary
    _log_jsonl(SCAN_LOG, {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "cities_scanned": len(cities),
        "candidates": len(candidates),
        "skipped": len(skipped_reasons),
        "llm_evaluated": len(decisions),
        "buy_signals": len(buys),
        "skip_signals": len(skips_llm),
        "skipped_reasons_sample": dict(list(skipped_reasons.items())[:15]),
    })

    if skips_llm:
        print(f"\nLLM SKIP: {len(skips_llm)}")
        for d in skips_llm:
            llm = d["llm"]
            risks = llm.get("risk_factors", [])
            risk_str = f" | Risks: {', '.join(str(r) for r in risks)}" if risks else ""
            print(f"  {d['city']:<14} bucket={llm.get('bucket')}  {llm.get('reasoning', '')}{risk_str}")

    # 6. Telegram push — two tiers:
    #    @postpeak: only speed-alpha signals (post-peak, bucket confirmed, market 5-70%)
    #    Legacy channel: all BUY YES signals (unchanged behaviour)

    # Filter out signals already pushed recently
    fresh_buys = []
    for d in buys:
        llm = d["llm"]
        city = d["city"]
        bucket = llm.get("bucket")
        date = d.get("market_scan", {}).get("selected_date", "")
        if _is_on_cooldown(city, bucket, date):
            print(f"  {city} bucket={bucket} — on cooldown, skipping push")
        else:
            fresh_buys.append(d)

    # Classify each fresh buy as speed-alpha or not
    speed_alpha_buys = []
    other_buys = []
    for d in fresh_buys:
        # Attach max_so_far from the detail data for the finality gate
        d["max_so_far"] = d.get("detail", {}).get("current", {}).get("max_so_far")
        if is_speed_alpha_trade(d):
            speed_alpha_buys.append(d)
            mkt_p = d["llm"].get("estimated_market_price") or 0
            print(f"  {d['city']} bucket={d['llm'].get('bucket')} — SPEED ALPHA "
                  f"(post-peak, confirmed, mkt={mkt_p:.0%})")
        else:
            mode = d.get("entry_mode", "?")
            reason = []
            if mode != "post_peak":
                reason.append(f"mode={mode}")
            mkt_p = d["llm"].get("estimated_market_price") or 0
            if mkt_p < SPEED_ALPHA_MKT_MIN:
                reason.append(f"mkt={mkt_p:.1%}<5%")
            elif mkt_p > SPEED_ALPHA_MKT_MAX:
                reason.append(f"mkt={mkt_p:.1%}>70%")
            other_buys.append(d)
            print(f"  {d['city']} bucket={d['llm'].get('bucket')} — not speed-alpha "
                  f"({', '.join(reason) or 'bucket unconfirmed'})")

    def _build_chart_slug(d):
        """Build Polymarket chart slug for a decision."""
        llm = d["llm"]
        ms = d["market_scan"]
        date = ms.get("selected_date", "")
        city_name = d.get("name", d["city"]).lower().replace(" ", "-")
        llm_bucket = llm.get("bucket")
        base_slug = ms.get("selected_slug", "")
        unit_suffix = "f" if "f" in base_slug.split("-")[-1] else "c"
        if llm_bucket and date:
            from datetime import datetime as _dt
            try:
                dt = _dt.strptime(date, "%Y-%m-%d")
                date_part = dt.strftime("%B-%-d-%Y").lower()
            except Exception:
                date_part = date
            return f"highest-temperature-in-{city_name}-on-{date_part}-{llm_bucket}{unit_suffix}"
        return base_slug

    # --- @postpeak channel: speed-alpha signals only ---
    if speed_alpha_buys:
        pp_msg = _format_telegram_message([*speed_alpha_buys, *skips_llm], bankroll)
        print(f"\n--- @postpeak message ({len(speed_alpha_buys)} speed-alpha) ---\n{pp_msg}\n--- end ---\n")

        if not dry_run:
            print("Sending to @postpeak ...")
            _send_postpeak_telegram(pp_msg)
            for d in speed_alpha_buys:
                slug = _build_chart_slug(d)
                if slug:
                    llm = d["llm"]
                    ep = llm.get("edge_pct")
                    ep_str = f"{ep:+.1f}%" if isinstance(ep, (int, float)) else "?"
                    caption = (
                        f"{d['city']} | {llm.get('bucket')}deg | Edge {ep_str}\n"
                        f"Model {(llm.get('model_probability') or 0):.0%} vs Market {(llm.get('estimated_market_price') or 0):.0%}\n"
                        f"https://polymarket.com/market/{slug}"
                    )
                    chart_url = f"https://polymarket.com/api/og?mslug={slug}"
                    print(f"  @postpeak chart for {d['city']}: {chart_url[:60]}...")
                    _send_postpeak_telegram_photo(chart_url, caption)
            print("Sent to @postpeak.")
        else:
            print("[DRY RUN] Would send speed-alpha signals to @postpeak.")

    # --- Legacy channel: all BUY YES signals (unchanged) ---
    if decisions:
        msg = _format_telegram_message([*fresh_buys, *skips_llm], bankroll)
        print(f"\n--- Telegram message ---\n{msg}\n--- end ---\n")

        if not dry_run and fresh_buys:
            print("Sending to Telegram ...")
            _send_telegram(msg)

            for d in fresh_buys:
                slug = _build_chart_slug(d)
                if slug:
                    llm = d["llm"]
                    ep = llm.get("edge_pct")
                    ep_str = f"{ep:+.1f}%" if isinstance(ep, (int, float)) else "?"
                    caption = (
                        f"{d['city']} | {llm.get('bucket')}deg | Edge {ep_str}\n"
                        f"Model {(llm.get('model_probability') or 0):.0%} vs Market {(llm.get('estimated_market_price') or 0):.0%}\n"
                        f"https://polymarket.com/market/{slug}"
                    )
                    chart_url = f"https://polymarket.com/api/og?mslug={slug}"
                    print(f"  Sending chart for {d['city']}: {chart_url[:60]}...")
                    _send_telegram_photo(chart_url, caption)

                _mark_pushed(d["city"], d["llm"].get("bucket"),
                             d.get("market_scan", {}).get("selected_date", ""))

            print("Sent.")
        elif dry_run:
            print("[DRY RUN] Would send to Telegram.")
        elif not buys:
            print("No BUY signals, skipping Telegram push.")


# ---------------------------------------------------------------------------
# Loop runner
# ---------------------------------------------------------------------------


def loop(*, dry_run: bool, bankroll: float, wave: str, interval: int):
    """Run scan repeatedly, aligned with METAR update cadence."""
    print(f"Alpha Scanner LOOP mode — interval=15-30s random")
    print(f"  Cooldown per signal: {SIGNAL_COOLDOWN_SEC}s ({SIGNAL_COOLDOWN_SEC/60:.0f}min)")
    print(f"  Press Ctrl+C to stop\n")

    cycle = 0
    while True:
        cycle += 1
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        print(f"\n{'#'*70}")
        print(f"# Cycle {cycle} — {ts}")
        print(f"{'#'*70}\n")

        try:
            scan(dry_run=dry_run, bankroll=bankroll, wave=wave)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"\n[ERROR] Scan cycle {cycle} failed: {e}")

        # Sleep a random 15-30s before next cycle (avoid predictable polling pattern)
        import random
        delay = random.uniform(15, 30)
        wake = datetime.now(timezone.utc).timestamp() + delay
        wake_str = datetime.fromtimestamp(wake, tz=timezone.utc).strftime("%H:%M:%S UTC")
        print(f"\nSleeping {delay:.0f}s — next scan at ~{wake_str}")

        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PolyWeather Alpha Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Don't send to Telegram")
    parser.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL, help="Bankroll in USD")
    parser.add_argument("--wave", default="all", choices=["all", "apac", "europe", "americas", "mideast"], help="Scan only a specific wave")
    parser.add_argument("--loop", action="store_true", help="Run continuously every --interval seconds")
    parser.add_argument("--interval", type=int, default=DEFAULT_LOOP_INTERVAL, help=f"Loop interval in seconds (default {DEFAULT_LOOP_INTERVAL})")
    args = parser.parse_args()

    if args.loop:
        loop(dry_run=args.dry_run, bankroll=args.bankroll, wave=args.wave, interval=args.interval)
    else:
        scan(dry_run=args.dry_run, bankroll=args.bankroll, wave=args.wave)
