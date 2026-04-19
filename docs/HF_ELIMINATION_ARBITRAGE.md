# HF Elimination Arbitrage Strategy

**Status:** Proposed — do not implement yet
**Branch:** `experimental/hf-temperature-alpha`
**Date:** 2026-04-19

---

## 1. Core Insight

**Temperature is monotonic.** Once the daily maximum crosses into a higher bucket,
every lower bucket is **mathematically eliminated** from settling YES — it cannot
retroactively become the daily max.

Polymarket's daily-high markets settle on the **observed max**, and the market
updates on the hourly METAR cadence. Our HF (5-min weather.gov) data is
**10–50 minutes ahead** of the next METAR. When HF shows the max has already
crossed into a new bucket, **every bucket strictly below the current HF max is
dead** — but the market is still pricing some probability on those dead buckets.

**This flips the alpha problem upside down.** Instead of guessing which bucket
*will* settle (a probabilistic prediction with ~45–66% accuracy), we identify
which buckets *cannot* settle (a deterministic elimination with ~100% accuracy).

---

## 2. Live Example — San Francisco, Apr 19 2026 @ 21:40 UTC

HF chart (screenshot):
- Weather.gov 5-min dots reach **68°F at ~12:05 local**
- DEB forecast curve confirms sustained reading

Market snapshot at the same moment:

| Bucket | Market YES | Buy NO price | Status |
|---|---|---|---|
| **64-65°F** | 0% | 0.0¢ | already dead, correctly priced |
| **66-67°F** | 7% | **95¢** | **DEAD — HF max 68°F eliminates this bucket** |
| 68-69°F | 81% | 21¢ | LIVE (currently inside the bucket) |
| 70°F+ | 8% | 93.7¢ | LIVE (still possible later in the day) |

**The trade:** Buy NO on 66-67°F at 95¢.
- Payout: $1.00 if max ≠ 66-67°F (which is **already certain** — max is already 68°F)
- **Locked profit: 5.3¢ per $1 of NO purchased**
- Risk: only settlement-source error / data quality issues (see §7)

The market *will* reprice this bucket to ~99¢ NO once the 13:00 METAR is published
and everyone else sees 68°F. We want to be the ones who bought at 95¢ before that.

---

## 3. Why This Is Different From The Current Strategy

| Current speed-alpha | Elimination arbitrage |
|---|---|
| Predict which bucket WILL settle | Identify which buckets CANNOT settle |
| Accuracy: 44–66% (post-peak) | Accuracy: ~100% (monotonic physics) |
| Bet YES on one bucket | Bet NO on many buckets (one per city) |
| Requires model calibration | Requires only current HF max |
| Variance: high (binary bet) | Variance: near-zero per trade |
| Kelly q = 0.87–0.92 empirical | Kelly q = 0.99+ (truly certain) |
| Edge: model vs market | Edge: information vs market (pure latency) |

**The elimination trade is a strictly better alpha when available.** It has zero
model risk. The only risks are operational (data errors, settlement source
rules, tick-size / rounding edge cases).

---

## 4. Detection Algorithm

### Input

For each US city at scan time:
1. `current.max_so_far` (HF-overridden from `web/analysis_service.py`)
2. `hf_max_override.hf_max` + `hf_max_override.hf_bucket` (already computed)
3. `market_scan.available_buckets` — **we need to expand the scan to pull ALL
   buckets, not just the top-probability one** (see §5)
4. Settlement function: `apply_city_settlement(city, temp)` (existing)

### Rule

For each bucket `B` in the city's market:
- Compute `B_max`, the maximum temperature that would round into bucket `B`
  (e.g., for `66-67°F` the upper boundary with wu_round is 67.499…°F)
- If `hf_max > B_max`: bucket `B` is **ELIMINATED**
- If also `buy_no_price < 0.99`: **trade signal** with locked edge `1 - buy_no_price`

### Special cases per settlement rule

- **HKO floor** (Hong Kong, Lau Fau Shan): `floor(temp)` — bucket `30` eliminated
  when `hf_max ≥ 31.0°C`, NOT when `hf_max > 30.5`
- **wu_round half-up** (most cities): bucket `79` eliminated when `hf_max ≥ 79.5`
- **Band settlement** (some cities): bucket `[68.5, 69.5)` eliminated when
  `hf_max ≥ 69.5`

The existing `apply_city_settlement()` handles all three. We just ask it:
"what bucket does `hf_max` round to?" → any bucket strictly below that is
eliminated.

### Multi-bucket elimination cascade (single snapshot)

One city can eliminate **multiple consecutive buckets at once** if temperature
jumps between observations.

Example — HF at SF jumps from 65°F → 68°F in 5 minutes:
- Eliminates 64-65°F ✓
- Eliminates 66-67°F ✓
- Two NO trades from one HF reading

### Sequential elimination cascade (throughout the day) — THE REAL OPPORTUNITY

The far bigger alpha: **each new bucket crossed during the day is a fresh,
independent trade**. Temperature rises monotonically from morning minimum to
afternoon peak, and each bucket boundary crossed is a permanent elimination
that we can exploit as it happens.

**Live example — New York Apr 19, single day trajectory:**

| Time | HF reading | Bucket entered | Buckets now dead | Trade fires |
|---|---|---|---|---|
| 02:10 | 55.4°F | 54-55°F | 52-53°F and below | 1 elim trade |
| 05:00 | 53°F (overnight min) | 52-53°F | — | — |
| 08:45 | 56.2°F | 56-57°F | 52-55°F (2 new buckets) | 2 elim trades |
| 10:30 | 59.8°F | 58-59°F | 56-57°F | 1 elim trade |
| 11:15 | 62.0°F | 62-63°F | 58-61°F (2 new) | 2 elim trades |
| 12:00 | 64.3°F | 64-65°F | 62-63°F | 1 elim trade |
| 13:30 | 66.5°F | 66-67°F | 64-65°F | 1 elim trade |
| 14:45 | 68.0°F | 68-69°F | 66-67°F | 1 elim trade |
| 15:30 | 68.0°F (stable, peak) | — | — | — |

**Total:** 9 elimination trades fired from a single city on a single day, each
with its own ~10-50 minute alpha window before the next METAR catches up.

### Why this matters for sizing and frequency

- **Cities × peak-hour window × buckets-crossed-per-day** = total opportunity
- Typical US summer day: 10 cities × 5-8 buckets crossed = **50-80 elim trades**
- Even at $20 avg position with 3% avg edge = $30-50/day realized P&L on
  modest capital

### State tracking: per-bucket cooldown per city per day

Because the same city fires multiple trades through the day, we must track
**which buckets we've already traded** to avoid duplicate fills:

```python
# Key format: (city, date, bucket_label) → traded_at_timestamp
_elim_cooldown: dict[tuple[str, str, str], float] = {}

def _elim_already_traded(city: str, date: str, bucket_label: str) -> bool:
    return (city, date, bucket_label) in _elim_cooldown
```

Unlike the existing `@postpeak` cooldown (one trade per city per day at the
predicted bucket), elim-arb cooldown is **per-bucket** — a city can legitimately
fire 5-8 distinct elim trades on the same day, one per bucket boundary crossed.

### Ordering and re-entry rules

1. **First-cross wins:** fire ONLY on the first observation that crosses a
   bucket boundary. Subsequent HF readings in the same or higher bucket should
   NOT re-fire the same bucket.
2. **Skip-gaps:** if HF jumps 3°F in 5 min and crosses 2 bucket boundaries at
   once, fire both trades simultaneously (each with its own cooldown entry).
3. **No re-entry on retracement:** if temperature dips back below the boundary,
   do NOT fire again when it crosses back. The bucket is already eliminated
   permanently by the earlier reading.
4. **Day-boundary reset:** at local midnight, reset all elim cooldowns for the
   new day's markets.

---

## 5. Data Pipeline Changes

### 5.1 Backend — expose ALL buckets, not just the top

Currently `market_scan` returns a single "selected" bucket/slug. We need the
full bucket ladder to detect eliminations.

**Location:** `web/market_scan.py` (or wherever `build_market_scan` lives).

**Addition:** `market_scan.all_buckets: List[BucketInfo]` where each
`BucketInfo` has:
- `label` ("66-67°F", "30°C", etc.)
- `bucket_low`, `bucket_high` (inclusive °F or °C)
- `slug`
- `liquidity`
- `yes_price` (best-ask YES)
- `no_price` (best-ask NO)
- `last_trade_price`
- `yes_bid`, `yes_ask`, `no_bid`, `no_ask` (for spread awareness)

### 5.2 Backend — elimination field on city detail

Add to analysis result:
```python
"elimination_analysis": {
  "hf_max": 68.0,
  "hf_max_time": "12:05",
  "hf_bucket": "68-69°F",
  "hf_current_bucket_upper": 69.499,
  "eliminated_buckets": [
    {
      "label": "64-65°F",
      "slug": "...64-65f",
      "no_price": 0.999,
      "locked_edge_pct": 0.1,
      "liquidity": 4837,
      "no_size_available": ...,
      "eliminated_at_utc": "2026-04-19T18:45:00Z",   # first HF tick that killed it
      "eliminated_by_temp": 66.0,                    # HF reading that crossed
    },
    {
      "label": "66-67°F",
      "slug": "...66-67f",
      "no_price": 0.95,
      "locked_edge_pct": 5.3,
      "liquidity": 3680,
      "no_size_available": ...,
      "eliminated_at_utc": "2026-04-19T20:05:00Z",
      "eliminated_by_temp": 68.0,
    }
  ],
  "live_buckets": ["68-69°F", "70°F or higher"],
  "newly_eliminated_this_tick": ["66-67°F"],   # buckets eliminated since last scan
}
```

The `newly_eliminated_this_tick` field is the key for scanner logic — it tells
us which buckets just crossed on THIS scan vs which were already dead before.
Only newly-eliminated buckets should trigger a fresh Telegram alert.

### 5.3 Scanner — new pathway in `scripts/scan_alpha.py`

Add an `evaluate_elimination_trades(detail)` function that:
1. Reads `elimination_analysis.eliminated_buckets`
2. Filters for `no_price < 0.98` AND `locked_edge_pct >= 1.5%` (tunable floor
   to cover Polymarket fees + slippage)
3. Filters for sufficient NO-side liquidity (`≥ $500` depth)
4. **Skips buckets already in per-bucket elim cooldown** (see state tracking
   above) — this is critical because one city will fire multiple elim trades
   during the day, one per bucket boundary crossed
5. Produces a separate `elim_trades` list parallel to the existing `buys` list

**Per-bucket cooldown state:**

```python
# Separate cooldown dict from the existing @postpeak cooldown.
# Key: (city, date, bucket_label). Entries are NEVER cleared within the day —
# once a bucket is traded it stays traded. Cleared at local midnight.
_elim_cooldown: dict[tuple[str, str, str], float] = {}

def _is_elim_on_cooldown(city: str, date: str, bucket_label: str) -> bool:
    return (city, date, bucket_label) in _elim_cooldown

def _mark_elim_pushed(city: str, date: str, bucket_label: str) -> None:
    _elim_cooldown[(city, date, bucket_label)] = time.time()

def _reset_elim_cooldown_for_new_day() -> None:
    """Call at midnight UTC (or per-city local midnight) to reset trades."""
    _elim_cooldown.clear()
```

Distinct from the existing `@postpeak` cooldown (one trade per city per day
at the predicted bucket). The elim cooldown is per-bucket so a city can fire
5-8 distinct elim trades in a single day as temperature rises through
consecutive boundaries.

### 5.4 Telegram — new section or channel

Option A: **Reuse `@postpeak`** with a distinct `ELIM_ARB` header
Option B: **New channel `@postpeak_elim`** for elimination-only signals

Recommend **Option B** because the trade mechanics are different (NO side,
multiple per city, different sizing logic) and we want to evaluate the
backtested PnL separately without polluting the existing feed.

Each scan may produce multiple elim-arb trades across different cities AND
multiple buckets from the same city. The Telegram message must present them
clearly with one ACTION block per trade:

```
[ELIM-ARB] 3 trades this cycle

=== SAN FRANCISCO ===
  hf_max=68.0°F @ 12:05 (KSFO) | 226 obs @ 5min cadence
  session trajectory: 52°F(00:15)→56°F(08:45)→62°F(11:15)→68°F(12:05)
  buckets eliminated today so far: [52-53, 54-55, 56-57, 58-59, 60-61, 62-63, 64-65, 66-67]
  buckets already traded (cooldown): [52-53, 54-55, 56-57, 60-61, 62-63, 64-65]
  NEWLY ELIMINATED THIS CYCLE: [66-67°F]
  live buckets: [68-69°F (current), 70°F+]

  [1] TRADE: BUY_NO 66-67°F @ 95c  (edge=5.3%  liq=$3,680)
      market_slug=highest-temperature-in-san-francisco-on-april-19-2026-66-67f
      target_fill_price<=0.955  min_size_usd=$50  max_size_usd=$50
      eliminated_at=12:05  crossed_by=68.0°F  bucket_upper=67.499°F
      time_to_metar_confirm=~18min
      rationale: HF 68.0°F > 67.499°F (bucket upper); daily max cannot retreat
      url=https://polymarket.com/market/highest-temperature-in-san-francisco-on-april-19-2026-66-67f

=== HOUSTON ===
  hf_max=80.2°F @ 13:40 (KHOU) | 198 obs @ 5min cadence
  session trajectory: 58°F(00:00)→65°F(08:00)→72°F(11:00)→78°F(13:00)→80.2°F(13:40)
  NEWLY ELIMINATED THIS CYCLE: [76-77°F, 78-79°F]   <-- cascade: 2 trades

  [2] TRADE: BUY_NO 76-77°F @ 97c  (edge=3.0%  liq=$2,100)
      market_slug=...-76-77f  ...

  [3] TRADE: BUY_NO 78-79°F @ 89c  (edge=11.0%  liq=$4,500)
      market_slug=...-78-79f  ...
      *** HIGH EDGE — likely market hasn't seen 13:40 HF reading yet ***

=== SUMMARY ===
  total locked edge capital: $150  expected P&L: $6.80
  all 3 trades fire in parallel; no inter-dependency
```

**Message design principles:**
- One ACTION line per distinct BUY_NO trade
- Show session trajectory so AI can validate the monotonic-rise claim itself
- Highlight high-edge trades (>8%) with `***` markers for human sanity check
- Always include per-bucket elim cooldown context so the AI doesn't re-trade

---

## 6. Position Sizing

### Kelly with q ≈ 1.0

For an elimination trade with edge `e = 1 - no_price`:
- Full Kelly fraction = `(q - no_price) / (1 - no_price)` with `q → 1.0`
- For `no_price = 0.95`, full Kelly = `(1.0 - 0.95) / (1 - 0.95) = 1.0` = **100%**
- For `no_price = 0.98`, full Kelly = `(1.0 - 0.98) / (1 - 0.98) = 1.0` = **100%**

Full Kelly says "bet everything." Clearly we cannot do that. Caps:

| Cap | Rationale |
|---|---|
| **5% of bankroll per trade** | Operational + settlement risk |
| **15% of bankroll total per day across all elim trades** | Correlated settlement-source risk (one NOAA outage could affect multiple cities) |
| **$X = min(5% bankroll, 30% of bucket NO-side liquidity, $500)** | Avoid walking the book |
| **min edge 1.5%** | Below this the Polymarket fee (~2% round-trip in some cases) eats the profit |
| **min observations since HF crossing: 3** | One outlier reading doesn't trigger |
| **min gap from bucket boundary: 0.3°F** | Settlement rounding safety margin |

### Expected value

With `no_price = 0.95`, `q = 0.995` (100-bps haircut for operational risk):
- EV per $1 = `0.995 * 1 - 1 = -0.005 + 0.05 = 4.45¢` locked
- Required capital to lock $1 of profit: ~$21 at no_price=0.95
- Scales **quadratically** with how aggressive we want to be on edge floor

---

## 7. Risks and Mitigations

### 7.1 Data quality — HF sensor error

- Single spike reading (e.g., 68°F for 1 minute then back to 65°F) → we bought
  NO on a bucket that IS valid
- **Mitigation:** require the HF max to be **sustained ≥ 3 observations** (~15
  min on weather.gov 5-min feed) before confirming the bucket crossing
- **Mitigation:** cross-validate against cluster stations (e.g., KSFO + KOAK +
  KSJC for Bay Area) — require majority to show max above the bucket boundary
- **Mitigation:** reject if HF `median_gap_minutes > 15` (station not reporting
  reliably)

### 7.2 Settlement source revision

- NOAA has issued corrections to ASOS data historically, though extremely rarely
  for observed max (< 0.1% of station-days)
- Polymarket's settlement committee uses the "finalized" reading at EOD, not the
  real-time feed we're reading
- **Mitigation:** only act on HF if the reading is ≥ 0.5°F above the bucket
  boundary (avoid edge cases where NOAA might round differently at QC time)
- **Mitigation:** additional margin for settlement sources known to be volatile
  (HKO for Hong Kong, MGM for Turkish cities)

### 7.3 Bucket boundary ambiguity — wu_round vs floor vs band

- Different cities use different rounding rules; we must use
  `apply_city_settlement()` to determine the exact boundary
- **Mitigation:** `apply_city_settlement(city, bucket_upper_bound + 0.01)` must
  return a strictly higher bucket than the eliminated one — validate this
  programmatically before emitting the signal
- **Example:** for SF `wu_round(67.5) = 68`, so bucket 66-67°F eliminated iff
  `hf_max ≥ 67.5`. If `hf_max = 67.4` we do NOT fire.

### 7.4 Market-side mechanics

- **Slippage:** executing $X of NO at the 95¢ ask may move the price
  - Mitigation: size ≤ 30% of NO-side order book depth
- **Fees:** Polymarket charges on entry + exit; if we hold to settlement
  there's no exit fee but entry fee is ~0%
- **Resolution delay:** if Polymarket delays settlement we tie up capital
  - Mitigation: hold period for elim trades is ≤ 12 hours (same-day)

### 7.5 Correlated failures

- If our HF source (weather.gov) goes down mid-day, we rely on METAR only
  - Mitigation: compute `data_staleness` = minutes since last HF observation;
    block new elim trades if `> 10 min`
- If settlement source itself is in question (rare NOAA outage), many cities
  affected simultaneously
  - Mitigation: daily cap of 15% bankroll across all elim trades

### 7.6 Adversarial "squeeze" on illiquid buckets

- Eliminated buckets may have thin liquidity and price-discovery traders
  specifically
- Mitigation: require `liq ≥ $500` on NO side; require `no_price < 0.99`
  (above this the absolute edge is too small to matter anyway)

### 7.7 Cascade-specific: duplicate-fire protection

- The same bucket boundary could be "newly eliminated" on successive scans
  if our elimination-analysis code is stateless
- A naive implementation would re-fire 5 elim trades for 66-67°F in SF over
  the course of 25 minutes (5 scan cycles at 5-min interval)
- **Mitigation:** per-bucket elim cooldown keyed by `(city, date, bucket)` is
  mandatory — never clear within the day even if temperature dips back below
  boundary
- **Mitigation:** `elimination_analysis.newly_eliminated_this_tick` is computed
  on the backend by comparing against the previous cached scan's
  `eliminated_buckets` — this prevents the stateless scanner from re-firing

### 7.8 Cascade-specific: bucket-above partial fill tracking

- We buy NO on 64-65°F at 13:15, then HF crosses into 66-67°F at 13:30 and
  we want to buy NO on that bucket too
- If our first fill only partially executed, we still have capital allocated
  to the earlier trade; need to ensure we don't over-deploy
- **Mitigation:** the daily cap (15% bankroll across all elim trades) is
  computed on nominal capital at risk, not locked fills — so partial fills
  don't free up capacity until actually filled

---

## 8. Backtest Plan

Before going live:

1. **Historical pull:** for each US city × each day in last 30 days, reconstruct:
   - HF 5-min series (from weather.gov or IEM ASOS 1-min archive)
   - METAR hourly series
   - Polymarket bucket ladder + NO prices at each 5-min tick

2. **Simulate elim-arb:** at every 5-min tick, if a bucket just became
   eliminated AND NO price < 0.98, record a virtual $100 NO buy

3. **Metrics:**
   - Win rate (should be ~99.5%+ if theory holds)
   - Average locked edge per trade
   - Total PnL per day and per city
   - Distribution of `time-to-METAR-catch-up` (how long until the market
     reprices to 0.99+)
   - Empirical fail cases (what were the <1% losses actually?)

4. **Tune the thresholds:**
   - Minimum edge (try 1%, 2%, 3%, 5%)
   - Minimum hold time vs market-repricing speed
   - Maximum trades per day

5. **Pass/fail gate for production:**
   - Win rate ≥ 98.5%
   - Sharpe over 30 days ≥ 3.0
   - Max drawdown ≤ 5% of deployed capital
   - No single-day loss > bankroll cap

---

## 9. Rollout Plan

### Phase 1 — Data plumbing (no trading)
- [ ] Extend `market_scan.all_buckets` to expose the full bucket ladder with
      NO prices
- [ ] Add `elimination_analysis` block to `/api/city/{name}` response
- [ ] Display eliminated buckets in the frontend intraday panel as a **red
      "ELIMINATED"** badge on dead buckets (visual sanity check)
- [ ] Log every detected elimination to `data/alpha_logs/eliminations.jsonl`
      for backtest replay

### Phase 2 — Dry run (no Telegram)
- [ ] Add `evaluate_elimination_trades()` to `scan_alpha.py`
- [ ] Print trades with full verbose format to stdout
- [ ] Run for 7 days in parallel with existing speed-alpha
- [ ] Compare against market reprice lag to confirm our edge is real

### Phase 3 — Telegram pilot (`@postpeak_elim`)
- [ ] Create `@postpeak_elim` channel
- [ ] Wire up Telegram push with verbose messages
- [ ] Small capital ($100 bankroll) for 14 days of real trades
- [ ] Track actual fill prices, settlement outcomes, PnL

### Phase 4 — Scale
- [ ] Increase capital per backtest-validated Kelly size
- [ ] Add cluster-station cross-validation
- [ ] Consider expanding to non-US cities once alternative HF sources identified

---

## 10. Metrics To Track Going Forward

- **Elimination detection latency:** minutes between HF bucket-cross and our
  signal emission
- **Market reprice latency:** minutes between our signal and NO price reaching
  ≥ 0.99
- **Edge capture efficiency:** `(our_fill_price - final_price) / theoretical_edge`
- **Win rate:** settled NO trades / total NO trades (target ≥ 99%)
- **Daily P&L** attributable to elim-arb vs existing speed-alpha
- **Cities contributing most alpha** (likely Houston, Dallas, Atlanta in warm
  months; Chicago, Seattle in shoulder seasons)
- **Time-of-day heatmap** of when eliminations fire most (expect late-morning
  rise through peak)

---

## 11. Why This Is Different From "Buying NO On A 99¢ Market"

Buying NO on any market at 99¢ is **not free money** because:
- You risk 99¢ to make 1¢
- If you're wrong you lose 99¢
- Win rate has to be ≥ 99% just to break even

**Elimination arbitrage is fundamentally different:**
- Physics (monotonic max) guarantees win rate approaches 100%
- We buy when NO is **mispriced** (~90–97¢) due to HF latency
- The 3–10¢ edge vs "true" NO price (~99¢+) is not a probabilistic edge —
  it's an **information latency edge**
- The market WILL reprice to 99¢ within 10–50 minutes of the next METAR;
  we're simply the first to observe the HF crossing

The edge disappears entirely once METARs catch up, but we've already locked the
fill. It's the temporal analogue of a true arbitrage.

---

## 12. Open Questions (Decide Before Implementation)

1. **Single channel or separate?** `@postpeak` combined vs `@postpeak_elim`
   separate. Recommend separate for PnL attribution and message clarity.
2. **Auto-execute vs alert-only?** Telegram message only, or push to a
   trading bot? Recommend **alert-only initially** to observe real-world
   market behavior before automating.
3. **Hedge against HF source outage?** Require both weather.gov AND
   a METAR reading in the cross-validation, OR accept HF-only and carry
   operational risk? Recommend: HF-only but with cluster-station check.
4. **Expiration handling:** if we buy NO on 66-67°F and Polymarket delays
   settlement, we tie up capital. Cap holding period at 24h.
5. **Non-US cities:** AWC METAR + SPECI gives us 30-60 min resolution.
   Is 30-60 min still enough alpha vs hourly METAR? Probably yes but needs
   separate backtest — defer.
6. **Partial-bucket "live" filter:** when HF shows the max is **within** the
   current bucket (68.2°F → 68-69°F live), do we do anything special? Probably
   not — it's still live, but we could publish "bucket approaching upper
   boundary" as a warning for the adjacent-bucket YES trade.

---

## 13. Summary

**The elimination arbitrage is pure latency arbitrage enabled by the HF data
we already fetch.** The backend changes needed are minimal (~1 new field on
the city detail endpoint + full bucket ladder from market_scan). The scanner
and Telegram pipeline gets one new entry path with a simpler, higher-confidence
signal than the existing speed-alpha.

**Critical insight: cascade per city.** The signal is NOT one-shot per city
— temperature rises through multiple bucket boundaries during the day and
each crossing is a fresh, independent arb. Per-bucket cooldown state tracks
which buckets we've already traded so we can fire 5–8 distinct elim trades
per city per day.

Expected daily volume on a $1,000 bankroll:

| Scale | Cities × buckets | Trades/day | Avg edge | Avg size | Daily P&L |
|---|---|---|---|---|---|
| Conservative (current HF enabled for 10 US cities) | 10 × 5 | 50 | 3% | $15 | ~$22 |
| Moderate (tighter edge floor, more per-city buckets) | 10 × 7 | 70 | 3.5% | $20 | ~$49 |
| Aggressive (add cluster validation, lower edge floor) | 10 × 8 | 80 | 2.5% | $25 | ~$50 |

Key scaling levers:
- **Bankroll:** linear up to per-bucket liquidity cap (~$500-2000 NO side)
- **Cities covered:** add more US ASOS stations (Denver, Phoenix, Seattle, etc.)
- **International:** if we get sub-hourly data for EU/Asia aerodromes, same
  strategy applies with smaller edge windows
- **Season:** highest volume in transition months (spring/fall) when diurnal
  temperature swing crosses the most bucket boundaries; summer days with
  slow steady rise fire 6-8 trades per city, spring days with rapid warm-up
  can fire 10+

**This is worth building.** The multi-bucket cascade turns what looks like
a 3-8 trade/day strategy into a 50-80 trade/day strategy, dramatically
improving Sharpe via trade count even with small per-trade edge.
