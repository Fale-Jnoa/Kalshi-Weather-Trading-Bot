import requests, re, datetime, tempfile, os
from pathlib import Path

HEADERS   = {"User-Agent": "FloodCheck.com inoajohn12@gmail.com"}
METAR_URL = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/KNYC.TXT"
BASE_DIR  = Path(__file__).resolve().parent
CUR_PATH  = BASE_DIR / "CurrentTemp.txt"
LOG_PATH  = BASE_DIR / "TempLog.txt"

def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{ts}Z {msg}\n")

def parse_tgroup_c(metar: str):
    # TsnTTTsnTTT  e.g. T01171011  => +11.7°C, -1.1°C
    m = re.search(r'\bT(\d)(\d{3})(\d)(\d{3})\b', metar)
    if not m:
        return None
    s1, t1 = m.group(1), int(m.group(2))
    temp_c = (-1 if s1 == "1" else 1) * (t1 / 10.0)
    return temp_c

def parse_6hr_high_c(metar: str):
    # 1sTTT => 6-hour max in tenths °C with sign bit
    m = re.search(r'\b1(\d)(\d{3})\b', metar)
    if not m:
        return None
    sign = -1 if m.group(1) == "1" else 1
    return sign * (int(m.group(2)) / 10.0)

def fahrenheit(c):
    return c * 9/5 + 32

def atomic_write(path: Path, text: str):
    with tempfile.NamedTemporaryFile('w', delete=False, dir=path.parent, encoding='utf-8') as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, path)  # atomic on same filesystem

def read_recorded():
    try:
        with CUR_PATH.open("r", encoding="utf-8") as f:
            s = f.readline().strip()
            return float(s) if s else float("-inf")
    except FileNotFoundError:
        return float("-inf")
    except Exception as e:
        log(f"read_recorded error: {e!r}")
        return float("-inf")

def write_if_higher(temp_f: float):
    prev = read_recorded()
    if temp_f > prev:
        atomic_write(CUR_PATH, f"{round(temp_f):.2f}\n")
        log(f"Updated to {round(temp_f):.2f}F (prev {prev if prev!=-float('inf') else 'none'})")
    else:
        log(f"No update ({(temp_f):.2f}F <= {prev:.2f}F)")

def fetch_metar_text():
    r = requests.get(METAR_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text.strip()

def main():
    try:
        text = fetch_metar_text()
    except Exception as e:
        log(f"HTTP error: {e!r}")
        return

    lines = text.splitlines()
    if len(lines) < 2:
        log("Bad METAR format")
        return

    stamp, metar = lines[0].strip(), lines[1].strip()

    # Use data, not wall clock: prefer 6-hr high if present and higher.
    t_now_c = parse_tgroup_c(metar)
    if t_now_c is None:
        log(f"No T-group in: {metar}")
        return

    six_c = parse_6hr_high_c(metar)
    chosen_c = max(t_now_c, six_c) if six_c is not None else t_now_c

    write_if_higher(fahrenheit(chosen_c))

if __name__ == "__main__":
    main()
