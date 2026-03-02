import os
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

import requests

API_KEY = os.getenv("TWC_API_KEY")

# Keep your list here, or pass it in from the bot if you want.
STATION_IDS = [
    "KNYNEWYO1796",
    "KNYNEWYO1115",
    "KNYNEWYO1967",
    "KNYNEWYO270",
    "KNYNEWYO1024",
    "KNYNEWYO1238",
    "KNYNEWYO2109",
]

DEFAULT_TZ_NAME = "America/New_York"
DEFAULT_AGREE_EPS_F = 0.3


class PWSBiasError(Exception):
    """Raised for configuration or hard failures (e.g., missing API key)."""


def _twc_get(url: str, params: dict, timeout_s: int = 12) -> requests.Response:
    # Centralize timeout + raise_for_status where appropriate.
    # We do NOT always raise_for_status because TWC can return 204 and you handle that.
    return requests.get(url, params=params, timeout=timeout_s)


def bias_last_4hours(tz_name: str = DEFAULT_TZ_NAME) -> dict:
    """
    Returns a dict mapping "HH:MM" -> tmpf (NWS/Mesonet reference) for the last 4 hours,
    anchored to :51 local time.
    """
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    re = requests.get(
        "https://mesonet.agron.iastate.edu/api/1/obhistory.json",
        params={
            "date": str(now.date()),
            "full": "1",
            "network": "NY_ASOS",
            "station": "NYC",
        },
        timeout=12,
    )
    re.raise_for_status()

    rows = re.json().get("data", [])

    # Build the last 4 target times at :51 (local)
    anchor = now.replace(minute=51, second=0, microsecond=0)
    if now.minute < 51:
        anchor -= timedelta(hours=1)

    targets = [anchor - timedelta(hours=i) for i in range(4)]  # newest -> older
    target_strings = {t.strftime("%Y-%m-%dT%H:%M") for t in targets}

    time_temp: dict[str, float] = {}
    for r in rows:
        lv = r.get("local_valid")  # e.g. "2026-01-14T19:51"
        tmpf = r.get("tmpf")
        if lv in target_strings and tmpf is not None:
            hhmm = datetime.fromisoformat(lv).strftime("%H:%M")
            time_temp[hhmm] = float(tmpf)

    # Return even if partial (bot can handle “missing”)
    return time_temp


def current_pws_temps(
    station_ids: list[str] | None = None,
    *,
    api_key: str | None = None,
) -> dict:
    """
    Returns:
      {
        "ok": True/False,
        "stations": {
            "<id>": {"ok": True, "temp_f": 32.1, "obs_time_local": "..."} OR {"ok": False, "status": 204, "error": "..."}
        }
      }
    """
    if station_ids is None:
        station_ids = STATION_IDS

    api_key = api_key or API_KEY
    if not api_key:
        raise PWSBiasError("Missing TWC_API_KEY environment variable.")

    out = {"ok": True, "stations": {}}

    for sid in station_ids:
        try:
            re = _twc_get(
                "https://api.weather.com/v2/pws/observations/current",
                params={
                    "stationId": sid,
                    "format": "json",
                    "units": "e",
                    "numericPrecision": "decimal",
                    "apiKey": api_key,
                },
            )

            if re.status_code == 204:
                out["ok"] = False
                out["stations"][sid] = {"ok": False, "status": 204, "error": "No current observation (204)."}
                continue

            if re.status_code != 200:
                out["ok"] = False
                out["stations"][sid] = {
                    "ok": False,
                    "status": re.status_code,
                    "error": re.text[:200],
                }
                continue

            data = re.json()
            obs0 = data["observations"][0]
            temp_f = obs0["imperial"]["temp"]
            out["stations"][sid] = {
                "ok": True,
                "temp_f": temp_f,
                "obs_time_local": obs0.get("obsTimeLocal"),
            }

        except Exception as e:
            out["ok"] = False
            out["stations"][sid] = {"ok": False, "status": None, "error": str(e)}

    return out


def pws_bias_compare_methods(
    time_temp: dict[str, float],
    *,
    station_ids: list[str] | None = None,
    tz_name: str = DEFAULT_TZ_NAME,
    max_diff_minutes: int = 12,
    agree_eps_f: float = DEFAULT_AGREE_EPS_F,
    min_points: int = 2,
    api_key: str | None = None,
) -> dict:
    """
    Bot-ready output. No prints.

    Returns:
      {
        "ok": True/False,
        "tz": "America/New_York",
        "reference_times": { "HH:MM": tmpf, ... },
        "stations": [
          {
            "station_id": "...",
            "ok": True/False,
            "status": 200/204/etc,
            "error": "...",

            "bias_by_time": { "HH:MM": bias, ... },
            "matched_points": int,

            "median_bias": float,
            "weighted_bias": float,

            "pws_now": float,
            "pred_nws_median": float,
            "pred_nws_weighted": float,

            "agree_diff": float,
            "agree": bool,
          },
          ...
        ]
      }
    """
    if station_ids is None:
        station_ids = STATION_IDS

    api_key = api_key or API_KEY
    if not api_key:
        raise PWSBiasError("Missing TWC_API_KEY environment variable.")

    tz = ZoneInfo(tz_name)

    # Sort "HH:MM" chronologically relative to today in that TZ
    today = datetime.now(tz).date()
    sorted_times = sorted(
        time_temp.keys(),
        key=lambda hhmm: datetime.combine(today, datetime.strptime(hhmm, "%H:%M").time()),
    )
    weights = list(range(1, len(sorted_times) + 1))  # 1..n (older -> newer)

    result = {
        "ok": True,
        "tz": tz_name,
        "reference_times": dict(time_temp),
        "stations": [],
    }

    for sid in station_ids:
        station_out = {
            "station_id": sid,
            "ok": False,
            "status": None,
            "error": None,
            "bias_by_time": {},
            "matched_points": 0,
            "median_bias": None,
            "weighted_bias": None,
            "pws_now": None,
            "pred_nws_median": None,
            "pred_nws_weighted": None,
            "agree_diff": None,
            "agree": None,
        }

        try:
            # 1) Get last-day observations
            re = _twc_get(
                "https://api.weather.com/v2/pws/observations/all/1day",
                params={
                    "stationId": sid,
                    "format": "json",
                    "units": "e",
                    "numericPrecision": "decimal",
                    "apiKey": api_key,
                },
            )

            station_out["status"] = re.status_code

            if re.status_code == 204:
                station_out["error"] = "No obs in last day / station offline (204)."
                result["ok"] = False
                result["stations"].append(station_out)
                continue

            if re.status_code != 200:
                station_out["error"] = re.text[:200]
                result["ok"] = False
                result["stations"].append(station_out)
                continue

            pws = re.json()
            obs = pws.get("observations", [])
            if not obs:
                station_out["error"] = "No observations array returned."
                result["ok"] = False
                result["stations"].append(station_out)
                continue

            # bucket observations by hour
            buckets: dict[str, list[tuple[datetime, dict]]] = {}
            latest_dt = None

            for o in obs:
                dt = datetime.strptime(o["obsTimeLocal"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
                hour_key = dt.strftime("%Y-%m-%d %H")
                buckets.setdefault(hour_key, []).append((dt, o))
                if latest_dt is None or dt > latest_dt:
                    latest_dt = dt

            if latest_dt is None:
                station_out["error"] = "Could not parse obsTimeLocal."
                result["ok"] = False
                result["stations"].append(station_out)
                continue

            # 2) Compute bias at each target HH:MM
            bias_by_time: dict[str, float] = {}

            for hhmm in sorted_times:
                ref_temp = time_temp.get(hhmm)
                if ref_temp is None:
                    continue

                hh, mm = map(int, hhmm.split(":"))
                target = latest_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
                hour_key = target.strftime("%Y-%m-%d %H")

                if hour_key not in buckets:
                    continue

                best_obs = None
                best_diff = None

                for dt, o in buckets[hour_key]:
                    diff_min = abs((dt - target).total_seconds()) / 60.0
                    if best_diff is None or diff_min < best_diff:
                        best_diff = diff_min
                        best_obs = o

                if best_obs is None or best_diff is None or best_diff > max_diff_minutes:
                    continue

                pws_temp = best_obs.get("imperial", {}).get("tempAvg")
                if pws_temp is None:
                    continue

                bias_by_time[hhmm] = float(pws_temp) - float(ref_temp)

            station_out["bias_by_time"] = bias_by_time

            biases_in_order = [bias_by_time[t] for t in sorted_times if t in bias_by_time]
            station_out["matched_points"] = len(biases_in_order)

            if len(biases_in_order) < min_points:
                station_out["error"] = f"Not enough matched points. Matched={len(biases_in_order)}."
                result["ok"] = False
                result["stations"].append(station_out)
                continue

            # 3) Method 1: median bias
            median_bias = float(median(biases_in_order))

            # 4) Method 2: recency-weighted bias
            used_biases = []
            used_weights = []
            for t, w in zip(sorted_times, weights):
                if t in bias_by_time:
                    used_biases.append(bias_by_time[t])
                    used_weights.append(w)

            weighted_bias = float(sum(b * w for b, w in zip(used_biases, used_weights)) / sum(used_weights))

            station_out["median_bias"] = median_bias
            station_out["weighted_bias"] = weighted_bias

            # 5) Current PWS temp
            re_cur = _twc_get(
                "https://api.weather.com/v2/pws/observations/current",
                params={
                    "stationId": sid,
                    "format": "json",
                    "units": "e",
                    "numericPrecision": "decimal",
                    "apiKey": api_key,
                },
            )

            if re_cur.status_code == 204:
                station_out["error"] = "Current PWS temp: 204 (no content)."
                result["ok"] = False
                result["stations"].append(station_out)
                continue

            if re_cur.status_code != 200:
                station_out["error"] = f"Current PWS temp unavailable: {re_cur.status_code} {re_cur.text[:120]}"
                result["ok"] = False
                result["stations"].append(station_out)
                continue

            cur_data = re_cur.json()
            pws_now = float(cur_data["observations"][0]["imperial"]["temp"])
            station_out["pws_now"] = pws_now

            # 6) Predict NWS using both methods
            pred_nws_median = pws_now - median_bias
            pred_nws_weighted = pws_now - weighted_bias

            station_out["pred_nws_median"] = float(pred_nws_median)
            station_out["pred_nws_weighted"] = float(pred_nws_weighted)

            agree_diff = abs(pred_nws_median - pred_nws_weighted)
            station_out["agree_diff"] = float(agree_diff)
            station_out["agree"] = bool(agree_diff <= agree_eps_f)

            station_out["ok"] = True
            result["stations"].append(station_out)

        except Exception as e:
            station_out["error"] = str(e)
            result["ok"] = False
            result["stations"].append(station_out)

    return result


def run_bias_snapshot() -> dict:
    """
    Convenience function for the bot:
      - pulls last 4 NYC(:51) reference temps
      - runs bias compare across all stations
      - returns one payload
    """
    time_temp = bias_last_4hours()
    return pws_bias_compare_methods(time_temp)
