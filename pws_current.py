import os
import requests

API_KEY = os.getenv("TWC_API_KEY")

STATION_IDS = [
    "KNYNEWYO1796",
    "KNYNEWYO1115",
    "KNYNEWYO1967",
    "KNYNEWYO270",
    "KNYNEWYO1024",
    "KNYNEWYO1238",
    "KNYNEWYO2109",
]

class PWSCurrentError(Exception):
    pass


def current_pws_data(station_ids=None, *, api_key: str | None = None, timeout_s: int = 12) -> dict:
    """
    Returns:
      {
        "ok": bool,
        "stations": {
          "<station_id>": {
             "ok": bool,
             "status": int|None,
             "temp_f": float|None,
             "obs_time_local": str|None,
             "error": str|None
          },
          ...
        }
      }
    """
    station_ids = station_ids or STATION_IDS
    api_key = api_key or API_KEY
    if not api_key:
        raise PWSCurrentError("Missing TWC_API_KEY environment variable.")

    out = {"ok": True, "stations": {}}

    for sid in station_ids:
        try:
            re = requests.get(
                "https://api.weather.com/v2/pws/observations/current",
                params={
                    "stationId": sid,
                    "format": "json",
                    "units": "e",
                    "numericPrecision": "decimal",
                    "apiKey": api_key,
                },
                timeout=timeout_s,
            )

            if re.status_code == 204:
                out["ok"] = False
                out["stations"][sid] = {
                    "ok": False,
                    "status": 204,
                    "temp_f": None,
                    "obs_time_local": None,
                    "error": "No current observation (204).",
                }
                continue

            if re.status_code != 200:
                out["ok"] = False
                out["stations"][sid] = {
                    "ok": False,
                    "status": re.status_code,
                    "temp_f": None,
                    "obs_time_local": None,
                    "error": (re.text or "")[:200],
                }
                continue

            data = re.json()
            obs0 = data["observations"][0]
            out["stations"][sid] = {
                "ok": True,
                "status": 200,
                "temp_f": float(obs0["imperial"]["temp"]),
                "obs_time_local": obs0.get("obsTimeLocal"),
                "error": None,
            }

        except Exception as e:
            out["ok"] = False
            out["stations"][sid] = {
                "ok": False,
                "status": None,
                "temp_f": None,
                "obs_time_local": None,
                "error": str(e),
            }

    return out
