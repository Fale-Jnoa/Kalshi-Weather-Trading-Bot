"""
trading_algo.py
---------------
Kalshi Weather Trading Algorithm – NYC Temperature Markets

Signal pipeline:
  1. Iowa Mesonet (KNYC/ASOS) → ground-truth current NWS temp
  2. PWS bias module          → bias-corrected current temp from 7 private stations
  3. HRRR forecast file       → interpolated Central Park temp trend (next 1-6 hrs)
  4. Trend agreement logic    → compare PWS trend vs HRRR trend (direction, not magnitude)
  5. Confidence scoring       → combine all signals into a 0-1 confidence score
  6. Kalshi order logic       → decide YES/NO, size, and limit price

Key design decision:
  - Agreement is based on TREND DIRECTION (both rising, both falling, or flat).
  - When trends disagree (opposite directions), PWS is trusted and HRRR is down-weighted.
  - Mesonet KNYC is used as an independent anchor to validate both sources.
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from statistics import median, mean
from pathlib import Path

from kalshi_wrapper import (
    get_market,
    get_orderbook,
    get_balance,
    place_order,
    KalshiError,
)
from Core.pws_bias import run_bias_snapshot
from Core.hourback_service import hourly_report_all

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")

# Kalshi series ticker for NYC temperature markets (adjust to actual series)
# e.g. "HIGHNY" for daily high, "KNYC" for hourly – check Kalshi's market list
KALSHI_SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "HIGHNY")

# Folder where grib.py writes kalshi_forecast_*.txt files
FORECAST_DIR = Path(os.getenv("KALSHI_FORECAST_DIR", r"C:\KalshiBot\Forecasts"))

# Risk controls
MAX_CONTRACTS    = int(os.getenv("MAX_CONTRACTS", 5))      # per trade
MAX_SPEND_USD    = float(os.getenv("MAX_SPEND_USD", 25.0)) # per trade
MIN_CONFIDENCE   = float(os.getenv("MIN_CONFIDENCE", 0.60)) # skip trade below this
MIN_EDGE_CENTS   = int(os.getenv("MIN_EDGE_CENTS", 5))     # min edge vs market price

DEMO_MODE        = os.getenv("KALSHI_DEMO", "true").lower() == "true"


# ---------------------------------------------------------------------------
# 1. Iowa Mesonet – live KNYC anchor temp
# ---------------------------------------------------------------------------

def fetch_mesonet_knyc() -> float | None:
    """
    Pull the most recent KNYC (Central Park NWS) observation from Iowa Mesonet.
    Returns temperature in °F, or None on failure.
    """
    now_et = datetime.now(ET)
    date_str = now_et.strftime("%Y-%m-%d")

    try:
        r = requests.get(
            "https://mesonet.agron.iastate.edu/api/1/obhistory.json",
            params={
                "station":  "NYC",
                "network":  "NY_ASOS",
                "date":     date_str,
                "full":     "1",
            },
            timeout=12,
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows:
            return None

        # Sort by valid time descending, take most recent with a real tmpf
        rows_sorted = sorted(rows, key=lambda x: x.get("valid", ""), reverse=True)
        for row in rows_sorted:
            tmpf = row.get("tmpf")
            if tmpf is not None:
                return float(tmpf)

    except Exception as e:
        print(f"[Mesonet] fetch failed: {e}")

    return None


# ---------------------------------------------------------------------------
# 2. Load HRRR forecast – most recent file in FORECAST_DIR
# ---------------------------------------------------------------------------

def load_latest_hrrr_forecast() -> list[tuple[datetime, float]]:
    """
    Reads the most recent kalshi_forecast_*.txt written by grib.py.
    Each line: "YYYY-MM-DD HH:MM UTC <temp> °F"
    Returns list of (valid_time_utc, temp_f) sorted chronologically.
    """
    files = sorted(FORECAST_DIR.glob("kalshi_forecast_*.txt"), reverse=True)
    if not files:
        print("[HRRR] No forecast files found in", FORECAST_DIR)
        return []

    latest = files[0]
    entries = []
    try:
        for line in latest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            # Format: "2026-03-02 18:00 UTC 41.23 °F"
            parts = line.split()
            if len(parts) < 4:
                continue
            dt_str = f"{parts[0]} {parts[1]}"
            temp_f = float(parts[3])
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            entries.append((dt, temp_f))
    except Exception as e:
        print(f"[HRRR] Failed to parse {latest.name}: {e}")

    return sorted(entries, key=lambda x: x[0])


# ---------------------------------------------------------------------------
# 3. Trend analysis – direction-based agreement
# ---------------------------------------------------------------------------

FLAT_THRESHOLD_F = 0.5  # °F change smaller than this is considered "flat"


def classify_trend(delta_f: float) -> str:
    """Return 'rising', 'falling', or 'flat' based on a temperature delta."""
    if delta_f > FLAT_THRESHOLD_F:
        return "rising"
    elif delta_f < -FLAT_THRESHOLD_F:
        return "falling"
    else:
        return "flat"


def pws_trend(hourback_report: dict) -> tuple[float | None, str]:
    """
    Compute median delta_1h across all healthy PWS stations.
    Returns (median_delta_f, trend_direction).
    """
    deltas = []
    for s in hourback_report.get("stations", []):
        if s.get("ok") and s.get("delta_1h_f") is not None:
            deltas.append(s["delta_1h_f"])

    if not deltas:
        return None, "unknown"

    med_delta = median(deltas)
    return med_delta, classify_trend(med_delta)


def hrrr_trend(forecast: list[tuple[datetime, float]]) -> tuple[float | None, str]:
    """
    Compute expected temperature change over next 2 hours from HRRR forecast.
    Returns (delta_f, trend_direction).
    """
    if len(forecast) < 2:
        return None, "unknown"

    now_utc = datetime.now(timezone.utc)

    # Find nearest forecast hour to now and 2 hours ahead
    def nearest(target_utc):
        return min(forecast, key=lambda x: abs((x[0] - target_utc).total_seconds()))

    t_now  = nearest(now_utc)
    t_plus2 = nearest(now_utc + timedelta(hours=2))

    delta = t_plus2[1] - t_now[1]
    return delta, classify_trend(delta)


def assess_agreement(pws_direction: str, hrrr_direction: str) -> tuple[str, float]:
    """
    Compare trend directions and return (agreement_label, hrrr_weight).

    Rules:
      - Same direction (both rising/falling)  → "agree",   hrrr_weight=0.5
      - One or both flat                       → "neutral", hrrr_weight=0.4
      - Opposite directions                    → "disagree", hrrr_weight=0.15
        (PWS trusted more – HRRR heavily down-weighted)
    """
    if pws_direction == "unknown" or hrrr_direction == "unknown":
        return "insufficient_data", 0.3

    if pws_direction == hrrr_direction:
        return "agree", 0.5

    if "flat" in (pws_direction, hrrr_direction):
        return "neutral", 0.4

    # Opposite directions
    return "disagree", 0.15


# ---------------------------------------------------------------------------
# 4. Blended temperature estimate
# ---------------------------------------------------------------------------

def blended_temp_estimate(
    mesonet_f: float | None,
    bias_report: dict,
    forecast: list[tuple[datetime, float]],
    hrrr_weight: float,
) -> float | None:
    """
    Combine Mesonet anchor, bias-corrected PWS, and HRRR into one temperature estimate.

    Weights (approximate, normalized to 1.0):
      - Mesonet KNYC:   0.35  (independent ground truth)
      - PWS (bias-adj): 0.65 - hrrr_weight   (live private stations)
      - HRRR nearest:   hrrr_weight           (model forecast)
    """
    sources = []
    weights = []

    # Mesonet anchor
    if mesonet_f is not None:
        sources.append(mesonet_f)
        weights.append(0.35)

    # PWS bias-corrected (use weighted_bias predictions, median across stations)
    pws_preds = []
    for s in bias_report.get("stations", []):
        if s.get("ok") and s.get("pred_nws_weighted") is not None:
            pws_preds.append(s["pred_nws_weighted"])

    if pws_preds:
        pws_estimate = median(pws_preds)
        pws_weight = max(0.15, 0.65 - hrrr_weight)
        sources.append(pws_estimate)
        weights.append(pws_weight)

    # HRRR nearest valid time
    if forecast:
        now_utc = datetime.now(timezone.utc)
        nearest_hrrr = min(forecast, key=lambda x: abs((x[0] - now_utc).total_seconds()))
        sources.append(nearest_hrrr[1])
        weights.append(hrrr_weight)

    if not sources:
        return None

    total_w = sum(weights)
    return sum(s * w for s, w in zip(sources, weights)) / total_w


# ---------------------------------------------------------------------------
# 5. Confidence scoring
# ---------------------------------------------------------------------------

def compute_confidence(
    agreement: str,
    mesonet_available: bool,
    pws_ok_count: int,
    hrrr_available: bool,
    mesonet_f: float | None,
    blended_f: float | None,
) -> float:
    """
    Score 0.0 – 1.0 reflecting how much we trust our temperature estimate.
    """
    score = 0.0

    # Agreement bonus
    if agreement == "agree":
        score += 0.40
    elif agreement == "neutral":
        score += 0.25
    elif agreement == "disagree":
        score += 0.10   # we still trade but with lower confidence / smaller size
    else:
        score += 0.0

    # Data source availability
    if mesonet_available:
        score += 0.20
    if pws_ok_count >= 5:
        score += 0.25
    elif pws_ok_count >= 3:
        score += 0.15
    elif pws_ok_count >= 1:
        score += 0.05
    if hrrr_available:
        score += 0.10

    # Mesonet vs blended sanity check
    if mesonet_f is not None and blended_f is not None:
        diff = abs(mesonet_f - blended_f)
        if diff <= 1.0:
            score += 0.05  # very close → bonus
        elif diff >= 5.0:
            score -= 0.15  # large divergence → penalize

    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# 6. Kalshi market targeting
# ---------------------------------------------------------------------------

def find_target_market(series_ticker: str, blended_temp_f: float) -> dict | None:
    """
    Among open markets for the series, find the one whose strike is closest
    to our blended temperature estimate.

    Returns the market dict or None.
    """
    try:
        from kalshi_wrapper import get_markets
        markets = get_markets(series_ticker, demo=DEMO_MODE)
    except Exception as e:
        print(f"[Kalshi] Could not fetch markets: {e}")
        return None

    if not markets:
        print("[Kalshi] No open markets found for series:", series_ticker)
        return None

    # Each market has a 'floor_strike' and 'cap_strike' or a single 'strike_value'
    # We target the market whose midpoint is closest to our estimate
    def strike_mid(m: dict) -> float:
        floor = m.get("floor_strike") or m.get("strike_value", 0)
        cap   = m.get("cap_strike") or floor
        return (float(floor) + float(cap)) / 2.0

    best = min(markets, key=lambda m: abs(strike_mid(m) - blended_temp_f))
    return best


def decide_side_and_price(
    market: dict,
    blended_temp_f: float,
    confidence: float,
) -> tuple[str, int] | tuple[None, None]:
    """
    Decide YES or NO and compute a limit price in cents (1–99).

    Logic:
      - Estimate probability that temp will land in this market's range.
      - If blended_temp is well inside the range → YES
      - If blended_temp is well outside the range → NO
      - Adjust limit price based on confidence (more confident = willing to pay more for YES,
        or accept less for NO).

    Returns (side, limit_price_cents) or (None, None) to skip.
    """
    floor = float(market.get("floor_strike") or market.get("strike_value", 0))
    cap   = float(market.get("cap_strike") or floor + 2.0)  # default 2°F wide

    mid   = (floor + cap) / 2.0
    width = cap - floor

    # Distance of our estimate from the center of the range (in °F)
    dist_from_center = blended_temp_f - mid

    # Base probability: Gaussian-like, sigma ≈ half the range
    import math
    sigma = max(width / 2.0, 1.0)
    base_prob = math.exp(-0.5 * (dist_from_center / sigma) ** 2)

    # Scale by confidence
    est_prob = base_prob * confidence + (1 - confidence) * 0.5  # pull toward 50 when uncertain

    # Decide side
    if est_prob >= 0.55:
        side = "yes"
        # Willing to pay up to est_prob * 100 cents, but leave MIN_EDGE_CENTS of edge
        try:
            ob = get_orderbook(market["ticker"], depth=3, demo=DEMO_MODE)
            best_ask = ob.get("yes", [[]])[0][0] if ob.get("yes") else None
            if best_ask and best_ask <= int(est_prob * 100) - MIN_EDGE_CENTS:
                limit_price = best_ask
            else:
                limit_price = max(1, int(est_prob * 100) - MIN_EDGE_CENTS)
        except Exception:
            limit_price = max(1, int(est_prob * 100) - MIN_EDGE_CENTS)

    elif est_prob <= 0.45:
        side = "no"
        no_prob = 1 - est_prob
        try:
            ob = get_orderbook(market["ticker"], depth=3, demo=DEMO_MODE)
            best_ask_no = ob.get("no", [[]])[0][0] if ob.get("no") else None
            if best_ask_no and best_ask_no <= int(no_prob * 100) - MIN_EDGE_CENTS:
                limit_price = best_ask_no
            else:
                limit_price = max(1, int(no_prob * 100) - MIN_EDGE_CENTS)
        except Exception:
            limit_price = max(1, int(no_prob * 100) - MIN_EDGE_CENTS)

    else:
        # Too uncertain – skip
        return None, None

    limit_price = max(1, min(99, limit_price))
    return side, limit_price


def size_position(confidence: float, limit_price_cents: int, balance_usd: float) -> int:
    """
    Determine number of contracts to trade.
    Scales up with confidence, capped by MAX_CONTRACTS and MAX_SPEND_USD.
    """
    # Fraction of MAX_CONTRACTS driven by confidence above MIN_CONFIDENCE
    conf_range = 1.0 - MIN_CONFIDENCE
    conf_above_min = max(0.0, confidence - MIN_CONFIDENCE)
    frac = conf_above_min / conf_range if conf_range > 0 else 0

    raw_count = max(1, round(frac * MAX_CONTRACTS))

    # Budget cap
    cost_per_contract = limit_price_cents / 100.0  # in USD
    affordable = int(min(MAX_SPEND_USD, balance_usd * 0.1) / cost_per_contract) if cost_per_contract > 0 else 1

    return max(1, min(raw_count, MAX_CONTRACTS, affordable))


# ---------------------------------------------------------------------------
# 7. Main entry point
# ---------------------------------------------------------------------------

def run_trading_cycle(dry_run: bool = False) -> dict:
    """
    Full trading cycle. Returns a summary dict suitable for Discord bot output.
    Set dry_run=True to compute signals without placing orders.
    """
    now_et = datetime.now(ET)
    summary = {
        "timestamp_et": now_et.strftime("%Y-%m-%d %H:%M ET"),
        "dry_run": dry_run,
        "demo_mode": DEMO_MODE,
        "signals": {},
        "decision": {},
        "order": None,
        "error": None,
    }

    try:
        # --- Step 1: Mesonet anchor ---
        print("[1/7] Fetching Iowa Mesonet KNYC temperature...")
        mesonet_f = fetch_mesonet_knyc()
        summary["signals"]["mesonet_f"] = mesonet_f
        print(f"      Mesonet KNYC: {mesonet_f} °F")

        # --- Step 2: PWS bias-corrected estimate ---
        print("[2/7] Running PWS bias snapshot...")
        bias_report = run_bias_snapshot()
        pws_ok_count = sum(1 for s in bias_report.get("stations", []) if s.get("ok"))
        pws_preds = [s["pred_nws_weighted"] for s in bias_report.get("stations", []) if s.get("ok") and s.get("pred_nws_weighted") is not None]
        pws_estimate = median(pws_preds) if pws_preds else None
        summary["signals"]["pws_estimate_f"] = pws_estimate
        summary["signals"]["pws_ok_stations"] = pws_ok_count
        print(f"      PWS estimate: {pws_estimate} °F ({pws_ok_count} stations OK)")

        # --- Step 3: PWS trend (1-hour delta) ---
        print("[3/7] Fetching PWS hourly trends...")
        hourback = hourly_report_all()
        pws_delta, pws_direction = pws_trend(hourback)
        summary["signals"]["pws_delta_1h_f"] = pws_delta
        summary["signals"]["pws_trend"] = pws_direction
        print(f"      PWS 1h delta: {pws_delta} °F → trend: {pws_direction}")

        # --- Step 4: HRRR forecast trend ---
        print("[4/7] Loading HRRR forecast...")
        forecast = load_latest_hrrr_forecast()
        hrrr_delta, hrrr_direction = hrrr_trend(forecast)
        hrrr_available = len(forecast) > 0
        summary["signals"]["hrrr_delta_2h_f"] = hrrr_delta
        summary["signals"]["hrrr_trend"] = hrrr_direction
        summary["signals"]["hrrr_available"] = hrrr_available
        print(f"      HRRR 2h delta: {hrrr_delta} °F → trend: {hrrr_direction}")

        # --- Step 5: Agreement & blended estimate ---
        print("[5/7] Assessing trend agreement...")
        agreement, hrrr_weight = assess_agreement(pws_direction, hrrr_direction)
        blended_f = blended_temp_estimate(mesonet_f, bias_report, forecast, hrrr_weight)
        summary["signals"]["agreement"] = agreement
        summary["signals"]["hrrr_weight_used"] = hrrr_weight
        summary["signals"]["blended_estimate_f"] = blended_f
        print(f"      Agreement: {agreement} | HRRR weight: {hrrr_weight} | Blended: {blended_f} °F")

        # --- Step 6: Confidence ---
        print("[6/7] Computing confidence score...")
        confidence = compute_confidence(
            agreement=agreement,
            mesonet_available=mesonet_f is not None,
            pws_ok_count=pws_ok_count,
            hrrr_available=hrrr_available,
            mesonet_f=mesonet_f,
            blended_f=blended_f,
        )
        summary["signals"]["confidence"] = round(confidence, 3)
        print(f"      Confidence: {confidence:.3f} (min required: {MIN_CONFIDENCE})")

        if confidence < MIN_CONFIDENCE:
            summary["decision"] = {
                "action": "skip",
                "reason": f"Confidence {confidence:.2f} below threshold {MIN_CONFIDENCE}",
            }
            print(f"      ⛔ Skipping trade – confidence too low.")
            return summary

        if blended_f is None:
            summary["decision"] = {"action": "skip", "reason": "No blended temperature estimate."}
            return summary

        # --- Step 7: Market targeting & order ---
        print("[7/7] Finding Kalshi market and placing order...")
        market = find_target_market(KALSHI_SERIES_TICKER, blended_f)
        if market is None:
            summary["decision"] = {"action": "skip", "reason": "No suitable market found."}
            return summary

        side, limit_price = decide_side_and_price(market, blended_f, confidence)
        if side is None:
            summary["decision"] = {
                "action": "skip",
                "reason": "Probability estimate too close to 50% – no edge.",
            }
            print("      ⛔ Skipping – no edge detected.")
            return summary

        balance = get_balance(demo=DEMO_MODE)
        contracts = size_position(confidence, limit_price, balance)

        summary["decision"] = {
            "action": "trade",
            "market_ticker": market["ticker"],
            "side": side,
            "contracts": contracts,
            "limit_price_cents": limit_price,
            "estimated_cost_usd": round(contracts * limit_price / 100, 2),
        }

        print(f"      ✅ Decision: {side.upper()} {contracts}x {market['ticker']} @ {limit_price}¢")
        print(f"         Estimated cost: ${contracts * limit_price / 100:.2f}")

        if dry_run:
            summary["order"] = "DRY RUN – order not placed."
            print("      DRY RUN – no order placed.")
        else:
            order_resp = place_order(
                ticker=market["ticker"],
                side=side,
                count=contracts,
                limit_price=limit_price,
                demo=DEMO_MODE,
            )
            summary["order"] = order_resp
            print(f"      Order placed: {order_resp}")

    except Exception as e:
        summary["error"] = str(e)
        print(f"[ERROR] {e}")

    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Kalshi Weather Trading Bot")
    parser.add_argument("--dry-run", action="store_true", help="Compute signals without placing orders")
    args = parser.parse_args()

    result = run_trading_cycle(dry_run=args.dry_run)
    print("\n--- SUMMARY ---")
    print(json.dumps(result, indent=2, default=str))
