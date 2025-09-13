#!/usr/bin/env python3
"""
Fetch Solcast rooftop PV forecast once per day and print daily kWh totals.
- Uses SOLCAST_API_KEY and SOLCAST_SITE_ID from environment.
- Caches response under ~/.cache/solcast/forecast.json (per local day).
- Prints per-day energy (kWh), with a "remaining today" value from now onward.
- Shows BOTH mean (pv_estimate) and optimistic (pv_estimate90) totals.
- Prints optimistic as kWh and as a percent of battery capacity.
No external dependencies.

Tip for Windows: if IANA tzdata is missing, the script falls back to the
system local timezone (handles DST).
"""

from __future__ import annotations
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import os
import re
import sys
import urllib.request
import urllib.error

# ---- Timezone (robust on Windows) ----
def get_local_tz():
    try:
        return ZoneInfo("America/Chicago")
    except ZoneInfoNotFoundError:
        # Fall back to system local tz (e.g., "Central Daylight Time" on Windows)
        return datetime.now().astimezone().tzinfo

LOCAL_TZ = get_local_tz()

# ---- Config ----
CACHE_DIR = Path.home() / ".cache" / "solcast"
CACHE_FILE = CACHE_DIR / "forecast.json"
BASE_URL = os.environ.get("SOLCAST_BASE_URL", "https://api.solcast.com.au")
SITE_ID = os.environ.get("SOLCAST_SITE_ID", "").strip()
API_KEY = os.environ.get("SOLCAST_API_KEY", "").strip()
BATTERY_KWH = float(os.environ.get("SOLCAST_BATTERY_KWH", "15"))  # default 15 kWh

# ISO-8601 duration "PT#H#M#S" to hours (float)
_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

def duration_hours(iso: str) -> float:
    t = _DURATION_RE.match(iso)
    if not t:
        # Fallback: default to 0.5h if missing/unexpected
        return 0.5
    h = int(t.group(1) or 0)
    m = int(t.group(2) or 0)
    s = int(t.group(3) or 0)
    return h + m / 60.0 + s / 3600.0

def parse_period_end(s: str) -> datetime:
    # Example: "2025-08-10T04:00:00.0000000Z"
    # Normalize fractional seconds to max 6 digits and turn 'Z' into '+00:00'
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Ensure at most 6 fractional digits before timezone
    if "." in s:
        head, tail = s.split(".", 1)
        # find last + or - that indicates tz
        pos_plus = tail.rfind("+")
        pos_minus = tail.rfind("-")
        pos = max(pos_plus, pos_minus)
        if pos != -1:
            frac = tail[:pos]
            tz = tail[pos:]
            frac = (frac[:6]).ljust(6, "0")
            s = f"{head}.{frac}{tz}"
        else:
            frac = (tail[:6]).ljust(6, "0")
            s = f"{head}.{frac}+00:00"
    return datetime.fromisoformat(s)

def fetch_forecast() -> dict:
    if not SITE_ID or not API_KEY:
        raise SystemExit("SOLCAST_SITE_ID and SOLCAST_API_KEY must be set in the environment.")
    url = f"{BASE_URL}/rooftop_sites/{SITE_ID}/forecasts?format=json"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data

def load_or_refresh_cache(now_local: datetime) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    need_fetch = True
    if CACHE_FILE.exists():
        try:
            mtime_local = datetime.fromtimestamp(CACHE_FILE.stat().st_mtime, LOCAL_TZ)
            if mtime_local.date() == now_local.date():
                need_fetch = False
        except Exception:
            need_fetch = True
    if need_fetch:
        try:
            data = fetch_forecast()
            CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
        except urllib.error.HTTPError as e:
            # If fetch fails but we have a cache, fall back to it
            if CACHE_FILE.exists():
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            else:
                raise SystemExit(f"HTTP error {e.code}: {e.reason}")
        except Exception as e:
            if CACHE_FILE.exists():
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            else:
                raise
    else:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return data

def kwh_by_day(data: dict, now_local: datetime):
    """Return list of dicts with per-day kWh for mean and optimistic(90th pct).
    For 'today', only include intervals with period_end_local >= now_local.
    """
    forecasts = data.get("forecasts", [])
    if not forecasts:
        return []

    last_period = forecasts[0].get("period", "PT30M")
    sums = {}  # day -> {"mean": x, "opt": y}

    for row in forecasts:
        mean_kw = float(row.get("pv_estimate", 0.0))
        opt_kw = float(row.get("pv_estimate90", mean_kw))  # optimistic uses 90th percentile
        per = row.get("period", last_period) or last_period
        last_period = per
        hours = duration_hours(per)

        t_utc = parse_period_end(row["period_end"])  # aware UTC
        t_local = t_utc.astimezone(LOCAL_TZ)
        day_key = t_local.date().isoformat()

        # For today, only count intervals ending at/after now
        if t_local.date() == now_local.date() and t_local < now_local:
            continue

        entry = sums.setdefault(day_key, {"mean": 0.0, "opt": 0.0})
        entry["mean"] += mean_kw * hours
        entry["opt"] += opt_kw * hours

    rows = []
    for day in sorted(sums.keys()):
        rows.append({
            "day": day,
            "kwh_mean": sums[day]["mean"],
            "kwh_opt": sums[day]["opt"],
            "is_today": (day == now_local.date().isoformat()),
        })
    return rows

def main() -> int:
    now_local = datetime.now(LOCAL_TZ)

    try:
        data = load_or_refresh_cache(now_local)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    rows = kwh_by_day(data, now_local)
    if not rows:
        print("No forecast data found.")
        return 0

    # Print compact table with both mean and optimistic (90th pct) and battery %
    cap = BATTERY_KWH if BATTERY_KWH > 0 else 15.0
    print("Solcast PV energy forecast (kWh):")
    print("  Day          Mean    Optimistic   (% of {:.0f} kWh)".format(cap))
    for r in rows:
        pct = (r["kwh_opt"] / cap) * 100.0 if cap > 0 else 0.0
        suffix = " (remaining)" if r["is_today"] else ""
        day_dt = datetime.fromisoformat(r["day"])
        day_str = day_dt.strftime("%a %m-%d")
        print(f"  {day_str}:  {r['kwh_mean']:6.2f}    {r['kwh_opt']:6.2f}    ({pct:5.1f}%)"+suffix)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
