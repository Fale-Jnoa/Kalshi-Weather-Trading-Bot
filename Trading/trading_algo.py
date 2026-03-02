import os
import json
import math
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from statistics import median
from pathlib import Path

from kalshi_wrapper import (
    get_markets,
    get_orderbook,
    get_balance,
    place_order,
    KalshiError,
)
from Core.pws_bias import run_bias_snapshot
from Core.hourback_service import hourly_report_all

ET = ZoneInfo("America/New_York")

KALSHI_SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "HIGHNY")
FORECAST_DIR         = Path(os.getenv("KALSHI_FORECAST_DIR", r"C:\KalshiBot\Forecasts"))
MAX_CONTRACTS        = int(os.getenv("MAX_CONTRACTS", 150))
MAX_SPEND_USD        = float(os.getenv("MAX_SPEND_USD", 25.0))
MIN_CONFIDENCE       = float(os.getenv("MIN_CONFIDENCE", 0.60))
MIN_EDGE_CENTS       = int(os.getenv("MIN_EDGE_CENTS", 5))
DEMO_MODE            = os.getenv("KALSHI_DEMO", "true").lower() == "true"

FLAT_THRESHOLD_F = 0.5


def fetch_mesonet_knyc() -> float | None:
    now_et   = datetime.now(ET)
    date_str = now_et.strftime("%Y-%m-%d")
    try:
        r = requests.get(
            "https://mesonet.agron.iastate.edu/api/1/obhistory.json",
            params={"station": "NYC", "network": "NY_ASOS", "date": date_str, "full": "1"},
            timeout=12,
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows:
            return None
        for row in sorted(rows, key=lambda x: x.get("valid", ""), reverse=True):
            tmpf = row.get("tmpf")
            if tmpf is not None:
                return float(tmpf)
    except Exception as e:
        print(f"[Mesonet] fetch failed: {e}")
    return None


def load_latest_hrrr_forecast() -> list[tuple[datetime, float]]:
    files = sorted(FORECAST_DIR.glob("kalshi_forecast_*.txt"), reverse=True)
    if not files:
        print("[HRRR] No forecast files found in", FORECAST_DIR)
        return []
    entries = []
    try:
        for line in files[0].read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) < 4:
                continue
            dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            entries.append((dt, float(parts[3])))
    except Exception as e:
        print(f"[HRRR] Failed to parse {files[0].name}: {e}")
    return sorted(entries, key=lambda x: x[0])


def classify_trend(delta_f: float) -> str:
    if delta_f > FLAT_THRESHOLD_F:
        return "rising"
    elif delta_f < -FLAT_THRESHOLD_F:
        return "falling"
    return "flat"


def pws_trend(hourback_report: dict) -> tuple[float | None, str]:
    deltas = [
        s["delta_1h_f"]
        for s in hourback_report.get("stations", [])
        if s.get("ok") and s.get("delta_1h_f") is not None
    ]
    if not deltas:
        return None, "unknown"
    med = median(deltas)
    return med, classify_trend(med)


def hrrr_trend(forecast: list[tuple[datetime, float]]) -> tuple[float | None, str]:
    if len(forecast) < 2:
        return None, "unknown"
    now_utc = datetime.now(timezone.utc)
    nearest = lambda t: min(forecast, key=lambda x: abs((x[0] - t).total_seconds()))
    delta = nearest(now_utc + timedelta(hours=2))[1] - nearest(now_utc)[1]
    return delta, classify_trend(delta)


def assess_agreement(pws_dir: str, hrrr_dir: str) -> tuple[str, float]:
    if "unknown" in (pws_dir, hrrr_dir):
        return "insufficient_data", 0.3
    if pws_dir == hrrr_dir:
        return "agree", 0.5
    if "flat" in (pws_dir, hrrr_dir):
        return "neutral", 0.4
    return "disagree", 0.15


def blended_temp_estimate(
    mesonet_f: float | None,
    bias_report: dict,
    forecast: list[tuple[datetime, float]],
    hrrr_weight: float,
) -> float | None:
    sources, weights = [], []

    if mesonet_f is not None:
        sources.append(mesonet_f)
        weights.append(0.35)

    pws_preds = [
        s["pred_nws_weighted"]
        for s in bias_report.get("stations", [])
        if s.get("ok") and s.get("pred_nws_weighted") is not None
    ]
    if pws_preds:
        sources.append(median(pws_preds))
        weights.append(max(0.15, 0.65 - hrrr_weight))

    if forecast:
        now_utc = datetime.now(timezone.utc)
        nearest = min(forecast, key=lambda x: abs((x[0] - now_utc).total_seconds()))
        sources.append(nearest[1])
        weights.append(hrrr_weight)

    if not sources:
        return None

    total_w = sum(weights)
    return sum(s * w for s, w in zip(sources, weights)) / total_w


def compute_confidence(
    agreement: str,
    mesonet_available: bool,
    pws_ok_count: int,
    hrrr_available: bool,
    mesonet_f: float | None,
    blended_f: float | None,
) -> float:
    score = 0.0

    if agreement == "agree":
        score += 0.40
    elif agreement == "neutral":
        score += 0.25
    elif agreement == "disagree":
        score += 0.10

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

    if mesonet_f is not None and blended_f is not None:
        diff = abs(mesonet_f - blended_f)
        if diff <= 1.0:
            score += 0.05
        elif diff >= 5.0:
            score -= 0.15

    return max(0.0, min(1.0, score))


def find_target_market(series_ticker: str, blended_temp_f: float) -> dict | None:
    try:
        markets = get_markets(series_ticker, demo=DEMO_MODE)
    except Exception as e:
        print(f"[Kalshi] Could not fetch markets: {e}")
        return None

    if not markets:
        print("[Kalshi] No open markets found for series:", series_ticker)
        return None

    def strike_mid(m: dict) -> float:
        floor = m.get("floor_strike") or m.get("strike_value", 0)
        cap   = m.get("cap_strike") or floor
        return (float(floor) + float(cap)) / 2.0

    return min(markets, key=lambda m: abs(strike_mid(m) - blended_temp_f))


def decide_side_and_price(
    market: dict,
    blended_temp_f: float,
    confidence: float,
) -> tuple[str, int] | tuple[None, None]:
    floor = float(market.get("floor_strike") or market.get("strike_value", 0))
    cap   = float(market.get("cap_strike") or floor + 2.0)
    mid   = (floor + cap) / 2.0
    sigma = max((cap - floor) / 2.0, 1.0)

    base_prob = math.exp(-0.5 * ((blended_temp_f - mid) / sigma) ** 2)
    est_prob  = base_prob * confidence + (1 - confidence) * 0.5

    if est_prob >= 0.55:
        side = "yes"
        try:
            ob = get_orderbook(market["ticker"], depth=3, demo=DEMO_MODE)
            best_ask = ob.get("yes", [[]])[0][0] if ob.get("yes") else None
            limit_price = best_ask if (best_ask and best_ask <= int(est_prob * 100) - MIN_EDGE_CENTS) else max(1, int(est_prob * 100) - MIN_EDGE_CENTS)
        except Exception:
            limit_price = max(1, int(est_prob * 100) - MIN_EDGE_CENTS)

    elif est_prob <= 0.45:
        side = "no"
        no_prob = 1 - est_prob
        try:
            ob = get_orderbook(market["ticker"], depth=3, demo=DEMO_MODE)
            best_ask_no = ob.get("no", [[]])[0][0] if ob.get("no") else None
            limit_price = best_ask_no if (best_ask_no and best_ask_no <= int(no_prob * 100) - MIN_EDGE_CENTS) else max(1, int(no_prob * 100) - MIN_EDGE_CENTS)
        except Exception:
            limit_price = max(1, int(no_prob * 100) - MIN_EDGE_CENTS)

    else:
        return None, None

    return side, max(1, min(99, limit_price))


def size_position(confidence: float, limit_price_cents: int, balance_usd: float) -> int:
    conf_range    = 1.0 - MIN_CONFIDENCE
    frac          = max(0.0, confidence - MIN_CONFIDENCE) / conf_range if conf_range > 0 else 0
    raw_count     = max(1, round(frac * MAX_CONTRACTS))
    cost_per      = limit_price_cents / 100.0
    affordable    = int(min(MAX_SPEND_USD, balance_usd * 0.1) / cost_per) if cost_per > 0 else 1
    return max(1, min(raw_count, MAX_CONTRACTS, affordable))


def run_trading_cycle(dry_run: bool = False) -> dict:
    now_et  = datetime.now(ET)
    summary = {
        "timestamp_et": now_et.strftime("%Y-%m-%d %H:%M ET"),
        "dry_run":   dry_run,
        "demo_mode": DEMO_MODE,
        "signals":   {},
        "decision":  {},
        "order":     None,
        "error":     None,
    }

    try:
        mesonet_f = fetch_mesonet_knyc()
        summary["signals"]["mesonet_f"] = mesonet_f

        bias_report  = run_bias_snapshot()
        pws_ok_count = sum(1 for s in bias_report.get("stations", []) if s.get("ok"))
        pws_preds    = [s["pred_nws_weighted"] for s in bias_report.get("stations", []) if s.get("ok") and s.get("pred_nws_weighted") is not None]
        pws_estimate = median(pws_preds) if pws_preds else None
        summary["signals"]["pws_estimate_f"]   = pws_estimate
        summary["signals"]["pws_ok_stations"]  = pws_ok_count

        hourback                             = hourly_report_all()
        pws_delta, pws_direction             = pws_trend(hourback)
        summary["signals"]["pws_delta_1h_f"] = pws_delta
        summary["signals"]["pws_trend"]      = pws_direction

        forecast                              = load_latest_hrrr_forecast()
        hrrr_delta, hrrr_direction            = hrrr_trend(forecast)
        hrrr_available                        = len(forecast) > 0
        summary["signals"]["hrrr_delta_2h_f"] = hrrr_delta
        summary["signals"]["hrrr_trend"]      = hrrr_direction
        summary["signals"]["hrrr_available"]  = hrrr_available

        agreement, hrrr_weight               = assess_agreement(pws_direction, hrrr_direction)
        blended_f                            = blended_temp_estimate(mesonet_f, bias_report, forecast, hrrr_weight)
        summary["signals"]["agreement"]           = agreement
        summary["signals"]["hrrr_weight_used"]    = hrrr_weight
        summary["signals"]["blended_estimate_f"]  = blended_f

        confidence = compute_confidence(
            agreement=agreement,
            mesonet_available=mesonet_f is not None,
            pws_ok_count=pws_ok_count,
            hrrr_available=hrrr_available,
            mesonet_f=mesonet_f,
            blended_f=blended_f,
        )
        summary["signals"]["confidence"] = round(confidence, 3)

        if confidence < MIN_CONFIDENCE:
            summary["decision"] = {"action": "skip", "reason": f"Confidence {confidence:.2f} below threshold {MIN_CONFIDENCE}"}
            return summary

        if blended_f is None:
            summary["decision"] = {"action": "skip", "reason": "No blended temperature estimate."}
            return summary

        market = find_target_market(KALSHI_SERIES_TICKER, blended_f)
        if market is None:
            summary["decision"] = {"action": "skip", "reason": "No suitable market found."}
            return summary

        side, limit_price = decide_side_and_price(market, blended_f, confidence)
        if side is None:
            summary["decision"] = {"action": "skip", "reason": "No edge detected."}
            return summary

        balance   = get_balance(demo=DEMO_MODE)
        contracts = size_position(confidence, limit_price, balance)

        summary["decision"] = {
            "action":              "trade",
            "market_ticker":       market["ticker"],
            "side":                side,
            "contracts":           contracts,
            "limit_price_cents":   limit_price,
            "estimated_cost_usd":  round(contracts * limit_price / 100, 2),
        }

        if dry_run:
            summary["order"] = "DRY RUN – order not placed."
        else:
            summary["order"] = place_order(
                ticker=market["ticker"],
                side=side,
                count=contracts,
                limit_price=limit_price,
                demo=DEMO_MODE,
            )

    except Exception as e:
        summary["error"] = str(e)
        print(f"[ERROR] {e}")

    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi Weather Trading Bot")
    parser.add_argument("--dry-run", action="store_true")
    args   = parser.parse_args()
    result = run_trading_cycle(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
