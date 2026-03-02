# Kalshi Weather Trading Bot

An automated trading bot that executes trades on [Kalshi](https://kalshi.com) NYC temperature markets using a combination of private weather station data, NWS observations, and HRRR model forecasts.

---

## How It Works

The bot pulls from three data sources and combines them into a single blended temperature estimate, which is then used to determine whether to place a YES or NO trade on a given Kalshi market.

**Iowa Mesonet (KNYC/ASOS)** serves as an independent ground-truth anchor, pulling the most recent official NWS observation for Central Park. This is used to validate the other sources and penalize the confidence score if there's a large divergence.

**Private Weather Stations (PWS)** — seven personal weather stations around NYC are queried via The Weather Company API. Each station's historical bias against NWS reference temps is calculated and used to produce a bias-corrected temperature estimate. The 1-hour delta across all stations is also computed to determine the current temperature trend.

**HRRR Forecast Files** are produced by a separate script in the [HRRR Bilinear Implementation](https://github.com/Fale-Jnoa/hrrr-bilinear-implementation) repo. That script downloads HRRR model data, performs bilinear interpolation to Central Park coordinates, and writes forecast files to a shared `Forecasts/` directory that this bot reads from. The 2-hour delta from those forecasts is used to determine the model's predicted trend. Examples of the output can be seen in the Forecasts folder above.

### Trend Agreement Logic

The core signal is a comparison of the PWS trend and the HRRR trend based on **direction**, not magnitude. Both sources may report different absolute temperatures, but what matters is whether they agree on which way the temperature is moving.

- If both trends point the same direction → HRRR is weighted at 50% in the blended estimate
- If one source is flat → HRRR is weighted at 40%
- If trends point opposite directions → PWS is trusted and HRRR is down-weighted to 15%

A confidence score (0–1) is computed from the agreement result, data availability, and how closely the Mesonet anchor matches the blended estimate. Trades are only placed when confidence exceeds a configurable threshold.

---

## Project Structure

```
KalshiBot/
├── core/
│   ├── pws_bias.py           # Bias calculation and NWS temp prediction from PWS stations
│   ├── pws_current.py        # Current PWS observation fetcher
│   └── hourback_service.py   # 1-hour delta and high from PWS stations
├── trading/
│   ├── trading_algo.py       # Main trading pipeline
│   └── kalshi_wrapper.py     # Kalshi REST API wrapper
├── discord_bot/
│   └── bot.py                # Discord bot interface for PWS commands
├── Forecasts/                # HRRR forecast files written by external script
└── .env                      # API keys (never committed)
```

---

## Setup

**Install dependencies:**
```bash
pip install requests cryptography
```

**Set environment variables** (create a `.env` file or export directly):
```
KALSHI_API_KEY_ID=your_key_id
KALSHI_PRIVATE_KEY=/path/to/private_key.pem
TWC_API_KEY=your_weather_company_key
KALSHI_SERIES_TICKER=HIGHNY
KALSHI_FORECAST_DIR=C:\KalshiBot\Forecasts
KALSHI_DEMO=true
MAX_CONTRACTS=5
MAX_SPEND_USD=25.0
MIN_CONFIDENCE=0.60
MIN_EDGE_CENTS=5
```

**HRRR Forecasts** are generated separately by the [HRRR Bilinear Implementation](https://github.com/Fale-Jnoa/hrrr-bilinear-implementation) script, which must be run in its own conda environment. Make sure `KALSHI_FORECAST_DIR` points to the same `Forecasts/` folder that script writes to.

---

## Usage

```bash
# Dry run – computes signals and logs decision without placing any orders
python trading/trading_algo.py --dry-run

# Live run (uses demo mode by default unless KALSHI_DEMO=false)
python trading/trading_algo.py
```

---

## Risk Controls

| Parameter | Default | Description |
|---|---|---|
| `MAX_CONTRACTS` | 150 | Max contracts per trade |
| `MAX_SPEND_USD` | $25 | Max spend per trade |
| `MIN_CONFIDENCE` | 0.60 | Minimum confidence to place a trade |
| `MIN_EDGE_CENTS` | 5¢ | Minimum edge required vs market price |

Position size scales with confidence — higher confidence trades closer to the max contracts limit.

---

## Dependencies

- [Kalshi API v2](https://trading-api.kalshi.com/trade-api/v2) — prediction market trading
- [The Weather Company API](https://docs.google.com/document/d/1eKCnKXI9xnoMGRRzOL1xPCBihNV2rOet08qpE_gArAY) — private weather station data
- [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu) — NWS ASOS reference observations
- [HRRR Bilinear Implementation](https://github.com/Fale-Jnoa/hrrr-bilinear-implementation) — HRRR forecast generation (separate repo)
