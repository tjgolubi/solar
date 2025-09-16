#!/usr/bin/env python3

"""
Fetch Solcast rooftop PV forecast once per day and print daily kWh totals.
- Uses SOLCAST_API_KEY and SOLCAST_SITE_ID from environment.
- Caches response under ~/.cache/solcast/forecast.json (per local day).
- Prints per-day energy (kWh), with a "remaining today" value from now onward.
- Shows BOTH mean (pv_estimate) and optimistic (pv_estimate90) totals.
- Prints optimistic as kWh and as a percent of battery capacity.

Behavioral details:
- If cache is older than 3 days, it is deleted and never used.
- On transient DNS failures, performs **three** short back-off retries while
  printing a single line to stderr like:
  "DNS lookup failed, retrying retrying retrying" (then a newline).
- If fetch fails and a same-day cache exists, it falls back with a warning.
- If fetch fails and no usable cache exists, prints an error and exits 1.

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
import time
import socket
import urllib.request
import urllib.error

# ---- Timezone (robust on Windows) ----
def get_local_tz():
    try:
        return ZoneInfo("America/Chicago")
    except ZoneInfoNotFoundError:
        # Fall back to system local tz (e.g., "Central Daylight Time" on Win)
        return datetime.now().astimezone().tzinfo


LOCAL_TZ = get_local_tz()

# ---- Config ----
CACHE_DIR = Path.home() / ".cache" / "solcast"
CACHE_FILE = CACHE_DIR / "forecast.json"
BASE_URL = os.environ.get("SOLCAST_BASE_URL", "https://api.solcast.com.au")
SITE_ID = os.environ.get("SOLCAST_SITE_ID", "").strip()
API_KEY = os.environ.get("SOLCAST_API_KEY", "").strip()
BATTERY_KWH = float(os.environ.get("SOLCAST_BATTERY_KWH", "15"))  # default 15

# ISO-8601 duration "PT#H#M#S" to hours (float)
_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def DurationHours(iso: str) -> float:
    t = _DURATION_RE.match(iso)
    if not t:
        # Fallback: default to 0.5h if missing/unexpected
        return 0.5
    h = int(t.group(1) or 0)
    m = int(t.group(2) or 0)
    s = int(t.group(3) or 0)
    return h + m / 60.0 + s / 3600.0


def ParsePeriodEnd(s: str) -> datetime:
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


def FetchForecast() -> dict:
    if not SITE_ID or not API_KEY:
        raise RuntimeError(
            "Set SOLCAST_SITE_ID and SOLCAST_API_KEY in the environment."
        )

    url = f"{BASE_URL}/rooftop_sites/{SITE_ID}/forecasts?format=json"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {API_KEY}")

    # Perform up to 3 retries (after the first failure) for DNS resolution issues.
    max_retries = 3
    retries_done = 0
    retry_delay = 1
    printed = False

    while True:
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if printed:
                    # Finish the retry status line with a newline.
                    print("", file=sys.stderr)
                return data
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            is_dns = isinstance(reason, socket.gaierror)

            if is_dns and (retries_done < max_retries):
                if not printed:
                    print("DNS lookup failed,", end="", file=sys.stderr)
                    printed = True
                print(" retrying", end="", file=sys.stderr, flush=True)
                time.sleep(retry_delay)
                retry_delay *= 2
                retries_done += 1
                continue

            if printed:
                print("", file=sys.stderr)
            raise
        except Exception:
            if printed:
                print("", file=sys.stderr)
            raise


def LoadOrRefreshCache(now_local: datetime) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    need_fetch = True
    cache_is_stale = False

    if CACHE_FILE.exists():
        try:
            mtime_local = datetime.fromtimestamp(
                CACHE_FILE.stat().st_mtime, LOCAL_TZ
            )
            # Delete cache if older than 3 days.
            if (now_local - mtime_local) > timedelta(days=3):
                try:
                    CACHE_FILE.unlink()
                    print(
                        "Info: deleted stale Solcast cache (>3 days old).",
                        file=sys.stderr,
                    )
                except Exception:
                    # If deletion failed, mark as stale so we won't use it.
                    cache_is_stale = True
                need_fetch = True
            else:
                # Same-day cache means we can skip fetching.
                if mtime_local.date() == now_local.date():
                    need_fetch = False
        except Exception:
            need_fetch = True

    if need_fetch:
        try:
            data = FetchForecast()
            CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
        except urllib.error.HTTPError as e:
            # Fetch failed. If a usable cache exists, fall back with warning.
            if CACHE_FILE.exists() and not cache_is_stale:
                try:
                    mtime_local = datetime.fromtimestamp(
                        CACHE_FILE.stat().st_mtime, LOCAL_TZ
                    )
                    ts = mtime_local.strftime("%Y-%m-%d %H:%M %Z")
                except Exception:
                    ts = "unknown time"
                print(
                    f"Warning: fetch failed (HTTP {e.code}: {e.reason}); "
                    f"using cached forecast from {ts}.",
                    file=sys.stderr,
                )
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            else:
                raise RuntimeError(f"HTTP error {e.code}: {e.reason}")
        except Exception as e:
            if CACHE_FILE.exists() and not cache_is_stale:
                # Generic failure; fall back with warning.
                try:
                    mtime_local = datetime.fromtimestamp(
                        CACHE_FILE.stat().st_mtime, LOCAL_TZ
                    )
                    ts = mtime_local.strftime("%Y-%m-%d %H:%M %Z")
                except Exception:
                    ts = "unknown time"
                print(
                    f"Warning: fetch failed ({type(e).__name__}: {e}); "
                    f"using cached forecast from {ts}.",
                    file=sys.stderr,
                )
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            else:
                raise RuntimeError(
                    "Could not fetch Solcast forecast and no cache exists.\n"
                    f"{type(e).__name__}: {e}"
                )
    else:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

    return data


def KwhByDay(data: dict, now_local: datetime):
    """Return list of dicts with per-day kWh for mean and optimistic(90th).
    For 'today', only include intervals with period_end_local >= now_local.
    """
    forecasts = data.get("forecasts", [])
    if not forecasts:
        return []

    last_period = forecasts[0].get("period", "PT30M")
    sums = {}  # day -> {"mean": x, "opt": y}

    for row in forecasts:
        mean_kw = float(row.get("pv_estimate", 0.0))
        opt_kw = float(row.get("pv_estimate90", mean_kw))
        per = row.get("period", last_period) or last_period
        last_period = per
        hours = DurationHours(per)

        t_utc = ParsePeriodEnd(row["period_end"])  # aware UTC
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
        rows.append(
            {
                "day": day,
                "kwh_mean": sums[day]["mean"],
                "kwh_opt": sums[day]["opt"],
                "is_today": (day == now_local.date().isoformat()),
            }
        )
    return rows


def main() -> int:
    now_local = datetime.now(LOCAL_TZ)

    try:
        data = LoadOrRefreshCache(now_local)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    rows = KwhByDay(data, now_local)
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
        print(
            f"  {day_str}:  {r['kwh_mean']:6.2f}    {r['kwh_opt']:6.2f}    "
            f"({pct:5.1f}%)" + suffix
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
