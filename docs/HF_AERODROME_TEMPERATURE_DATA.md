# High-Frequency Aerodrome Temperature Data Sources

Investigation of providers offering minute-by-minute (or near-minute) temperature observations at aerodromes, including AWOS raw data, ASOS 1-minute archives, and high-frequency METAR/SPECI feeds.

---

## 1. ASOS 1-Minute Data (USA)

The **Automated Surface Observing System (ASOS)** is a joint NWS/FAA/DOD program with 900+ sites across the United States. ASOS records observations continuously and archives data at **1-minute intervals**, making it the highest-resolution publicly available aerodrome temperature dataset in the world.

### What is recorded (1-min)

| Parameter | Unit | Notes |
|---|---|---|
| Air Temperature | deg F | 1-min instantaneous reading |
| Dew Point Temperature | deg F | 1-min instantaneous reading |
| Wind Speed / Direction | knots/deg | Includes 5-sec gust |
| Visibility | statute miles | Multiple sensor readings |
| Station Pressure | inHg | Up to 3 sensors |
| Precipitation | inches | 1-min accumulation |

### Access Methods

#### a) NCEI (NOAA) Direct Archive

Official archive hosted by the National Centers for Environmental Information.

- **1-Minute Page 1** (wind, temp, dew point, pressure): https://www.ncei.noaa.gov/data/automated-surface-observing-system-one-minute-pg1/
- **1-Minute Page 2** (precipitation, visibility): https://www.ncei.noaa.gov/data/automated-surface-observing-system-one-minute-pg2/
- **5-Minute Data**: https://www.ncei.noaa.gov/data/automated-surface-observing-system-five-minute/
- Format: Fixed-width text files, organized by station and year-month
- Coverage: ~2000 to present
- Cost: **Free**
- Latency: Data lags by approximately 1 month

#### b) Iowa Environmental Mesonet (IEM) -- Recommended

The IEM at Iowa State University ingests and reprocesses the NCEI 1-minute ASOS archive into a user-friendly download interface. This is the easiest way to get 1-minute ASOS temperature data.

- **Download Interface**: https://mesonet.agron.iastate.edu/request/asos/1min.phtml
- **API Backend**: https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py?help
- Features:
  - Select individual or multiple stations (all US ASOS sites)
  - Choose timezone (UTC, Eastern, Central, Mountain, Pacific, Alaska)
  - Select specific variables (temperature, dew point, wind, pressure, precip, visibility)
  - Configurable sampling interval (every 1, 5, 10, 20 min, or hourly)
  - CSV/TSV output, scriptable
- Archive available up to: ~1-2 days lag from NCEI feed (as of Apr 2026, up to 16 Apr 2026)
- Coverage: 2000-present for most ASOS sites
- Cost: **Free**
- Ideal for: Bulk historical 1-minute temperature retrieval at US aerodromes

#### c) NCEI Bulk FTP

Raw archive files organized by year:
- https://www.ncei.noaa.gov/pub/data/noaa/ (Global Hourly / ISD)
- 1-minute files also available via FTP at NCEI

---

## 2. AWOS Data (USA - FAA)

The **Automated Weather Observing System (AWOS)** is operated by the FAA. These are generally older stations that predate ASOS. AWOS stations report at **20-minute intervals** (not 1-minute) and do not issue SPECI (special observations).

### AWOS vs ASOS

| Feature | AWOS | ASOS |
|---|---|---|
| Operator | FAA | NWS/FAA/DOD |
| Report Interval | Every 20 min | Every 1 min (archived), hourly (METAR) |
| SPECI Reports | No | Yes |
| Sensor Suite | Basic (varies by type) | Comprehensive |
| Number of Sites | ~900 | ~900 |
| Data Archive | NCEI (via ISD/Global Hourly) | NCEI (1-min, 5-min, hourly) |

### AWOS Types

- **AWOS-A**: Altimeter setting only
- **AWOS-1**: Altimeter, wind speed/direction, temperature, dew point
- **AWOS-2**: AWOS-1 + visibility
- **AWOS-3**: AWOS-2 + cloud/ceiling
- **AWOS-3P**: AWOS-3 + precipitation identification
- **AWOS-3PT**: AWOS-3P + thunderstorm/lightning detection
- **AWOS-3T**: AWOS-3 + thunderstorm/lightning detection (no precip ID)

### Access

- AWOS data flows into the **Integrated Surface Dataset (ISD)** at NCEI and is also available through the Global Hourly product.
- Download: https://www.ncei.noaa.gov/access/search/data-search/global-hourly
- AWOS data does **not** have a 1-minute archive. The 20-minute reports are the native resolution.
- Some AWOS data is available in real-time via MADIS (see Section 5).
- Cost: **Free**

### Key Limitation

AWOS is 20-minute resolution at best. For true minute-by-minute temperature data at US aerodromes, **ASOS 1-minute data is the primary source**.

---

## 3. METAR / SPECI / HF METAR

### Standard METAR

METAR (Meteorological Aerodrome Report) is the international standard for aerodrome weather observations. Standard METARs are issued:
- **Hourly** at most aerodromes (e.g., at :50 or :00 past the hour)
- **Half-hourly** at some high-traffic airports

Temperature in METAR is reported as whole-degree Celsius (e.g., `15/08`), which limits precision. Some METAR variants include a temperature/dew-point remarks group with 0.1 degC resolution (e.g., `T01560083` = 15.6C / 8.3C).

### SPECI (Special Reports)

SPECI reports are unscheduled METARs triggered by significant weather changes (visibility drops, wind shifts, thunderstorms, etc.). At busy aerodromes, SPECI reports can be frequent, effectively increasing the observation cadence during weather events to near-continuous.

### High-Frequency METAR

Some countries and airport authorities issue METARs more frequently than standard hourly:

| Country/Region | Typical Frequency | Notes |
|---|---|---|
| USA (ASOS) | Hourly + SPECI (raw data: 1-min) | 1-min data available via NCEI/IEM (see Section 1) |
| Canada (AWOS) | Every 20 min (some every 10 min) | Nav Canada operates AWOS; data via Environment Canada |
| UK (Met Office) | Every 30 min at major airports | Available via OGIMET, Met Office DataPoint |
| Germany (DWD) | Every 30 min | Available via DWD Open Data, OGIMET |
| France (Meteo-France) | Hourly + frequent SPECI | Available via OGIMET |
| Australia (BoM) | Every 30 min at major airports, some half-hourly AWOS | Available via BoM website |
| Hong Kong (HKO) | Every 30 min | HKO Aviation Weather Services |
| Singapore (Changi) | Every 30 min | Available via Changi Met |
| Middle East (major hubs) | Hourly + frequent SPECI | UAE, Qatar, Saudi -- via OGIMET or direct met authority |

### Access Methods for METAR/SPECI

#### a) Aviation Weather Center (AWC) -- NOAA

- **URL**: https://aviationweather.gov/data/metar/
- **Data API**: https://aviationweather.gov/api/data/metar?ids=KJFK&format=json
- Features:
  - METAR + SPECI from worldwide stations
  - Last 15 days of data
  - Formats: raw text, JSON, GeoJSON, CSV, XML, IWXXM
  - Cache files updated every minute: https://aviationweather.gov/data/cache/metars.cache.csv.gz
- Rate limit: 100 requests/minute
- Coverage: Worldwide (all ICAO reporting stations)
- Cost: **Free**
- Limitation: Only standard METAR/SPECI interval, not sub-METAR (not 1-minute)

#### b) OGIMET

- **METAR Query**: https://ogimet.com/metars.phtml.en
- Features:
  - Worldwide METAR, SPECI, and TAF query interface
  - Historical data back to 2005
  - Can filter by SA (METAR+SPECI), SP (SPECI only), FC (short TAF), FT (long TAF)
  - HTML or plain text output
- Coverage: Global (receives reports via GTS -- WMO Global Telecommunication System)
- Cost: **Free** (community/research service, request fair use)
- Ideal for: Historical METAR/SPECI retrieval worldwide

#### c) Iowa Environmental Mesonet (IEM) -- METAR/SPECI

- **Download**: https://mesonet.agron.iastate.edu/request/download.phtml
- Provides decoded METAR/SPECI data for all ASOS/AWOS stations
- Scriptable API with CSV output
- Good for supplementing 1-minute data with human-augmented METARs

---

## 4. MADIS (Meteorological Assimilation Data Ingest System)

NCEP/NOAA's MADIS aggregates real-time surface observations from multiple networks into a single QC'd database.

- **URL**: https://madis-data.ncep.noaa.gov/
- **Data Application**: https://madis.ncep.noaa.gov/data_application.shtml
- Includes:
  - ASOS and AWOS observations
  - State/regional mesonets (hundreds of additional surface stations)
  - RAWS, COOP, maritime, and other networks
- Format: netCDF files with QC flags
- Access: Requires registration (free for government, research, education)
- Update frequency: Near real-time (sub-hourly for many mesonets)
- Mesonet stations often report at **5-minute** intervals

### Relevance to Aerodrome Temperature

MADIS integrates ASOS/AWOS data alongside dense mesonet data. For aerodromes with adjacent mesonet stations, MADIS can provide supplementary sub-hourly temperature observations.

---

## 5. International Aerodrome Observation Systems

### Canada -- Nav Canada / Environment Canada

- AWOS operated by Nav Canada at ~200 airports
- Reports at 20-minute intervals (some at 10 minutes)
- Historical data: https://climate.weather.gc.ca/ (Climate Data Online)
- Real-time METAR/SPECI via AWC or OGIMET

### United Kingdom -- Met Office

- Observations at major aerodromes at 30-minute intervals
- **DataPoint API**: https://www.metoffice.gov.uk/services/data/datapoint
- Provides hourly observations (not sub-hourly via public API)
- 30-min METARs available via OGIMET

### Germany -- DWD (Deutscher Wetterdienst)

- **Open Data Portal**: https://opendata.dwd.de/
- 10-minute observations at climate stations (many at/near aerodromes)
- Path: `opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/`
- Parameters include air temperature at 2m, humidity, pressure
- Coverage: ~400 stations across Germany
- Cost: **Free** (open data policy)
- This is one of the best freely available sub-hourly aerodrome temperature sources outside the US.

### Australia -- Bureau of Meteorology (BoM)

- Half-hourly observations at major airports
- Real-time data: http://www.bom.gov.au/aviation/
- Historical 30-min data available for some stations
- METAR/SPECI via OGIMET or AWC

### Japan -- Japan Meteorological Agency (JMA)

- AMeDAS network: 10-minute observations at ~1300 stations
- Some co-located with aerodromes
- Data access: https://www.jma.go.jp/jma/en/Activities/amedas/amedas.html

---

## 6. Commercial / API Providers

### a) Meteomatics Weather API

- **URL**: https://www.meteomatics.com/en/weather-api/
- Offers observation station data alongside model data
- Claims "minute-by-minute" resolution globally via downscaling
- METAR station observations available through the API
- Includes historical archives
- Pricing: Commercial (tiered plans)

### b) OpenWeatherMap

- **URL**: https://openweathermap.org/api
- One Call API 3.0 claims "updated every minute" for current conditions
- Historical data back to 1979 (model reanalysis, not station obs)
- METAR-specific data: not directly available
- Pricing: Free tier available, paid plans for higher frequency

### c) Tomorrow.io (formerly ClimaCell)

- **URL**: https://www.tomorrow.io/
- Provides 1-minute precipitation nowcasting
- Temperature observations at sub-hourly resolution via proprietary sensor fusion
- METAR integration available in aviation-specific plans
- Pricing: Commercial

### d) Synoptic Data (formerly MesoWest)

- **URL**: https://synopticdata.com/
- Aggregates mesonet + ASOS + AWOS + RAWS + other networks
- API provides near-real-time observations with sub-hourly resolution
- Particularly strong for US aerodrome-adjacent stations
- Pricing: Free tier (limited), commercial plans

### e) Visual Crossing Weather

- **URL**: https://www.visualcrossing.com/
- Historical and real-time weather data including airport stations
- Hourly resolution (not sub-hourly for station observations)
- Pricing: Free tier, commercial plans

---

## 7. Summary Comparison

| Source | Resolution | Temp Precision | Coverage | Cost | Latency | Best For |
|---|---|---|---|---|---|---|
| **ASOS 1-min (NCEI/IEM)** | 1 min | 0.1 degF | USA (~900 sites) | Free | ~1-30 day archive lag | Historical HF temp analysis |
| **ASOS 5-min (NCEI)** | 5 min | 0.1 degF | USA (~900 sites) | Free | ~1-30 day archive lag | Slightly coarser analysis |
| **AWOS (ISD/Global Hourly)** | 20 min | 1 degC (METAR) | USA (~900 sites) | Free | Variable | Basic aerodrome monitoring |
| **AWC METAR API** | Hourly + SPECI | 1 degC (0.1 in remarks) | Worldwide | Free | Real-time | Global real-time METAR |
| **OGIMET** | Hourly + SPECI | 1 degC | Worldwide | Free | Near real-time | Historical METAR/SPECI |
| **DWD Open Data** | 10 min | 0.1 degC | Germany (~400 stn) | Free | Near real-time | European HF temp |
| **MADIS** | Sub-hourly (varies) | Varies | USA + partners | Free (registration) | Real-time | Multi-network fusion |
| **Nav Canada AWOS** | 10-20 min | 1 degC | Canada (~200 stn) | Free | Near real-time | Canadian aerodromes |
| **JMA AMeDAS** | 10 min | 0.1 degC | Japan (~1300 stn) | Free | Near real-time | Japan aerodromes |
| **Meteomatics API** | 1 min (modeled) | 0.1 degC | Worldwide | Commercial | Real-time | Commercial applications |
| **Synoptic Data** | Sub-hourly | Varies | USA primarily | Freemium | Real-time | US multi-network access |

---

## 8. Recommendations for PolyWeather

### For minute-by-minute temperature at US aerodromes:
1. **Primary**: ASOS 1-minute data via IEM API -- the only true 1-minute station-observed temperature data freely available
2. **Supplement**: AWC METAR API for real-time SPECI alerts and global coverage
3. **Enrichment**: MADIS for additional nearby mesonet stations

### For international aerodromes:
1. **Primary**: AWC METAR API (worldwide, real-time, SPECI included)
2. **Historical**: OGIMET for METAR/SPECI archives back to 2005
3. **High-resolution**: DWD 10-min data (Germany), JMA AMeDAS (Japan) for sub-hourly station obs

### Key API Endpoints

```
# IEM ASOS 1-minute (US) -- temperature for KJFK, last 24h, CSV
https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py?station=JFK&tz=UTC&year1=2026&month1=4&day1=16&hour1=0&minute1=0&year2=2026&month2=4&day2=17&hour2=0&minute2=0&vars=tmpf&vars=dwpf&sample=1min&what=download&delim=comma

# AWC METAR API (worldwide) -- latest METAR for KJFK as JSON
https://aviationweather.gov/api/data/metar?ids=KJFK&format=json

# AWC METAR cache (all current METARs worldwide, gzipped CSV, updated every minute)
https://aviationweather.gov/data/cache/metars.cache.csv.gz

# OGIMET METAR query (historical, any ICAO station)
https://ogimet.com/metars.phtml.en
# (web form: enter ICAO code, date range, select SA for METAR+SPECI)

# DWD 10-minute temperature (Germany)
https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/
```

### Data Pipeline Architecture

```
                    +------------------+
                    |  AWC METAR Cache  | (1-min update, worldwide)
                    +--------+---------+
                             |
 +------------------+        |        +------------------+
 | IEM ASOS 1-min   |        |        |  OGIMET Archive  |
 | (US aerodromes)  |        |        |  (worldwide METAR)|
 +--------+---------+  +-----+-----+  +--------+---------+
          |            |  Ingest &  |           |
          +----------->|  Normalize |<----------+
                       |  Pipeline  |
                       +-----+------+
                             |
                    +--------v---------+
                    |  Temperature DB   |
                    | (1-min US, hourly |
                    |  international)   |
                    +------------------+
```

---

## 9. Notes on Data Quality

- **ASOS 1-minute temperature** is the raw sensor reading before any human augmentation. It may differ slightly from the official METAR temperature due to rounding and QC.
- **METAR temperature remarks** (e.g., `T01560083`) provide 0.1 degC resolution, which is more precise than the integer body temperature group.
- **SPECI frequency** is weather-dependent. During stable conditions, observations may only come hourly; during rapidly changing conditions, SPECI can be issued every few minutes.
- **AWOS 20-minute data** does not include SPECI, making it strictly less useful than ASOS for high-frequency monitoring.
- **Commercial APIs** (Meteomatics, Tomorrow.io) offering "1-minute" data may use interpolation/model downscaling rather than actual 1-minute station observations. Verify whether the data is observed or modeled.

---

*Last updated: 2026-04-17*
