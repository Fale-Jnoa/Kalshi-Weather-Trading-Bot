# FastKalshiBot_append.py
# Monitors Central Park (KNYC) via api.weather.gov and APPENDS every new highest temp.
# Uses ETag conditional requests + polite polling to avoid rate limits.

import requests, time, os
from datetime import datetime

NOTE_PATH   = r"C:\KalshiBot\daily_high.txt"
URL_LATEST  = "https://api.weather.gov/stations/KNYC/observations/latest"
URL_RECENT  = "https://api.weather.gov/stations/KNYC/observations?limit=6"
UA          = {"User-Agent": "FloodChecker.com (contact: inoajohn12@gmail.com)"}

def c_to_f(c):
    return None if c is None else (c * 9 / 5) + 32

def next_sleep_seconds():
    """Poll faster near the typical posting window; slower otherwise."""
    m = datetime.now().minute
    return 10 if (m >= 50 or m <= 10) else 30

# ---------- Note file helpers (append-only) ----------
def ensure_header():
    if not os.path.exists(NOTE_PATH):
        with open(NOTE_PATH, "w", encoding="utf-8") as f:
            f.write("Highest temperatures observed at KNYC (Central Park)\n")
            f.write("Format: <temp °F>  (observed YYYY-MM-DD HH:MM:SS TZ)\n")
            f.write("-----------------------------------------------------\n")

def read_last_high():
    """Return the last recorded high (float) from the note; -inf if none."""
    if not os.path.exists(NOTE_PATH):
        return float("-inf")
    try:
        last = float("-inf")
        with open(NOTE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(("Highest","Format","---")):
                    continue
                # First token is the number (e.g., '68.0')
                tok = line.split()[0]
                last = float(tok)
        return last
    except Exception:
        return float("-inf")

def append_high_line(highF, ts_utc):
    ts_local = datetime.fromisoformat(ts_utc.replace("Z", "+00:00")).astimezone()
    with open(NOTE_PATH, "a", encoding="utf-8") as f:
        f.write(f"{highF:.1f} °F  (observed {ts_local:%Y-%m-%d %H:%M:%S %Z})\n")
    print(f"[{datetime.now():%H:%M:%S}] NEW HIGH {highF:.1f}°F  ({ts_local:%H:%M:%S} local)")

# ---------- Main fast loop ----------
def run_fast_loop():
    ensure_header()
    session   = requests.Session()
    session.headers.update(UA)
    etag      = None
    seen_ts   = None
    # Persist across restarts by reading the last line in the note
    day_high  = read_last_high()

    while True:
        try:
            # Conditional GET with ETag
            hdrs = {"If-None-Match": etag} if etag else {}
            r = session.get(URL_LATEST, headers=hdrs, timeout=6)

            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "60"))
                print(f"Rate limited — backing off {wait}s.")
                time.sleep(wait)
                continue

            if r.status_code == 304:
                time.sleep(next_sleep_seconds())
                continue

            r.raise_for_status()
            etag = r.headers.get("ETag", etag)
            props = r.json()["properties"]

            ts = props["timestamp"]  # UTC ISO
            # Only act on a new observation timestamp
            if ts != seen_ts:
                seen_ts = ts
                # Fetch a few recent obs to catch any spike between polls
                r2 = session.get(URL_RECENT, timeout=6)
                r2.raise_for_status()
                for ob in r2.json().get("features", []):
                    valF = c_to_f(ob["properties"]["temperature"]["value"])
                    if valF is not None and valF > day_high:
                        day_high = valF
                        append_high_line(day_high, ob["properties"]["timestamp"])

        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] Error: {e}")
            time.sleep(60)

        time.sleep(next_sleep_seconds())

if __name__ == "__main__":
    print("Starting fast KNYC watcher (append mode)…")
    run_fast_loop()
