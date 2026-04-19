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

### Multi-bucket elimination cascade

One city can eliminate **multiple consecutive buckets at once**.

Example — HF at SF jumps from 65°F → 68°F:
- Eliminates 64-65°F ✓
- Eliminates 66-67°F ✓
- Two NO trades from one HF reading

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
  "eliminated_buckets": [
    {
      "label": "64-65°F",
      "slug": "...64-65f",
      "no_price": 0.999,
      "locked_edge_pct": 0.1,
      "liquidity": 4837,
      "no_size_available": ...,
    },
    {
      "label": "66-67°F",
      "slug": "...66-67f",
      "no_price": 0.95,
      "locked_edge_pct": 5.3,
      "liquidity": 3680,
      "no_size_available": ...,
    }
  ],
  "live_buckets": ["68-69°F", "70°F or higher"],
}
```

### 5.3 Scanner — new pathway in `scripts/scan_alpha.py`

Add an `evaluate_elimination_trades(detail)` function that:
1. Reads `elimination_analysis`
2. Filters for `no_price < 0.98` AND `locked_edge_pct >= 1.5%` (tunable floor
   to cover Polymarket fees + slippage)
3. Filters for sufficient NO-side liquidity (`≥ $500` depth)
4. Produces a separate `elim_trades` list parallel to the existing `buys` list

### 5.4 Telegram — new section or channel

Option A: **Reuse `@postpeak`** with a distinct `ELIM_ARB` header
Option B: **New channel `@postpeak_elim`** for elimination-only signals

Recommend **Option B** because the trade mechanics are different (NO side,
multiple per city, different sizing logic) and we want to evaluate the
backtested PnL separately without polluting the existing feed.

The elim-arb message must include:
```
[ELIM-ARB] SAN FRANCISCO | hf_max=68°F @ 12:05 (KSFO) | 226 obs @ 5min cadence
  Eliminated buckets (can't win YES):
    - 64-65°F  NO=0.999  edge=0.1%  liq=$4,837  [skip: below threshold]
    - 66-67°F  NO=0.950  edge=5.3%  liq=$3,680  **TRADE**
  Live buckets:
    - 68-69°F (current)  YES=0.82
    - 70°F+  YES=0.08  (possible if warming continues)

  ACTION: BUY_NO  market_slug=highest-temperature-in-san-francisco-on-april-19-2026-66-67f
    target_fill_price<=0.955  min_size_usd=$50  max_size_usd=$X (see sizing)
    locked_edge_pct=5.3  time_to_metar_confirm=~18min
    rationale: HF max 68°F already exceeds upper bound of 66-67°F bucket.
               Monotonic daily max cannot re-enter this bucket.
               Risk: settlement source revision / sensor error (historical <0.1%).
    url=https://polymarket.com/market/highest-temperature-in-san-francisco-on-april-19-2026-66-67f
```

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

Expected outcome on a $1,000 bankroll:
- 3–8 elimination trades per peak-hour window in US cities during warm
  season
- Average locked edge ~3% per trade
- Daily P&L contribution ~$5–20 from elim arb alone at current scale
- Scales linearly with bankroll up to the liquidity cap on each bucket

**This is worth building.**
