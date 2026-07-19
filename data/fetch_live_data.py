#!/usr/bin/env python3
"""Load LIVE weather + ALL-INDIA mandi prices into Snowflake.

Sources:
  - Weather: Open-Meteo (no API key), past 10 days + today, per pilot district
    (on-demand weather for any other district is a Snowflake proc,
    GET_DISTRICT_WEATHER — see data/setup_on_demand_weather.sql).
  - Mandi prices: data.gov.in Agmarknet daily feed, full national snapshot
    (resource 9ef84268-d588-465a-a308-a864a43d0070, needs DATA_GOV_KEY).

Idempotent: rows land via MERGE keyed on the natural key, so re-running
(daily cron/Task) never duplicates. The Agmarknet feed only carries the
latest day's snapshot, so daily runs accumulate history in Snowflake.

Usage:
  python data/fetch_live_data.py            # weather + mandi -> Snowflake
  python data/fetch_live_data.py --dry-run  # fetch + summarize, no Snowflake
  python data/fetch_live_data.py --weather-only | --mandi-only
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent

# Telangana pilot districts (CLAUDE.md scope) with coordinates for Open-Meteo.
DISTRICTS = {
    "Warangal": (17.978, 79.594),
    "Khammam": (17.247, 80.151),
    "Karimnagar": (18.438, 79.128),
    "Nizamabad": (18.673, 78.100),
}

AGMARKNET_RESOURCE = "9ef84268-d588-465a-a308-a864a43d0070"
AGMARKNET_URL = f"https://api.data.gov.in/resource/{AGMARKNET_RESOURCE}"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# data.gov.in's WAF silently drops the default python-requests User-Agent
# (times out / 502s); any other UA is served instantly.
HEADERS = {"User-Agent": "kisan-copilot/1.0"}

IST = timezone(timedelta(hours=5, minutes=30))


def load_env():
    """Minimal .env loader so we don't hard-depend on python-dotenv."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if value and key not in os.environ:
            os.environ[key] = value


def fetch_weather():
    """Return rows (district, date, temp_max, temp_min, rainfall_mm, humidity)."""
    today = datetime.now(IST).date()
    rows = []
    for district, (lat, lon) in DISTRICTS.items():
        resp = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "hourly": "relative_humidity_2m",
                "past_days": 10,
                "forecast_days": 1,
                "timezone": "Asia/Kolkata",
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        # Average hourly humidity into a per-day mean.
        humidity_by_day = {}
        for ts, rh in zip(data["hourly"]["time"], data["hourly"]["relative_humidity_2m"]):
            if rh is None:
                continue
            humidity_by_day.setdefault(ts[:10], []).append(rh)

        daily = data["daily"]
        for i, day in enumerate(daily["time"]):
            if date.fromisoformat(day) > today:  # skip forecast rows
                continue
            rh_vals = humidity_by_day.get(day)
            rows.append((
                district,
                day,
                daily["temperature_2m_max"][i],
                daily["temperature_2m_min"][i],
                daily["precipitation_sum"][i],
                round(sum(rh_vals) / len(rh_vals), 1) if rh_vals else None,
            ))
    return rows


def fetch_mandi_prices(api_key):
    """Return rows (commodity, variety, state, district, market, min, max, modal, arrival_date).

    Pulls the FULL national Agmarknet snapshot (all states, all commodities —
    scope filtering happens at query time, not load time). Rows are deduped on
    the merge key because the feed splits records by `grade`, which we don't
    store; duplicate source keys would make the Snowflake MERGE error out.
    """
    seen, rows, offset, limit = set(), [], 0, 1000
    while True:
        # data.gov.in is flaky under load (evening especially) — retry hard,
        # because the feed only carries today's snapshot: a failed run means
        # that day's data is gone for good.
        for attempt in range(5):
            try:
                resp = requests.get(
                    AGMARKNET_URL,
                    params={
                        "api-key": api_key,
                        "format": "json",
                        "limit": limit,
                        "offset": offset,
                    },
                    headers=HEADERS,
                    timeout=90,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == 4:
                    # Don't re-raise raw: the exception text embeds the
                    # request URL, which contains the api key.
                    status = getattr(getattr(e, "response", None), "status_code", "n/a")
                    raise RuntimeError(
                        f"data.gov.in request failed after 5 attempts: "
                        f"{type(e).__name__} (http {status})"
                    ) from None
                time.sleep(5 * (2 ** attempt))
        records = resp.json().get("records", [])
        for r in records:
            try:
                arrival = datetime.strptime(r["arrival_date"], "%d/%m/%Y").date().isoformat()
                row = (
                    r["commodity"].strip(),
                    (r.get("variety") or "Other").strip(),
                    r["state"].strip(),
                    r["district"].strip(),
                    r["market"].strip(),
                    float(r["min_price"]),
                    float(r["max_price"]),
                    float(r["modal_price"]),
                    arrival,
                )
            except (KeyError, ValueError, TypeError):
                continue  # skip malformed records rather than fail the load
            key = (row[0], row[1], row[2], row[3], row[4], row[8])
            if key not in seen:  # first grade listed wins
                seen.add(key)
                rows.append(row)
        if len(records) < limit:
            return rows
        offset += limit


def connect_snowflake():
    import snowflake.connector

    key_path = os.environ.get("SF_PRIVATE_KEY_PATH")
    if key_path and (REPO_ROOT / key_path).exists():
        # Key-pair: no browser, no token expiry — reliable for cron/CI.
        conn = snowflake.connector.connect(
            account=os.environ.get("SF_ACCOUNT", "bm13081.ap-southeast-2"),
            user=os.environ.get("SF_USER", "NIKKY001"),
            private_key_file=str(REPO_ROOT / key_path),
        )
    elif os.environ.get("SNOWFLAKE_CONNECTION_NAME"):
        conn = snowflake.connector.connect(
            connection_name=os.environ["SNOWFLAKE_CONNECTION_NAME"])
    else:
        conn = snowflake.connector.connect(
            account=os.environ["SF_ACCOUNT"],
            user=os.environ["SF_USER"],
            password=os.environ["SF_PASSWORD"],
        )
    cur = conn.cursor()
    cur.execute("USE WAREHOUSE KISAN_WH")
    cur.execute("USE SCHEMA AGRI.PUBLIC")
    return conn


def merge_rows(conn, temp_ddl, insert_sql, merge_sql, rows, label):
    if not rows:
        print(f"  {label}: nothing to load")
        return
    cur = conn.cursor()
    cur.execute(temp_ddl)
    cur.executemany(insert_sql, rows)
    cur.execute(merge_sql)
    inserted, updated = cur.fetchone()[:2]
    print(f"  {label}: {len(rows)} fetched -> {inserted} inserted, {updated} updated")


WEATHER_MERGE = """
MERGE INTO AGRI.PUBLIC.WEATHER t
USING TMP_WEATHER s
  ON t.district = s.district AND t.date = s.date
WHEN MATCHED THEN UPDATE SET
  t.temp_max = s.temp_max, t.temp_min = s.temp_min,
  t.rainfall_mm = s.rainfall_mm, t.humidity = s.humidity
WHEN NOT MATCHED THEN INSERT (district, date, temp_max, temp_min, rainfall_mm, humidity)
  VALUES (s.district, s.date, s.temp_max, s.temp_min, s.rainfall_mm, s.humidity)
"""

MANDI_MERGE = """
MERGE INTO AGRI.PUBLIC.MANDI_PRICES t
USING TMP_MANDI s
  ON t.commodity = s.commodity AND t.variety = s.variety AND t.state = s.state
 AND t.district = s.district AND t.market = s.market AND t.arrival_date = s.arrival_date
WHEN MATCHED THEN UPDATE SET
  t.min_price = s.min_price, t.max_price = s.max_price, t.modal_price = s.modal_price
WHEN NOT MATCHED THEN INSERT
  (commodity, variety, state, district, market, min_price, max_price, modal_price, arrival_date)
  VALUES (s.commodity, s.variety, s.state, s.district, s.market,
          s.min_price, s.max_price, s.modal_price, s.arrival_date)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="fetch only, no Snowflake")
    parser.add_argument("--weather-only", action="store_true")
    parser.add_argument("--mandi-only", action="store_true")
    args = parser.parse_args()
    load_env()

    weather_rows, mandi_rows = [], []

    if not args.mandi_only:
        print("Fetching weather (Open-Meteo, past 10 days, 4 districts)...")
        weather_rows = fetch_weather()
        print(f"  {len(weather_rows)} weather rows, "
              f"dates {min(r[1] for r in weather_rows)} .. {max(r[1] for r in weather_rows)}")

    if not args.weather_only:
        api_key = os.environ.get("DATA_GOV_KEY")
        if not api_key:
            print("Skipping mandi prices: DATA_GOV_KEY not set in .env", file=sys.stderr)
        else:
            print("Fetching mandi prices (data.gov.in Agmarknet, all India)...")
            mandi_rows = fetch_mandi_prices(api_key)
            dates = sorted({r[8] for r in mandi_rows})
            states = len({r[2] for r in mandi_rows})
            print(f"  {len(mandi_rows)} price rows across {states} states, "
                  f"arrival dates: {dates or 'none'}")

    if args.dry_run:
        for r in weather_rows[:3]:
            print("  sample weather:", r)
        for r in mandi_rows[:3]:
            print("  sample mandi:", r)
        print("Dry run — not writing to Snowflake.")
        return

    print("Connecting to Snowflake...")
    conn = connect_snowflake()
    try:
        if weather_rows:
            merge_rows(
                conn,
                "CREATE TEMP TABLE TMP_WEATHER LIKE AGRI.PUBLIC.WEATHER",
                "INSERT INTO TMP_WEATHER VALUES (%s, %s, %s, %s, %s, %s)",
                WEATHER_MERGE, weather_rows, "WEATHER",
            )
        if mandi_rows:
            merge_rows(
                conn,
                "CREATE TEMP TABLE TMP_MANDI LIKE AGRI.PUBLIC.MANDI_PRICES",
                "INSERT INTO TMP_MANDI VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                MANDI_MERGE, mandi_rows, "MANDI_PRICES",
            )
    finally:
        conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
