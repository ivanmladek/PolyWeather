# PolyWeather Trading Plan

## Overview

Use the local PolyWeather API (`http://localhost:8000`) to scan all 48 cities, pull full intraday analysis, compare model probability buckets against live Polymarket prices, pipe everything into an LLM for buy/sell/size decisions, and execute manually on Polymarket.

## Architecture

```
[Cron / Manual trigger]
        |
        v
[scan_alpha.py]  -- one-off script, NOT part of the bot
        |
        +-- GET /api/city/{name}/detail   x48 cities
        |     returns: current, DEB, probabilities, peak, deviation,
        |              market_scan (bucket, model_prob, market_price, edge, signal),
        |              surface structure, upper-air, TAF, commentary
        |
        +-- Polymarket CLOB (fallback for missing prices)
        |     GET https://clob.polymarket.com/midpoint?token_id=...
        |
        v
[Assemble context per city]
        |
        v
[LLM call]  -- Groq / OpenAI / local model
        |     Input: structured JSON with all city data
        |     Output: { action, bucket, confidence, size_pct, reasoning }
        |
        v
[Output: Markdown report + Telegram push (optional)]
```

## Trading Windows by Region

Polymarket closes order books ~2-4 hours before settlement. The scan must run while markets are still open.

### Wave 1: Asia-Pacific (scan at ~01:00-04:00 UTC)

| City | TZ offset | Peak window (local) | Scan by (UTC) | Unit | Risk |
|------|-----------|---------------------|---------------|------|------|
| Wellington | +12 | 12:00-16:00 | 00:00 | C | low |
| Tokyo | +9 | 14:00-16:00 | 03:00 | C | medium |
| Seoul | +9 | 12:00-13:00 | 02:00 | C | high |
| Busan | +9 | 13:00-14:00 | 03:00 | C | medium |
| Taipei | +8 | 12:00-13:00 | 02:00 | C | low |
| Hong Kong | +8 | 13:00-15:00 | 03:00 | C | medium |
| Lau Fau Shan | +8 | 13:00-15:00 | 03:00 | C | medium |
| Shanghai | +8 | 13:00-14:00 | 03:00 | C | medium |
| Shenzhen | +8 | 13:00-17:00 | 03:00 | C | medium |
| Beijing | +8 | 16:00-17:00 | 05:00 | C | medium |
| Chengdu | +8 | 17:00-18:00 | 06:00 | C | medium |
| Chongqing | +8 | 17:00-18:00 | 06:00 | C | high |
| Wuhan | +8 | 15:00-16:00 | 05:00 | C | high |
| Singapore | +8 | 14:00-16:00 | 04:00 | C | low |
| Kuala Lumpur | +8 | 14:00 | 04:00 | C | medium |
| Jakarta | +7 | 13:00 | 04:00 | C | medium |

### Wave 2: South Asia + Middle East (scan at ~05:00-08:00 UTC)

| City | TZ offset | Peak window (local) | Scan by (UTC) | Unit | Risk |
|------|-----------|---------------------|---------------|------|------|
| Lucknow | +5.5 | 14:00 | 06:00 | C | medium |
| Jeddah | +3 | 13:00-14:00 | 08:00 | C | medium |
| Tel Aviv | +3 | 14:00-15:00 | 09:00 | C | medium |

### Wave 3: Europe + Africa (scan at ~07:00-11:00 UTC)

| City | TZ offset | Peak window (local) | Scan by (UTC) | Unit | Risk |
|------|-----------|---------------------|---------------|------|------|
| Moscow | +3 | 12:00-13:00 | 08:00 | C | medium |
| Helsinki | +3 | 14:00-17:00 | 09:00 | C | medium |
| Istanbul | +3 | 14:00-15:00 | 09:00 | C | medium |
| Ankara | +3 | 15:00-17:00 | 10:00 | C | medium |
| Warsaw | +2 | 15:00 | 10:00 | C | medium |
| Amsterdam | +2 | 15:00-16:00 | 10:00 | C | medium |
| Paris | +2 | 16:00-18:00 | 11:00 | C | medium |
| Munich | +2 | 16:00 | 11:00 | C | high |
| Milan | +2 | 17:00 | 11:00 | C | medium |
| Madrid | +2 | 17:00-19:00 | 12:00 | C | medium |
| London | +1 | 15:00-16:00 | 11:00 | C | low |
| Lagos | +1 | 14:00-15:00 | 10:00 | C | medium |
| Cape Town | +2 | 14:00 | 09:00 | C | medium |

### Wave 4: Americas (scan at ~12:00-18:00 UTC)

| City | TZ offset | Peak window (local) | Scan by (UTC) | Unit | Risk |
|------|-----------|---------------------|---------------|------|------|
| Buenos Aires | -3 | 14:00-15:00 | 14:00 | C | medium |
| Sao Paulo | -3 | 14:00-16:00 | 14:00 | C | high |
| Panama City | -5 | 14:00 | 16:00 | C | medium |
| Mexico City | -6 | 14:00-16:00 | 17:00 | C | high |
| New York | -4 | 15:00 | 16:00 | F | low |
| Toronto | -4 | 14:00 | 15:00 | C | low |
| Miami | -4 | 13:00 | 14:00 | F | low |
| Atlanta | -4 | 17:00 | 18:00 | F | low |
| Chicago | -5 | 17:00 | 19:00 | F | high |
| Houston | -5 | 16:00 | 18:00 | F | medium |
| Austin | -5 | 16:00-18:00 | 18:00 | F | medium |
| Dallas | -5 | 18:00 | 20:00 | F | medium |
| Denver | -6 | 13:00 | 16:00 | F | medium |
| Los Angeles | -7 | 12:00 | 16:00 | F | medium |
| San Francisco | -7 | 13:00 | 17:00 | F | medium |
| Seattle | -7 | 15:00-18:00 | 19:00 | F | low |

## Data Pulled Per City

From `GET /api/city/{name}/detail`:

```
ANCHOR STATE
  current.temp              -- live METAR reading
  current.max_so_far        -- highest observed today
  current.raw_metar         -- raw METAR string
  current.wind_speed_kt     -- wind speed
  current.wind_dir          -- wind direction
  current.wx_desc           -- weather phenomena (RA, -RA, etc)

FORECAST
  deb.prediction            -- DEB blended forecast
  forecast.today_high       -- Open-Meteo base forecast
  multi_model_daily         -- ECMWF, GFS, ICON, GEM, JMA, NWS per day
  ensemble.median/p10/p90   -- ensemble spread

PROBABILITIES
  probabilities.mu          -- dynamic center
  probabilities.raw_sigma   -- spread
  probabilities.distribution -- [{value, range, probability}, ...]
  probabilities.shadow_distribution -- EMOS shadow (if available)

PEAK & TREND
  peak.hours                -- expected peak window
  peak.status               -- before / during / past
  trend.direction           -- rising / falling / stagnant / mixed
  trend.is_dead_market      -- peak passed + falling = locked
  deviation_monitor.*       -- hot/cold/normal, severity, trend

MARKET SCAN (when available)
  market_scan.available           -- is market tradable right now?
  market_scan.temperature_bucket  -- {value, probability}
  market_scan.model_probability   -- model's P for matched bucket
  market_scan.market_price        -- Polymarket YES midpoint
  market_scan.edge_percent        -- (model - market) * 100
  market_scan.signal_label        -- BUY YES / BUY NO / MONITOR
  market_scan.confidence          -- low / medium / high
  market_scan.liquidity           -- market USD liquidity
  market_scan.sparkline           -- top bucket probabilities
  market_scan.anchor_model        -- which model anchors the forecast
  market_scan.primary_market      -- {question, slug, condition_id}

STRUCTURAL SIGNALS (detail endpoint only)
  vertical_profile_signal   -- CAPE, CIN, lifted index, BLH, shear
  taf.signal                -- TAF parsed segments, cloud/rain disruption
  dynamic_commentary        -- wind regime, weather phenomena notes
  official_nearby           -- nearby station network readings
  network_lead_signal       -- airport vs network delta
  metar_today_obs           -- all METAR observations today
```

## LLM Decision Prompt

For each city with a live market, assemble a structured prompt:

```
You are a temperature settlement market analyst. Given the following data
for {city} on {date}, decide whether to trade and how.

CURRENT STATE:
- Local time: {local_time}
- Current temp: {temp} | Max so far: {max_so_far}
- Peak window: {peak_hours} | Status: {peak_status}
- Deviation: {dev_label} ({dev_trend})
- Weather: {wx_desc} | Wind: {wind_dir} {wind_speed}kt
- METAR: {raw_metar}

MODEL FORECAST:
- DEB prediction: {deb_prediction}
- Multi-model: {model_list with values}
- Model range: {min} to {max} (spread: {spread})

PROBABILITY BUCKETS (model):
{formatted distribution table}

MARKET PRICES (Polymarket):
- Matched market: {question}
- Bucket: {bucket_value} | Model P: {model_p} | Market P: {market_p}
- Edge: {edge_percent}%
- Signal: {signal_label} | Confidence: {confidence}
- Liquidity: ${liquidity}
- All available bucket prices: {if fetched from CLOB}

STRUCTURAL SIGNALS:
- Surface: temp_delta={temp_delta}, dew_delta={dew_delta}, pressure_delta={pressure_delta}
- Wind evolution: {wind_evolution}
- Precip risk: {precip_pct}% window: {precip_window}
- Cloud cover delta: {cloud_delta}%
- Upper air: CAPE={cape}, CIN={cin}, LI={lifted_index}, BLH={blh}
- TAF: {taf_summary}
- Airport vs network: {delta}

CONTEXT:
- Settlement source: {settlement_source} ({station_code})
- Risk level: {risk_level} (airport distance: {distance_km}km)
- Nearby stations: {nearby_readings}

RULES:
1. Only recommend trades with |edge| > 3%
2. Scale position size by confidence:
   - high (|edge| > 8%): up to 5% of bankroll
   - medium (|edge| 5-8%): up to 2% of bankroll  
   - low (|edge| 3-5%): up to 1% of bankroll
3. Reduce size by 50% if:
   - risk_level = "high" (airport-city divergence)
   - liquidity < $1000
   - peak_status = "past" and trend is falling
   - heavy precip in peak window (>60%)
4. Do NOT trade if:
   - is_dead_market = true
   - market_scan.available = false
   - liquidity < $200
   - peak_status = "past" AND max_so_far is locked
5. For "or higher" / "or below" markets: sum model probabilities across the range
6. Prefer the model's TOP bucket (highest probability) not the matched market bucket
7. If warm bias is expanding pre-peak, lean toward higher buckets
8. If cool bias is expanding pre-peak, lean toward lower buckets

Respond in JSON:
{
  "action": "BUY_YES" | "BUY_NO" | "SKIP",
  "bucket": <integer>,
  "market_slug": "<slug for the bucket you'd trade>",
  "model_probability": <float>,
  "estimated_market_price": <float>,
  "edge_pct": <float>,
  "confidence": "high" | "medium" | "low",
  "size_pct_of_bankroll": <float>,
  "size_usd": <float>,  // assuming $1000 bankroll
  "reasoning": "<2-3 sentences>",
  "risk_factors": ["<factor1>", "<factor2>"],
  "time_sensitivity": "urgent" | "normal" | "wait"
}
```

## Script: `scripts/scan_alpha.py` (IMPLEMENTED)

```bash
# Dry run (prints to console, no Telegram)
python scripts/scan_alpha.py --dry-run

# Live run (pushes BUY YES signals to @notifyhotmath)
python scripts/scan_alpha.py

# Custom bankroll
python scripts/scan_alpha.py --bankroll 500
```

### Flow

1. Pulls all 48 cities from `GET /api/cities`
2. For each city, fetches `GET /api/city/{name}/detail`
3. Pre-filters: skip dead markets, past-peak, no market, closed order books, low liquidity (<$200)
4. Timing filter: only cities **1.5-6 hours before peak** (sweet spot for actionable edge)
5. Edge floor: `|edge| > 0.1%` passes to LLM (very low bar — LLM decides the real threshold)
6. Sorts candidates by absolute edge (largest first)
7. For each candidate, assembles full context and calls **Anthropic Claude** (or Groq fallback)
8. LLM returns `BUY_YES` or `SKIP` with reasoning, confidence, and position size
9. Only `BUY_YES` signals are pushed to Telegram

### LLM is extremely conservative by design

- Minimum edge 3% to even consider
- Must have clean structural confirmation (weather direction aligns with bucket)
- Halves size for high-risk cities, thin liquidity, precipitation risk
- Will SKIP if deviation bias contradicts the bucket direction
- Will SKIP if model probability < 25% or sigma too wide

### Env vars

```env
ANTHROPIC_API_KEY=sk-ant-...     # Primary LLM (Claude)
GROQ_API_KEY=gsk_...             # Fallback LLM (Llama 3.3 70B)
TELEGRAM_BOT_TOKEN=...           # For push to channel
TELEGRAM_CHAT_IDS=...            # Target channel(s)
```

## Position Sizing Matrix

| Edge | Confidence | Base Size | Risk=high | Liq<$1k | Precip>60% | Final Range |
|------|-----------|-----------|-----------|---------|------------|-------------|
| >15% | high | 5% | 2.5% | 2.5% | 2.5% | 1.25-5% |
| 8-15% | high | 5% | 2.5% | 2.5% | 2.5% | 1.25-5% |
| 5-8% | medium | 2% | 1% | 1% | 1% | 0.5-2% |
| 3-5% | low | 1% | 0.5% | 0.5% | 0.5% | 0.25-1% |
| <3% | any | 0% | -- | -- | -- | SKIP |

Bankroll example at $1000:

| Scenario | Size |
|----------|------|
| High edge + high confidence + low risk | $50 |
| High edge + high confidence + high risk city | $25 |
| Medium edge + thin market | $5-10 |
| Low edge | SKIP |

## Daily Routine

```
06:00 UTC  Wave 1 scan (APAC) -- Wellington, Tokyo, Seoul, HK, etc.
08:00 UTC  Wave 2 scan (South Asia + Middle East) -- Lucknow, Jeddah, Tel Aviv
10:00 UTC  Wave 3 scan (Europe + Africa) -- Moscow through London
14:00 UTC  Wave 4a scan (LatAm) -- Buenos Aires, Sao Paulo, Panama, Mexico
16:00 UTC  Wave 4b scan (US East) -- NY, Toronto, Miami, Atlanta
18:00 UTC  Wave 4c scan (US Central/West) -- Chicago, Dallas, Austin, Houston
20:00 UTC  Wave 4d scan (US West) -- Denver, LA, SF, Seattle

Each scan:
1. Run scan_alpha.py for that wave's cities
2. Review LLM recommendations
3. Execute manually on polymarket.com
4. Log trades in a local CSV/SQLite
```

## Key Gotchas

1. **Polymarket closes order books early** -- typically 2-4h before settlement. Scan BEFORE peak, not during.
2. **model_scan.available = false** does not mean no market exists -- it means the order book is closed or the market is past end time. Check the `reason` field.
3. **Bucket mismatch** -- the API sometimes matches a "14°C or below" market but reports edge against a single bucket. For compound markets, manually sum model probabilities.
4. **DEB needs history** -- a fresh instance has no settlement history, so DEB defaults to equal-weight average. After 3-7 days of running, predictions improve.
5. **Risk level matters** -- "high" risk cities (Chicago, Munich, Sao Paulo, Mexico City, etc.) have airports far from city center. The METAR reading may not represent actual city temp.
6. **Fahrenheit cities** -- US cities use °F buckets on Polymarket. The model outputs °F for these cities. Do not mix units.
7. **Dead markets** -- once `is_dead_market = true`, the settlement bucket is known. Only trade if market hasn't caught up to the obvious outcome.
