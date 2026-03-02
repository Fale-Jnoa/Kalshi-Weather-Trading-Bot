import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API_KEY = os.getenv("TWC_API_KEY")

DEFAULT_STATIONS = [
    "KNYNEWYO1796",
    "KNYNEWYO1115",
    "KNYNEWYO1967",
    "KNYNEWYO270",
    "KNYNEWYO1024",
    "KNYNEWYO1238",
    "KNYNEWYO2109",
]

DEFAULT_TZ_NAME = "America/New_York"
DEFAULT_MATCH_TOL_MIN = 10  # handles 15-min cadence stations


class HourBackError(Exception):
    pass


def fetch_1day_obs(station_id: str, *, api_key: str, timeout_s: int = 12) -> list[dict] | None:
    re = requests.get(
        "https://api.weather.com/v2/pws/observations/all/1day",
        params={
            "stationId": station_id,
            "format": "json",
            "units": "e",
            "numericPrecision": "decimal",
            "apiKey": api_key,
        },
        timeout=timeout_s,
    )

    if re.status_code == 204:
        return None
    if re.status_code != 200:
        raise RuntimeError(f"{station_id} HTTP {re.status_code}: {(re.text or '')[:120]}")

    data = re.json()
    return data.get("observations", [])


def _parse_local_dt(obs: dict, tz: ZoneInfo) -> datetime:
    # "YYYY-MM-DD HH:MM:SS"
    return datetime.strptime(obs["obsTimeLocal"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)


def _closest_obs_to_target(observations: list[dict], target_dt: datetime, tz: ZoneInfo) -> tuple[dict | None, float | None]:
    best = None
    best_diff = None

    for o in observations:
        dt = _parse_local_dt(o, tz)
        diff = abs((dt - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best = o
            best_diff = diff

    return best, best_diff  # seconds


def hourly_report_for_station(
    station_id: str,
    *,
    tz_name: str = DEFAULT_TZ_NAME,
    match_tol_min: int = DEFAULT_MATCH_TOL_MIN,
    api_key: str | None = None,
) -> dict:
    """
    Returns a bot-friendly dict for one station.
    """
    api_key = api_key or API_KEY
    if not api_key:
        raise HourBackError("Missing TWC_API_KEY environment variable.")

    tz = ZoneInfo(tz_name)
    obs = fetch_1day_obs(station_id, api_key=api_key)

    if not obs:
        return {
            "station_id": station_id,
            "ok": False,
            "status": 204,
            "error": "No data (204).",
        }

    # sort by time just in case
    obs = sorted(obs, key=lambda o: o.get("epoch", 0))

    latest = obs[-1]
    latest_dt = _parse_local_dt(latest, tz)
    latest_temp = latest.get("imperial", {}).get("tempAvg")

    if latest_temp is None:
        return {
            "station_id": station_id,
            "ok": False,
            "status": 200,
            "error": "Latest tempAvg is None.",
        }

    target_dt = latest_dt - timedelta(hours=1)
    one_hr_obs, diff_sec = _closest_obs_to_target(obs, target_dt, tz)

    one_hr_ok = bool(one_hr_obs and diff_sec is not None and diff_sec <= match_tol_min * 60)

    one_hr_dt = None
    one_hr_temp = None
    one_hr_off_min = None
    delta_1h = None

    if one_hr_obs and diff_sec is not None:
        one_hr_off_min = diff_sec / 60.0

    if one_hr_ok:
        one_hr_dt = _parse_local_dt(one_hr_obs, tz)
        one_hr_temp = one_hr_obs.get("imperial", {}).get("tempAvg")
        if one_hr_temp is not None:
            delta_1h = float(latest_temp) - float(one_hr_temp)
        else:
            one_hr_ok = False

    # last hour window high
    window_start = latest_dt - timedelta(hours=1, minutes=1)
    window_obs: list[tuple[datetime, float]] = []
    for o in obs:
        dt = _parse_local_dt(o, tz)
        if window_start <= dt <= latest_dt:
            t = o.get("imperial", {}).get("tempAvg")
            if t is not None:
                window_obs.append((dt, float(t)))

    if window_obs:
        high_dt, high_temp = max(window_obs, key=lambda x: x[1])
    else:
        high_dt, high_temp = None, None

    return {
        "station_id": station_id,
        "ok": True,
        "status": 200,
        "now": {"temp_f": float(latest_temp), "dt_local": latest_dt},
        "one_hour_ago": {
            "ok": one_hr_ok,
            "temp_f": None if one_hr_temp is None else float(one_hr_temp),
            "dt_local": one_hr_dt,
            "off_by_minutes": one_hr_off_min,
            "tolerance_minutes": match_tol_min,
        },
        "delta_1h_f": delta_1h,
        "high_1h": {"temp_f": high_temp, "dt_local": high_dt},
    }


def hourly_report_all(
    station_ids: list[str] | None = None,
    *,
    tz_name: str = DEFAULT_TZ_NAME,
    match_tol_min: int = DEFAULT_MATCH_TOL_MIN,
    api_key: str | None = None,
) -> dict:
    """
    Returns:
      { "ok": bool, "tz": tz_name, "stations": [hourly_report_for_station(...), ...] }
    """
    station_ids = station_ids or DEFAULT_STATIONS
    out = {"ok": True, "tz": tz_name, "stations": []}

    for sid in station_ids:
        try:
            out["stations"].append(
                hourly_report_for_station(
                    sid, tz_name=tz_name, match_tol_min=match_tol_min, api_key=api_key
                )
            )
        except Exception as e:
            out["ok"] = False
            out["stations"].append(
                {"station_id": sid, "ok": False, "status": None, "error": str(e)}
            )

    out["ok"] = out["ok"] and all(s.get("ok") for s in out["stations"])
    return out
